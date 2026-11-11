"""Text helpers: normalisation, hashing, Jaccard similarity and concept tags.

Similarity and concept tags are ports of OpenClaw's ``memory/tokenize.ts`` and
``extensions/memory-core/src/concept-vocabulary.ts`` (MIT, see NOTICE). Python's ``re``
has no ``\\p{L}`` classes, so letters/digits are matched with ``[^\\W_]`` (Unicode-aware).
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from typing import Iterable

MAX_CONCEPT_TAGS = 8
_MAX_TOKEN_LEN = 32

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]+")
_KANA_RE = re.compile(r"^[\u3040-\u30ff]+$")
_WORD_RE = re.compile(r"[^\W_]\w*", re.UNICODE)
_ALNUM_RE = re.compile(r"[^\W_]", re.UNICODE)
_EDGE_RE = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)
_COMPOUND_RE = re.compile(r"[^\W_]+(?:[._/-][^\W_]+)+", re.UNICODE)
_SEGMENT_RE = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*", re.UNICODE)
_NUMERIC_RE = re.compile(r"^\d+(?:[./-]\d+)*$")
_DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.[^\W_]+$")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

# OpenClaw stop words (concept-vocabulary.ts:11-218), verbatim.
_OPENCLAW_STOP = """
about after agent again also assistant because before being between build called could daily default deploy during every
file files from have into just line lines long main make memory month more most move much next note notes over part past
port same score search session sessions short should since some subagent system than that their there these they this
through today user using with work workspace year and are for its our the then were you your
al con como de del el en es la las los para por que se sin su sus una uno unos unas y
au aux avec dans des du est et le les ou pour qui sans ses son sur une un
auf aus bei das dem den der die ein eine einem einen einer für ich im in mit nach oder ohne über und von zu zum zur
が から する して した で と に の は へ まで も や を 与 为 了 及 和 在 将 或 把 是 用 的
과 는 도 로 를 에 에서 와 은 으로 을 이 하다 한 할 해 했다
cjs cpp cts jsx json md mjs mts text toml ts tsx txt yaml yml
""".split()

# Addition (not in OpenClaw): Portuguese, which OpenClaw's list does not cover.
_PT_STOP = """
uma umas uns ao aos às pelo pela pelos pelas num numa nos nas dos das isso isto esse essa esses essas este esta estes
estas aquele aquela aqui ali onde quando porque pois mas mais menos muito muita muitos muitas tem têm ter tenho
temos foi foram ser são era eram está estão estou estava vai vou vamos fazer feito faz fiz pode podem posso
também ainda já só sobre entre até depois antes sempre nunca agora então assim cada todo toda todos todas
coisa coisas você vocês ele ela eles elas nós meu minha meus minhas seu sua seus suas dele dela deles delas
não sim com sem para por que qual quais quem como mesmo mesma outro outra outros outras bem bom boa
fim dia dias vez vezes hoje ontem amanhã aí lá precisa precisamos quero queria acho deve devem
""".split()

STOP_WORDS = frozenset(_OPENCLAW_STOP) | frozenset(_PT_STOP)

# OpenClaw protected glossary (concept-vocabulary.ts:220-262), verbatim. Configurable via
# the HERMES_DREAMING_GLOSSARY env var (comma separated) or settings.
OPENCLAW_GLOSSARY = (
    "backup backups embedding embeddings failover gateway glacier gpt kv network openai router s3 vlan "
    "sauvegarde routeur passerelle konfiguration sicherung überwachung configuración respaldo enrutador "
    "puerta-de-enlace バックアップ フェイルオーバー ルーター ネットワーク ゲートウェイ 障害対応 路由器 备份 "
    "故障转移 网络 网关 라우터 백업 페일오버 네트워크 게이트웨이 장애대응"
).split()


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_claim(text: str) -> str:
    """Comparable form of a fact: NFKC, lowercase, punctuation-light, collapsed whitespace."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = re.sub(r"^[\s\-*•·]+", "", t)
    t = re.sub(r"[\"'`“”‘’]", "", t)
    return normalize_ws(t).rstrip(".;:!")


