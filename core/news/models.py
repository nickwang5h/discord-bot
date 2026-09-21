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


def public_history(history):
    """History is comparison context, never a candidate's factual evidence."""
    result, size = [], 0
    for item in history:
        entry = {key: value[:600] if isinstance(value, str) else value
                 for key, value in item.items()
                 if key in {'title', 'content', 'rss_summary', 'publisher', 'category', 'delivered_at'}}
        entry['previous_result'] = {key: value for key, value in item.get('topic_result', {}).items()
                                    if key in {'project', 'country', 'stage', 'event_date', 'new_fact'}}
        size += len(json.dumps(entry, ensure_ascii=False))
        if size > 12000:
            break
        result.append(entry)
    return result


def public_candidate(candidate):
    return {key: value for key, value in candidate.items()
            if not key.startswith('_') and key not in {'url', 'cache_url', 'evidence_hash'}}
