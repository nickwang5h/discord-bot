"""Bounded news contracts. Topic-specific fields live in validated result dictionaries."""
import hashlib
import json
from dataclasses import dataclass, field


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Article:
    id: str
    source: str
    url: str
    title: str
    content: str
    category: str
    published_at: float | None
    first_seen: float
    version: str


@dataclass(frozen=True)
class Subscription:
    id: str
    topic: str
    source_groups: tuple[str, ...]
    times: tuple[str, ...]
    channel_id: int | None
    params: dict = field(default_factory=dict)
    max_candidates: int = 40
    max_output_tokens: int = 3000
    enabled: bool = True


@dataclass
class SelectionInput:
    candidates: list[dict]
    data: dict
    system: str


@dataclass
class Edition:
    embeds: list
    selected: list[dict]


def bind_evidence(candidates, articles):
    by_url = {article.url: article for article in articles}
    for candidate in candidates:
        article = by_url[candidate['url']]
        candidate['_article_id'] = article.id
        candidate['_version'] = article.version
        candidate['_evidence'] = {
            'url': article.url, 'title': article.title, 'content': article.content,
            'rss_summary': candidate.get('rss_summary', article.content[:600]),
            'publisher': article.source, 'category': article.category,
            'published_at': article.published_at, 'evidence_hash': article.version,
        }
    return candidates


MAX_HISTORY_ITEMS = 30
MAX_HISTORY_SUMMARY_CHARS = 100


def public_history(history, limit=MAX_HISTORY_ITEMS):
    """Minimal anti-repeat context for the model: newest `limit` deliveries, each only the
    original title plus at most 100 characters of raw RSS summary (title alone when none).

    History is comparison context, never a candidate's factual evidence; validators and the
    “新进展：” rule use the raw history's URLs, not this. Model results are never included.
    """
    ordered = sorted(history, key=lambda item: -float(item.get('delivered_at') or 0))
    result = []
    for item in ordered:
        title = ' '.join(str(item.get('title') or '').split())
        if not title:
            continue
        if len(result) == limit:
            break
        summary = ' '.join(str(item.get('rss_summary') or item.get('content') or '').split())
        entry = {'title': title}
        if summary:
            entry['summary'] = summary[:MAX_HISTORY_SUMMARY_CHARS]
        result.append(entry)
    return result


def public_candidate(candidate):
    return {key: value for key, value in candidate.items()
            if not key.startswith('_') and key not in {'url', 'cache_url', 'evidence_hash'}}


def trim_candidates(prepared, count):
    """The first `count` candidates in prepared (model) order, with their public entries."""
    data = {**prepared.data, 'candidates': prepared.data['candidates'][:count]}
    return SelectionInput(prepared.candidates[:count], data, prepared.system)


def lenient_items(text, *, label, max_items, required, optional=None):
    """Tolerate what free models get deterministically wrong, nothing factual (arch §6).

    Unknown keys (top level and per item) are ignored; a missing, null or blank optional
    field becomes its default; a list longer than `max_items` is cut in model order. A
    missing required field, a non-object item or a non-list `items` stays invalid, and every
    topic keeps its own id, evidence, link and length checks on what remains.
    """
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
        raise ValueError(f'{label}输出结构无效')
    optional = optional or {}
    result = []
    for item in payload['items'][:max_items]:
        if not isinstance(item, dict) or not set(required) <= set(item):
            raise ValueError(f'{label}条目字段无效')
        clean = {key: item[key] for key in required}
        for key, default in optional.items():
            value = item.get(key)
            missing = value is None or (isinstance(value, str) and not value.strip())
            clean[key] = (list(default) if isinstance(default, list) else default) if missing else value
        result.append(clean)
    return result
