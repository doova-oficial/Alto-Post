"""Geração de legendas com a API da Groq (endpoint compatível com chat completions).

Documentação consultada (out/2026): https://console.groq.com/docs/models ,
https://console.groq.com/docs/rate-limits , https://console.groq.com/docs/reasoning ,
https://console.groq.com/docs/deprecations .

* Modelo configurável por ``GROQ_MODEL`` (padrão ``openai/gpt-oss-120b``, modelo de produção).
* Timeout, tentativas limitadas com backoff exponencial, respeito a ``retry-after`` (429)
  e intervalo mínimo entre chamadas para não estourar o limite de uso.
* A saída passa por ``caption_validator`` (remove emojis, limita hashtags, detecta
  promessas proibidas). Falhas nunca produzem legenda vazia: levantam ``CaptionGenerationError``.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.services.caption_validator import CaptionCheck, validate_caption
from app.services.doova_context import context_as_text

logger = logging.getLogger(__name__)
PROMPT_VERSION = "v4"

STRUCTURES = [
    "comece com uma frase direta sobre o problema que o público enfrenta",
    "comece com uma pergunta curta ao leitor",
    "comece apresentando a ideia principal da arte em uma frase",
    "use dois parágrafos curtos: contexto e convite",
    "use uma frase de abertura e uma lista curta de até três linhas iniciadas por hífen",
    "comece com uma observação prática do dia a dia de quem presta ou contrata serviços",
]


class CaptionGenerationError(RuntimeError):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class CaptionRequest:
    topic: str
    audience: str
    objective: str
    art_text: str
    template: str
    cta: str | None = None
    message_type: str | None = None
    extra_context: str | None = None
    verified_opportunity: str | None = None
    structure_hint: str | None = None


@dataclass
class CaptionResult:
    text: str
    raw_text: str
    model: str
    prompt_version: str
    check: CaptionCheck
    usage: dict[str, Any] = field(default_factory=dict)


def build_system_prompt(settings: Settings) -> str:
    emoji_rule = (
        "- Não utilize emojis. Nunca produza legendas cheias de emojis."
        if not settings.caption_allow_emojis
        else "- Emojis são permitidos com moderação: no máximo um, e somente se acrescentar sentido."
    )
    if settings.caption_max_hashtags > 0:
        hashtag_rule = (
            f"- Use no máximo {settings.caption_max_hashtags} hashtags, no final da legenda, em uma linha separada.\n"
            "- A primeira hashtag deve ser sempre #doova.\n"
            "- As outras devem ser palavras ou expressões reais e comuns em português, em minúsculas, sem acentos "
            "e sem espaços, ligadas diretamente ao assunto (exemplos: #servicos, #telhado, #festa, #ipora, #goias).\n"
            "- Nunca junte palavras para criar termos que as pessoas não usam (exemplo do que NÃO fazer: "
            "#consertodetalhado). Nunca use hashtags que não tenham relação com o texto.\n"
            "- Se não houver uma hashtag natural e conhecida para o assunto, use só #doova."
        )
    else:
        hashtag_rule = "- Não utilize hashtags."
    return f"""Você escreve legendas para o Instagram do Doova.

{context_as_text()}

