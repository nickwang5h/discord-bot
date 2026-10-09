"""Text forms shared by the FTS index and its queries.

SQLite's ``unicode61`` tokenizer keeps a run of Han/Kana/Hangul characters as one
token, and ``trigram`` cannot use the index for two-character words such as
储能 or 关税. Both the stored text and every query therefore go through
``index_form()``, which splits each CJK run into overlapping two-character tokens
("储能电站" -> "储能 能电 电站") and leaves other scripts to ``unicode61``.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

# Bump when index_form() changes; the store then rebuilds the FTS table.
INDEX_VERSION = "bigram-1"

_CJK_RANGES = (
    "ᄀ-ᇿ"  # Hangul Jamo
    "々〇"  # 々 〇
    "぀-ヿ"  # Hiragana, Katakana
    "㄰-㆏"  # Hangul compatibility Jamo
    "ㇰ-ㇿ"  # Katakana phonetic extensions
    "㐀-䶿"  # CJK Extension A
    "一-鿿"  # CJK Unified Ideographs
    "가-힯"  # Hangul syllables
    "豈-﫿"  # CJK compatibility ideographs
    "\U00020000-\U0003134f"  # CJK Extensions B-G
)
_CJK_RUN = re.compile(f"[{_CJK_RANGES}]+")
_CJK_CHAR = re.compile(f"[{_CJK_RANGES}]")
# What unicode61 keeps as token characters, approximately: letters and digits.
_TOKEN = re.compile(r"[^\W_]+")
_SPACE = re.compile(r"\s+")

MAX_QUERY_TERMS = 16
MAX_TERM_CHARS = 80


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def _bigrams(match: re.Match[str]) -> str:
    run = match.group(0)
    if len(run) == 1:
        return f" {run} "
    return " " + " ".join(run[i:i + 2] for i in range(len(run) - 1)) + " "


def index_form(text: str) -> str:
    """NFKC + casefold, CJK runs as overlapping bigrams; used for documents and queries."""
    return _SPACE.sub(" ", _CJK_RUN.sub(_bigrams, normalize(text))).strip()


def query_terms(text: str, *, limit: int = MAX_QUERY_TERMS) -> list[str]:
    """Whitespace-separated terms of a free-text query, normalized and de-duplicated."""
    terms: list[str] = []
    for raw in normalize(text).split():
        term = raw[:MAX_TERM_CHARS]
        if _TOKEN.search(term) and term not in terms:
            terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def _phrase(term: str) -> str | None:
    tokens = _TOKEN.findall(index_form(term))
    if not tokens:
        return None
    # Tokens contain only letters/digits, so FTS operators (AND, NEAR, *, ^, :, (, "...)
    # can only reach SQLite as quoted phrase text. Doubling quotes is a second guard.
    phrase = '"' + " ".join(tokens).replace('"', '""') + '"'
    if len(tokens) == 1 and len(tokens[0]) == 1 and _CJK_CHAR.fullmatch(tokens[0]):
        # A lone CJK character is only indexed as the first half of a bigram.
        phrase += " *"
    return phrase


def fts_query(terms: Iterable[str] | str, *, mode: str = "or") -> str:
    """Safe FTS5 MATCH expression: each term becomes one quoted phrase.

    Returns "" when nothing searchable remains; callers must not run MATCH with it.
    """
    if mode not in {"or", "and"}:
        raise ValueError(f"unknown mode: {mode}")
    if isinstance(terms, str):
        terms = query_terms(terms)
    phrases: list[str] = []
    for term in terms:
        phrase = _phrase(str(term)[:MAX_TERM_CHARS])
        if phrase and phrase not in phrases:
            phrases.append(phrase)
        if len(phrases) >= MAX_QUERY_TERMS:
            break
    return f" {mode.upper()} ".join(phrases)


def passage(text: str, terms: Iterable[str] = (), *, limit: int = 600) -> str:
    """A window of at most ``limit`` characters around the first matching term."""
    flat = _SPACE.sub(" ", unicodedata.normalize("NFKC", text or "")).strip()
    if len(flat) <= limit:
        return flat
    first = None
    for term in terms:
        term = unicodedata.normalize("NFKC", str(term)).strip()
        if not term:
            continue
        match = re.search(re.escape(term), flat, re.IGNORECASE)
        if match and (first is None or match.start() < first):
            first = match.start()
    start = 0 if first is None else max(0, min(first - limit // 4, len(flat) - limit))
    window = flat[start:start + limit]
    if start > 0:
        window = "…" + window[1:]
    if start + limit < len(flat):
        window = window[:-1] + "…"
    return window