def sha1_12(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def claim_key(text: str) -> str:
    return "claim:" + sha1_12(normalize_claim(text))


def clip(text: str, limit: int) -> str:
    """Truncate at a sentence break past 55% or a word break past 65% (OpenClaw
    ``truncatePromotedSnippet``, short-term-promotion-utils.ts:90-121)."""
    text = normalize_ws(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for mark in (". ", "! ", "? ", "; "):
        idx = cut.rfind(mark)
        if idx >= int(limit * 0.55):
            return cut[: idx + 1].rstrip()
    idx = cut.rfind(" ")
    if idx >= int(limit * 0.65):
        return cut[:idx].rstrip() + "..."
    return cut.rstrip() + "..."


# --- similarity (port of memory/tokenize.ts) ---

def tokenize(text: str) -> set[str]:
    t = unicodedata.normalize("NFC", text or "").lower()
    tokens: set[str] = set()
    for run in _CJK_RE.findall(t):
        chars = list(run)
        tokens.update(a + b for a, b in zip(chars, chars[1:]))
        tokens.update(chars)
    t = _CJK_RE.sub(" ", t)
    tokens.update(_WORD_RE.findall(t))
    return tokens


def jaccard(a: str, b: str) -> float:
    ta, tb = tokenize(a), tokenize(b)
    if not ta and not tb:
        return 1.0 if normalize_ws(a) == normalize_ws(b) else 0.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# --- concept tags (port of concept-vocabulary.ts) ---

def _glossary() -> list[str]:
    raw = os.environ.get("HERMES_DREAMING_GLOSSARY")
    if raw is None:
        return [str(g) for g in OPENCLAW_GLOSSARY]
    return [g.strip().lower() for g in raw.split(",") if g.strip()]


def _script_min_len(token: str) -> int:
    return 2 if _CJK_RE.search(token) else 3


def normalize_concept_token(raw: str, glossary: Iterable[str] = ()) -> str | None:
    t = unicodedata.normalize("NFKC", raw or "")
    t = _EDGE_RE.sub("", t).replace("_", "-").lower()
    if not t or not _ALNUM_RE.search(t) or len(t) > _MAX_TOKEN_LEN:
        return None
    if _NUMERIC_RE.match(t) or _DATE_FILE_RE.match(t):
        return None
    gl = set(glossary)
    if len(t) < _script_min_len(t) and t not in gl:
        return None
    if _KANA_RE.match(t) and len(t) < 3:
        return None
    if t in STOP_WORDS:
        return None
    return t


def _glossary_matches(source: str, glossary: list[str]) -> list[str]:
    low = source.lower()
    hits = []
    for entry in glossary:
        if len(entry) < _script_min_len(entry):
            if re.search(r"(?<![^\W_])" + re.escape(entry) + r"(?![^\W_])", low):
                hits.append(entry)
        elif entry in low:
            hits.append(entry)
    return hits


def concept_tags(text: str, source_name: str = "", glossary: list[str] | None = None) -> list[str]:
    gl = _glossary() if glossary is None else glossary
    visible = _COMMENT_RE.sub(" ", text or "")
    source = f"{source_name} {visible}".strip()
    tags: list[str] = []
    for group in (_glossary_matches(source, gl), _COMPOUND_RE.findall(source), _SEGMENT_RE.findall(source)):
        for raw in group:
            tok = normalize_concept_token(raw, gl)
            if tok and tok not in tags:
                tags.append(tok)
                if len(tags) >= MAX_CONCEPT_TAGS:
                    return tags
    return tags


_NUMBERISH_RE = re.compile(r"(?:[~/.]?[\w.-]*/[\w./-]+|\b\d[\d.,:]*\b|https?://\S+)")


def novel_specifics(result: str, sources: Iterable[str]) -> list[str]:
    """Numbers, paths and URLs in ``result`` that appear in none of ``sources``: a cheap
    guard against a model inventing specifics while rewriting a memory entry."""
    pool = " ".join(sources)
    return [tok for tok in _NUMBERISH_RE.findall(result or "") if tok.strip(".,") and tok.strip(".,") not in pool]