Regras obrigatórias:
- Escreva em português brasileiro.
- Use linguagem natural, profissional, clara e objetiva, com tom direto, confiável e acessível.
{emoji_rule}
- Prefira legendas curtas e bem organizadas (entre 2 e 5 frases, parágrafos curtos).
{hashtag_rule}
- Evite clichês, exageros, excesso de pontuação e linguagem artificial ou publicitária demais.
- Não repita sempre a mesma estrutura; siga a sugestão de estrutura indicada.
- Não invente funcionalidades, usuários, depoimentos, vagas, números ou resultados.
- Não prometa renda, ganhos ou clientes garantidos.
- Não apresente oportunidades inexistentes como reais.
- Não inclua informações sem respaldo no contexto fornecido.
- Inclua uma chamada para ação discreta quando apropriado.
- Não use colchetes, marcadores de preenchimento ou instruções.
- Entregue somente a legenda pronta para publicação, sem título, aspas, explicações ou comentários."""


def build_user_prompt(req: CaptionRequest) -> str:
    parts = [
        f"Tema da publicação: {req.topic}",
        f"Público-alvo: {req.audience}",
        f"Objetivo da campanha: {req.objective}",
        f"Template visual: {req.template}",
        f"Tipo de mensagem: {req.message_type or 'informação'}",
        f"Texto que aparece na arte: {req.art_text}",
        f"Chamada para ação da arte: {req.cta or '(nenhuma)'}",
        f"Sugestão de estrutura: {req.structure_hint or random.choice(STRUCTURES)}",
    ]
    if req.verified_opportunity:
        parts.append(f"Oportunidade verificada pela equipe (pode ser mencionada exatamente assim): {req.verified_opportunity}")
    else:
        parts.append("Não há oportunidade de trabalho verificada nesta publicação: não mencione vagas.")
    if req.extra_context:
        parts.append(f"Contexto adicional fornecido pela equipe: {req.extra_context}")
    parts.append("A legenda deve complementar a arte, sem repetir o texto dela palavra por palavra.")
    return "\n".join(parts)


class _Throttle:
    def __init__(self):
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self, min_interval: float) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._last + min_interval - now
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


_throttle = _Throttle()


class GroqCaptionService:
    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None,
                 sleep=time.sleep):
        self.settings = settings or get_settings()
        self._client = client
        self._sleep = sleep

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.settings.groq_timeout_seconds)
        return self._client

    def _payload(self, req: CaptionRequest) -> dict[str, Any]:
        s = self.settings
        payload: dict[str, Any] = {
            "model": s.groq_model,
            "messages": [
                {"role": "system", "content": build_system_prompt(s)},
                {"role": "user", "content": build_user_prompt(req)},
            ],
            "temperature": s.groq_temperature,
            "max_completion_tokens": s.groq_max_completion_tokens,
        }
        if s.groq_model.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = s.groq_reasoning_effort
            payload["include_reasoning"] = False
        return payload

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        s = self.settings
        if not s.groq_configured:
            raise CaptionGenerationError("GROQ_API_KEY não configurada", retryable=False)
        url = f"{s.groq_base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {s.groq_api_key.get_secret_value()}", "Content-Type": "application/json"}
        last_err = "erro desconhecido"
        for attempt in range(s.groq_max_retries + 1):
            _throttle.wait(s.groq_min_interval_seconds)
            try:
                resp = self._http().post(url, json=payload, headers=headers, timeout=s.groq_timeout_seconds)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = f"falha de rede/timeout: {type(e).__name__}"
                logger.warning("Groq: %s (tentativa %d)", last_err, attempt + 1)
                self._backoff(attempt, None)
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {resp.status_code}"
                logger.warning("Groq indisponível/limitada: %s (tentativa %d)", last_err, attempt + 1)
                self._backoff(attempt, resp.headers.get("retry-after"))
                continue
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")[:300]
            except Exception:
                detail = resp.text[:300]
            # 400/401/403/404: erro de configuração (chave inválida, modelo descontinuado...)
            raise CaptionGenerationError(f"Groq HTTP {resp.status_code}: {detail}", retryable=False)
        raise CaptionGenerationError(f"Groq indisponível após {s.groq_max_retries + 1} tentativas: {last_err}")

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if attempt >= self.settings.groq_max_retries:
            return
        delay = min(30.0, 1.5 * (2 ** attempt)) + random.uniform(0, 0.5)
        if retry_after:
            try:
                delay = max(delay, min(60.0, float(retry_after)))
            except ValueError:
                pass
        self._sleep(delay)

    def generate(self, req: CaptionRequest) -> CaptionResult:
        payload = self._payload(req)
        data = self._call(payload)
        try:
            choice = data["choices"][0]
            raw = choice["message"].get("content") or ""
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError):
            raise CaptionGenerationError("resposta da Groq em formato inesperado")
        if not raw.strip():
            raise CaptionGenerationError("Groq retornou legenda vazia")
        if finish == "length":
            raise CaptionGenerationError("legenda truncada pelo limite de tokens")
        s = self.settings
        check = validate_caption(raw, allow_emojis=s.caption_allow_emojis, max_hashtags=s.caption_max_hashtags,
                                 min_chars=s.caption_min_chars, max_chars=s.caption_max_chars,
                                 verified_opportunity=bool(req.verified_opportunity))
        if not check.text:
            raise CaptionGenerationError("legenda vazia após validação")
        return CaptionResult(check.text, raw, s.groq_model, PROMPT_VERSION, check, data.get("usage", {}))

    def chat_json(self, system: str, user: str, max_tokens: int = 700) -> dict[str, Any]:
        """Chamada que deve devolver um objeto JSON (usada para pautas de assuntos do momento)."""
        s = self.settings
        payload: dict[str, Any] = {
            "model": s.groq_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.4,
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if s.groq_model.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = s.groq_reasoning_effort
            payload["include_reasoning"] = False
        data = self._call(payload)
        try:
            raw = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            raise CaptionGenerationError("resposta da Groq em formato inesperado")
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise CaptionGenerationError("a Groq não devolveu JSON", retryable=False)
        try:
            out = json.loads(m.group(0))
        except json.JSONDecodeError:
            raise CaptionGenerationError("JSON inválido devolvido pela Groq", retryable=False)
        if not isinstance(out, dict):
            raise CaptionGenerationError("JSON da Groq não é um objeto", retryable=False)
        return out

    def list_models(self) -> list[str]:
        """Consulta os modelos disponíveis na conta (GET /models)."""
        s = self.settings
        if not s.groq_configured:
            raise CaptionGenerationError("GROQ_API_KEY não configurada", retryable=False)
        resp = self._http().get(f"{s.groq_base_url.rstrip('/')}/models",
                                headers={"Authorization": f"Bearer {s.groq_api_key.get_secret_value()}"})
        resp.raise_for_status()
        return sorted(m["id"] for m in resp.json().get("data", []))
