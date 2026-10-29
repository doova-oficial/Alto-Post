from datetime import timedelta

import httpx
import pytest

from app.database import utcnow
from app.models import PostStatus
from app.services.caption_validator import limit_hashtags, strip_emojis, validate_caption
from app.services.groq_service import CaptionGenerationError, CaptionRequest
from tests.conftest import CAPTION_OK, make_content, make_groq, new_post, reload_settings

REQ = CaptionRequest(topic="Chegada em Iporá", audience="prestadores", objective="apresentar", art_text="Título",
                     template="autonomos")


def test_strip_emojis_preserves_portuguese():
    s = "Ação rápida! 🚀🔥 Você já pensou? 👍🏽 São — “aspas” ç ã õ é 1️⃣ 🇧🇷 família 👨‍👩‍👧 ok."
    out = strip_emojis(s)
    assert out == "Ação rápida! Você já pensou? São — “aspas” ç ã õ é família ok."
    for ch in "çãõéá—“”":
        assert ch in s and (ch in out or ch not in s)


def test_hashtags_limited():
    out, removed = limit_hashtags("Texto. #a #b #c #d #e", 3)
    assert out == "Texto. #a #b #c" and removed == 2


@pytest.mark.parametrize("text,issue", [
    ("Com o Doova você tem renda garantida todo mês, sem esforço algum.", "renda garantida"),
    ("Já somos 500 usuários em Iporá usando o aplicativo todos os dias.", "usuários"),
    ("Temos vagas abertas para eletricistas em Iporá, venha conferir agora.", "vaga"),
    ("Claro! Aqui está a legenda: O Doova chegou para ajudar a cidade toda.", "introdução"),
    ("O Doova chegou em [cidade] para conectar prestadores e clientes.", "placeholder"),
    ("O Doova chegou em Iporá para conectar prestadores e clientes e", "incompleta"),
])
def test_validation_flags_problems(text, issue):
    chk = validate_caption(text)
    assert chk.needs_review and not chk.is_valid
    assert any(issue in i for i in chk.issues), chk.issues


def test_emojis_can_be_enabled_by_config():
    text = "O Doova está chegando em Iporá para aproximar prestadores e clientes. 🚀"
    assert "🚀" in validate_caption(text, allow_emojis=True).text
    assert "🚀" not in validate_caption(text, allow_emojis=False).text


def test_groq_success_is_cleaned():
    svc = make_groq(["Seu serviço merece ser encontrado em Iporá!! 🚀 Siga o Doova e acompanhe. #a #b #c #d"])
    res = svc.generate(REQ)
    assert "🚀" not in res.text and "!!" not in res.text
    assert res.text.count("#") == 3
    body = svc.stub.requests[0]
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["include_reasoning"] is False
    sys_prompt = body["messages"][0]["content"]
    assert "Não utilize emojis" in sys_prompt and "no máximo 3 hashtags" in sys_prompt
    assert "Não prometa renda" in sys_prompt


def test_groq_retries_on_5xx_then_fails():
    svc = make_groq([httpx.Response(503, json={})])
    with pytest.raises(CaptionGenerationError) as e:
        svc.generate(REQ)
    assert e.value.retryable is True
    assert len(svc.stub.requests) == svc.settings.groq_max_retries + 1


def test_groq_respects_retry_after():
    svc = make_groq([httpx.Response(429, headers={"retry-after": "7"}, json={}), CAPTION_OK])
    assert svc.generate(REQ).text
    assert svc.sleeps and svc.sleeps[0] >= 7


def test_groq_timeout_is_retried():
    svc = make_groq([httpx.ReadTimeout("t"), CAPTION_OK])
    assert svc.generate(REQ).text and len(svc.stub.requests) == 2


def test_groq_auth_error_not_retried():
    svc = make_groq([httpx.Response(401, json={"error": {"message": "invalid key"}})])
    with pytest.raises(CaptionGenerationError) as e:
        svc.generate(REQ)
    assert e.value.retryable is False and len(svc.stub.requests) == 1


@pytest.mark.parametrize("resp", [
    httpx.Response(200, json={"choices": [{"message": {"content": "   "}, "finish_reason": "stop"}]}),
    httpx.Response(200, json={"choices": [{"message": {"content": "Texto cortado"}, "finish_reason": "length"}]}),
    httpx.Response(200, json={"unexpected": True}),
])
def test_groq_empty_or_truncated_output_raises(resp):
    with pytest.raises(CaptionGenerationError):
        make_groq([resp]).generate(REQ)


def test_missing_key_is_not_retryable(monkeypatch):
    s = reload_settings(monkeypatch, GROQ_API_KEY="")
    with pytest.raises(CaptionGenerationError) as e:
        make_groq(settings=s).generate(REQ)
    assert e.value.retryable is False


def test_post_with_groq_down_goes_to_caption_failed_and_recovers(db):
    cs = make_content([httpx.Response(503, json={})])
    p = new_post(db, cs, caption=None)
    assert p.status == PostStatus.CAPTION_FAILED.value
    assert p.caption_id is None and p.next_attempt_at is not None
    # a Groq volta a funcionar
    cs.captions.stub.responses = [CAPTION_OK]
    assert cs.retry_failed_captions(db, now=utcnow() - timedelta(minutes=1)) == 0  # ainda não venceu
    assert cs.retry_failed_captions(db, now=p.next_attempt_at + timedelta(seconds=1)) == 1
    db.refresh(p)
    assert p.status == PostStatus.PENDING_APPROVAL.value and p.caption.text


def test_post_goes_to_review_after_max_caption_attempts(db, monkeypatch):
    s = reload_settings(monkeypatch, CAPTION_MAX_ATTEMPTS=1)
    p = new_post(db, make_content([httpx.Response(503, json={})], settings=s), caption=None)
    assert p.status == PostStatus.NEEDS_REVIEW.value and p.caption_id is None


def test_post_without_caption_cannot_be_approved(db):
    from app.services.content_service import StateError

    cs = make_content([httpx.Response(401, json={})])
    p = new_post(db, cs, caption=None)
    assert p.status == PostStatus.NEEDS_REVIEW.value
    with pytest.raises(StateError):
        cs.approve(db, p)


def test_invalid_generated_caption_goes_to_review(db):
    cs = make_content(["Com o Doova você tem renda garantida todo mês em Iporá, comece agora."])
    p = new_post(db, cs, caption=None)
    assert p.status == PostStatus.NEEDS_REVIEW.value
    assert any("renda garantida" in i for i in p.caption.issues)