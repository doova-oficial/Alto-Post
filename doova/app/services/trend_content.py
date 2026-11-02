"""Assuntos do momento: tendência do Google Trends → palavra genérica → foto gratuita → publicação.

O fluxo é conservador de propósito:

1. **Triagem**: um assunto é descartado se o termo ou as manchetes ligadas a ele tiverem
   palavras sensíveis. Isso inclui tragédia, morte, crime, polícia, política, eleição,
   religião, saúde/epidemia, apostas e conteúdo adulto.
2. **Relação com serviços**: o assunto só segue se combinar com um tema de serviço do
   ``TREND_THEMES``, por exemplo calor → manutenção de ar-condicionado e chuva → telhado.
   Com a Groq configurada, ela pode encaixar em um desses temas um assunto que as palavras
   não pegaram. Ela não inventa temas novos e também pode vetar o assunto.
3. **Foto**: a busca usa uma palavra genérica do tema (nunca o nome de pessoas ou marcas).
   Só entram licenças comerciais, e o crédito vai na arte e na legenda.
4. **Revisão**: a publicação sempre vai para revisão manual e expira em
   ``TREND_POST_TTL_HOURS`` se não for publicada. Assunto do momento envelhece rápido.

O texto da arte vem dos temas abaixo (frases genéricas sobre a necessidade de um
serviço). Ele nunca afirma fatos sobre a notícia nem sobre o Doova além da proposta geral.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import utcnow
from app.models import ContentKind, Post, PostStatus
from app.services.content_service import ContentError, ContentService, PostInput, set_status
from app.services.groq_service import CaptionGenerationError, GroqCaptionService
from app.services.image_search import find_photo

logger = logging.getLogger(__name__)

PHOTO_TEMPLATES = ("foto_destaque", "foto_fundo")
SUBTITLES = {
    "contratantes": "O Doova conecta quem precisa de um serviço a quem sabe fazer.",
    "prestadores": "O Doova conecta quem oferece serviços a quem precisa contratar.",
    "ambos": "O Doova conecta quem precisa de um serviço a quem sabe fazer.",
}

# Temas de serviço aceitos. query_en vai para o Openverse (acervo majoritariamente em inglês);
# query_pt vai para o Pixabay (lang=pt). Edite à vontade: só palavras genéricas, sem pessoas ou marcas.
TREND_THEMES: dict[str, dict[str, Any]] = {
    "calor": {"label": "manutenção de ar-condicionado e ventiladores", "audience": "contratantes",
              "keywords": ["calor", "onda de calor", "temperatura", "ar condicionado", "ar-condicionado",
                           "ventilador", "climatizador"],
              "query_en": "air conditioner repair", "query_pt": "ar condicionado manutenção",
              "title": "Calor apertando? O ar-condicionado está em dia?"},
    "chuva": {"label": "conserto de telhado e calhas", "audience": "contratantes",
              "keywords": ["chuva", "chuvas", "temporal", "tempestade", "goteira", "frente fria"],
              "query_en": "roof repair", "query_pt": "telhado conserto",
              "title": "Choveu e apareceu goteira?"},
    "festas": {"label": "comida, bolos e decoração para festas", "audience": "contratantes",
               "keywords": ["festa", "festas", "natal", "ano novo", "reveillon", "carnaval", "festa junina",
                            "aniversario", "dia das maes", "dia dos pais", "dia das criancas", "casamento",
                            "formatura", "pascoa", "confraternizacao"],
               "query_en": "party food buffet", "query_pt": "festa salgados",
               "title": "Vai ter festa? Precisa de ajuda com comida ou decoração?"},
    "estudos": {"label": "aulas particulares e reforço escolar", "audience": "ambos",
                "keywords": ["enem", "vestibular", "volta as aulas", "simulado", "provas", "redacao enem"],
                "query_en": "student studying tutor", "query_pt": "estudante estudando",
                "title": "Precisa de aulas de reforço?"},
    "energia": {"label": "serviços de eletricista", "audience": "contratantes",
                "keywords": ["conta de luz", "energia eletrica", "falta de energia", "bandeira tarifaria",
                             "eletricista", "queda de energia"],
                "query_en": "electrician working", "query_pt": "eletricista",
                "title": "Precisa de um eletricista?"},
    "agua": {"label": "serviços de encanador", "audience": "contratantes",
             "keywords": ["falta de agua", "falta d agua", "vazamento", "encanador", "caixa d agua",
                          "conta de agua"],
             "query_en": "plumber fixing pipe", "query_pt": "encanador",
             "title": "Vazamento ou cano entupido?"},
    "pets": {"label": "banho, tosa e cuidado com pets", "audience": "contratantes",
             "keywords": ["pet", "pets", "cachorro", "cachorros", "gato", "gatos", "dia dos animais",
                          "dia do cachorro"],
             "query_en": "dog grooming", "query_pt": "banho e tosa cachorro",
             "title": "Precisa de alguém para cuidar do seu pet?"},
    "autonomos": {"label": "trabalho autônomo e divulgação de serviços", "audience": "prestadores",
                  "keywords": ["mei", "microempreendedor", "autonomo", "autonomos", "trabalho informal",
                               "dia do trabalhador", "dia do trabalho", "empreendedorismo"],
                  "query_en": "craftsman workshop", "query_pt": "trabalhador autônomo",
                  "title": "Trabalha por conta própria?"},
    "limpeza": {"label": "faxina e limpeza", "audience": "contratantes",
                "keywords": ["faxina", "diarista", "limpeza de casa", "dia da faxina"],
                "query_en": "house cleaning", "query_pt": "limpeza casa",
                "title": "Casa precisando de uma faxina?"},
    "jardim": {"label": "jardinagem, capina e poda", "audience": "contratantes",
               "keywords": ["primavera", "jardinagem", "jardim", "quintal", "capina", "poda"],
               "query_en": "gardener lawn mowing", "query_pt": "jardinagem",
               "title": "Quintal precisando de capina ou poda?"},
    "veiculos": {"label": "mecânica e revisão de carros e motos", "audience": "contratantes",
                 "keywords": ["ipva", "revisao do carro", "mecanico", "oficina mecanica", "troca de oleo",
                              "licenciamento"],
                 "query_en": "car mechanic", "query_pt": "mecânico carro",
                 "title": "Carro ou moto precisando de revisão?"},
    "reforma": {"label": "reforma, pintura e pequenos reparos", "audience": "contratantes",
                "keywords": ["reforma", "pintura de casa", "pedreiro", "construcao civil", "material de construcao"],
                "query_en": "house painter painting wall", "query_pt": "pintor parede",
                "title": "Pensando em reformar ou pintar a casa?"},
}

# Palavras sensíveis (sem acento). "*" no fim = qualquer palavra que comece assim.
SENSITIVE = [
    "morte*", "morre*", "morto*", "obito*", "falec*", "velorio", "enterro", "luto", "acidente*", "tragedia*",
    "vitima*", "ferido*", "assassin*", "homicid*", "crime*", "criminos*", "preso", "presa", "presos", "prisao",
    "policia*", "policial*", "tiroteio*", "baleado*", "tiro", "tiros", "estupr*", "abuso*", "violencia*",
    "agress*", "sequestr*", "roubo*", "assalt*", "furto*", "trafic*", "droga*", "guerra*", "ataque*", "atentado*",
    "terroris*", "bomba*", "explos*", "incendio*", "queimada*", "enchente*", "alagamento*", "deslizamento*",
    "desabamento*", "desaparecid*", "naufrag*", "eleic*", "eleitor*", "candidat*", "presidente*", "lula",
    "bolsonaro", "partido*", "stf", "senado", "senador*", "deputad*", "vereador*", "prefeit*", "governador*",
    "ministr*", "politic*", "impeachment", "cpi", "golpe*", "protesto*", "greve*", "religi*", "igreja*",
    "aborto*", "epidemia*", "pandemia*", "surto*", "covid*", "dengue", "virus", "doenca*", "cancer", "hospital*",
    "internad*", "suicid*", "aposta*", "bet", "bets", "cassino*", "tigrinho", "loteria*", "mega sena",
    "megasena", "lotofacil", "sexo", "nude*", "porn*", "escandalo*", "polemica*", "briga*", "demiss*",
    "falencia*", "racis*", "homofob*", "preconceito*",
    # dinheiro e benefícios: viram "promessa de renda" com facilidade e mudam rápido
    "auxilio*", "beneficio*", "emprestimo*", "credito*", "financiamento*", "bolsa familia", "inss", "fgts",
    "imposto*", "receita federal", "golpe do*", "pix", "dinheiro", "salario*", "renda", "indeniza*",
]
_MONEY_RE = re.compile(r"(r\$|\b\d[\d.,]*\s*(mil|milhao|milhoes|bi|bilhao|reais)\b)")


def norm(s: str) -> str:
    t = "".join(c for c in unicodedata.normalize("NFD", (s or "").lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _pattern(words: list[str]) -> re.Pattern:
    parts = []
    for w in words:
        w = norm(w.rstrip("*")) + ("*" if w.endswith("*") else "")
        if not w.strip("*"):
            continue
        parts.append(re.escape(w[:-1]) + r"\w*" if w.endswith("*") else re.escape(w))
    return re.compile(r"\b(" + "|".join(parts) + r")\b")


_SENSITIVE_RE = _pattern(SENSITIVE)
_THEME_RES = {k: _pattern(v["keywords"]) for k, v in TREND_THEMES.items()}


def screen_trend(term: str, news: list[str] | None = None, settings: Settings | None = None) -> str | None:
    """Motivo do bloqueio, ou None se o assunto passou na triagem."""
    s = settings or get_settings()
    texts = [term] + list(news or [])
    pats = [_SENSITIVE_RE] + ([_pattern(s.trend_blocklist())] if s.trend_blocklist() else [])
    for t in texts:
        if _MONEY_RE.search((t or "").lower()) or _MONEY_RE.search(norm(t)):
            where = "no termo" if t is term else "em manchete relacionada"
            return f"valor em dinheiro {where} (evita parecer promessa de renda)"
        n = norm(t)
        for p in pats:
            m = p.search(n)
            if m:
                where = "no termo" if t is term else "em manchete relacionada"
                return f"assunto sensível ({m.group(0)!r} {where})"
    return None


def match_theme(term: str) -> str | None:
    n = norm(term)
    for key, rx in _THEME_RES.items():
        if rx.search(n):
            return key
    return None


GROQ_SYSTEM = (
    "Você faz a triagem de assuntos em alta no Google Trends para o Instagram do Doova, um marketplace de "
    "serviços e trabalhos informais que começa em Iporá (GO). Responda SOMENTE com um objeto JSON: "
    '{"sensitive": true|false, "theme": "<chave da lista ou null>", "reason": "<motivo curto>"}. '
    "Marque sensitive=true para tragédias, mortes, acidentes, crimes, política, eleições, religião, saúde, "
    "apostas, conteúdo adulto, pessoas (celebridades, atletas, políticos), marcas, jogos e resultados esportivos. "
    "Escolha um theme somente quando o assunto cria naturalmente procura por um dos serviços da lista; "
    "na dúvida use null. Nunca crie chaves novas."
)


def groq_theme(groq: GroqCaptionService, term: str, news: list[str]) -> tuple[str | None, str]:
    options = "\n".join(f"- {k}: {v['label']}" for k, v in TREND_THEMES.items())
    user = f"Assunto: {term}\nManchetes: {' | '.join(news[:3]) or '(nenhuma)'}\nServiços (chave: descrição):\n{options}"
    out = groq.chat_json(GROQ_SYSTEM, user, max_tokens=300)
    reason = str(out.get("reason") or "")[:200]
    if out.get("sensitive") is True:
        return None, f"vetado pela Groq: {reason or 'sensível'}"
    theme = out.get("theme")
    if isinstance(theme, str) and theme in TREND_THEMES:
        return theme, f"tema escolhido pela Groq: {reason}"
    return None, f"sem relação com serviços (Groq): {reason}"


# ------------------------------------------------------------------ fluxo
def _recent_count(db: Session, hours: int) -> int:
    cutoff = utcnow() - timedelta(hours=hours)
    return db.scalar(select(func.count()).select_from(Post).where(
        Post.content_kind == ContentKind.TREND.value, Post.created_at >= cutoff)) or 0


def _recent(db: Session, column, value: str, days: int) -> bool:
    cutoff = utcnow() - timedelta(days=days)
    return db.scalar(select(Post.id).where(column == value, Post.created_at >= cutoff).limit(1)) is not None


def run_trend_auto(db: Session, settings: Settings | None = None, force: bool = False,
                   client: httpx.Client | None = None, groq: GroqCaptionService | None = None,
                   content: ContentService | None = None, items: list[dict] | None = None,
                   create: bool = True) -> dict[str, Any]:
    """Avalia os assuntos em alta e (se ``create``) cria publicações com foto para revisão."""
    s = settings or get_settings()
    out: dict[str, Any] = {"created": [], "report": [], "skipped": None, "trends_error": None}
    if create and not (s.trend_auto_enabled or force):
        out["skipped"] = "TREND_AUTO_ENABLED=false (use force para rodar manualmente)"
        return out
    quota = s.trend_auto_max_per_day - _recent_count(db, 24)
    if create and quota <= 0:
        out["skipped"] = f"limite de {s.trend_auto_max_per_day} publicação(ões) de assunto do momento em 24 h atingido"
        return out
    if items is None:
        from app.services.trends_service import cached_trends

        data = cached_trends(db, s, client=client)
        items, out["trends_error"] = data.get("items", []), data.get("error")
    use_groq = s.trend_use_groq and s.groq_configured
    if use_groq and groq is None:
        groq = GroqCaptionService(s)
    seen: set[str] = set()
    for it in items:
        term = (it.get("term") or "").strip()
        key = norm(term)
        if not term or key in seen:
            continue
        seen.add(key)
        news = [n for n in (it.get("news") or []) if n]
        row: dict[str, Any] = {"term": term, "geo": it.get("geo"), "traffic": it.get("approx_traffic")}
        out["report"].append(row)
        blocked = screen_trend(term, news, s)
        if blocked:
            row.update(decision="descartado", reason=blocked)
            continue
        theme = match_theme(term)
        row["reason"] = "palavra-chave do tema" if theme else "sem relação com serviços"
        if theme is None and use_groq:
            try:
                theme, row["reason"] = groq_theme(groq, term, news)
            except CaptionGenerationError as e:
                row["reason"] = f"sem relação por palavras; Groq indisponível ({e})"
        if theme is None:
            row["decision"] = "descartado"
            continue
        row["theme"] = theme
        if _recent(db, Post.trend_term, term, 7):
            row.update(decision="descartado", reason="assunto já usado nos últimos 7 dias")
            continue
        if _recent(db, Post.theme_key, f"trend:{theme}", 3):
            row.update(decision="descartado", reason=f"tema '{theme}' já usado nos últimos 3 dias")
            continue
        if not create:
            row["decision"] = "aproveitável"
            continue
        if len(out["created"]) >= quota:
            row.update(decision="aproveitável", reason="limite diário atingido nesta rodada")
            continue
        t = TREND_THEMES[theme]
        photo, log = find_photo(db, t["query_en"], t["query_pt"], s, client)
        row["photo_search"] = log
        if photo is None:
            row.update(decision="descartado", reason="nenhuma foto com licença adequada encontrada")
            continue
        if s.video_enabled and s.pixabay_configured:
            try:
                from app.services.video_service import VideoSearchError, create_video, public

                video = create_video(db, theme, term, t["query_pt"], s, client=client, render=True)
                row["video"] = public(video)
            except VideoSearchError as e:
                row["video_error"] = str(e)
        n_trend = db.scalar(select(func.count()).select_from(Post).where(
            Post.content_kind == ContentKind.TREND.value)) or 0
        template = PHOTO_TEMPLATES[n_trend % len(PHOTO_TEMPLATES)]  # alterna para comparar os dois modelos
        audience = t["audience"]
        title, subtitle = t["title"], SUBTITLES[audience]
        geo = "Goiás" if it.get("geo") == "BR-GO" else "Brasil"
        cs = content or ContentService(s)
        try:
            post = cs.create_post(db, PostInput(
                template_key=template, overrides={"title": title, "subtitle": subtitle, "tag": "Assunto do momento"},
                topic=f"{term} → {t['label']}", audience=audience,
                objective=f"aproveitar o assunto em alta '{term}' para lembrar de {t['label']} e apresentar o Doova",
                theme_key=f"trend:{theme}", campaign_slug="assuntos-do-momento", content_kind=ContentKind.TREND.value,
                message_type="pergunta", photo_id=photo.id, trend_term=term[:200],
                expires_in_hours=s.trend_post_ttl_hours, generate_caption=True,
                alt_text=f"Foto ilustrativa de banco de imagens com o texto: {title} {subtitle}",
                extra_context=(f"Assunto em alta no Google Trends ({geo}): '{term}'. Use o assunto só como gancho "
                               f"para lembrar de {t['label']}. Não afirme nada sobre o assunto além do nome dele, "
                               "não cite notícias, números, previsões do tempo nem dados."),
                extra_review_notes=[] if s.autonomous else [f"assunto do momento '{term}' ({geo}): confira se continua pertinente e "
                                    "se não ganhou um lado sensível"]))
        except ContentError as e:
            row.update(decision="erro", reason=str(e))
            continue
        row.update(decision="publicação criada", post_id=post.id, template=template, photo_id=photo.id)
        out["created"].append(post.id)
    return out


EXPIRABLE = {PostStatus.DRAFT.value, PostStatus.CAPTION_FAILED.value, PostStatus.NEEDS_REVIEW.value,
             PostStatus.PENDING_APPROVAL.value, PostStatus.APPROVED.value, PostStatus.SCHEDULED.value}


def expire_trend_posts(db: Session) -> int:
    """Cancela publicações de assunto do momento que passaram da validade sem serem publicadas."""
    posts = db.scalars(select(Post).where(Post.expires_at.is_not(None), Post.expires_at <= utcnow(),
                                          Post.status.in_(EXPIRABLE))).all()
    for p in posts:
        set_status(db, p, PostStatus.CANCELLED, "assunto do momento expirou antes da publicação")
    if posts:
        db.commit()
    return len(posts)


def trend_recheck(db: Session, post: Post, settings: Settings | None = None) -> str | None:
    """Confere de novo o assunto logo antes de publicar, com as notícias mais recentes do Trends.

    Substitui a pergunta "o assunto ganhou um lado sensível?" da revisão humana.
    """
    s = settings or get_settings()
    term = post.trend_term or ""
    news: list[str] = []
    try:
        from app.services.trends_service import cached_trends

        for it in cached_trends(db, s, max_age_hours=3).get("items", []):
            if norm(it.get("term") or "") == norm(term):
                news += [n for n in (it.get("news") or []) if n]
    except Exception as e:  # sem rede: confere só o termo
        logger.warning("não foi possível atualizar o Google Trends para reconferir '%s': %s", term, e)
    return screen_trend(term, news, s)

