import logging
import time
from typing import Any

from config import STATE_ROOT
from core.storage import JsonStore

logger = logging.getLogger(__name__)

CACHE_FILE = STATE_ROOT / "data" / "news_cache.json"
MAX_CACHE_SIZE = 150
_cache_store = JsonStore(CACHE_FILE, list)
CURRENT_SCORE_FIELDS = frozenset(
    {
        "relevance_score",
        "novelty_score",
        "quality_score",
        "llm_interestingness",
        "cross_domain_bridge",
        "discovery_score",
    }
)


def load_cache() -> list[dict[str, Any]]:
    data = _cache_store.read()
    return data if isinstance(data, list) else []


def save_cache(data: list[dict[str, Any]]) -> None:
    _cache_store.write(data)


def _is_current_item(item: object) -> bool:
    if not isinstance(item, dict) or not item.get("title") or not item.get("url"):
        return False
    if not CURRENT_SCORE_FIELDS.issubset(item):
        return False
    try:
        return all(0.0 <= float(item[field]) <= 1.0 for field in CURRENT_SCORE_FIELDS)
    except (TypeError, ValueError):
        return False


def prune_legacy_items() -> int:
    """Remove cache entries that cannot participate in the current scoring flow."""
    removed = 0

    def update(cache: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal removed
        current = [item for item in cache if _is_current_item(item)]
        removed = len(cache) - len(current)
        return current

    _cache_store.update(update)
    return removed


def add_items(new_items: list[dict[str, Any]]) -> int:
    """Append unseen items and retain the newest MAX_CACHE_SIZE entries."""
    added = 0

    def update(cache: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal added
        existing_urls = {item.get("url") for item in cache if item.get("url")}
        existing_titles = {item.get("title") for item in cache if item.get("title")}

        for source_item in new_items:
            item = dict(source_item)
            url = item.get("url")
            title = item.get("title")
            if (url and url in existing_urls) or (title and title in existing_titles):
                continue
            item["timestamp"] = time.time()
            cache.append(item)
            if url:
                existing_urls.add(url)
            if title:
                existing_titles.add(title)
            added += 1

        cache.sort(key=lambda entry: entry.get("timestamp", 0), reverse=True)
        return cache[:MAX_CACHE_SIZE]

    _cache_store.update(update)
    return added


def get_unpushed_items() -> list[dict[str, Any]]:
    return [item for item in load_cache() if not item.get("pushed")]


def mark_as_pushed(urls: list[str]) -> None:
    url_set = set(urls)

    def update(cache: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in cache:
            if item.get("url") in url_set:
                item["pushed"] = True
        return cache

    _cache_store.update(update)


def clear_pushed() -> int:
    removed = 0

    def update(cache: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal removed
        remaining = [item for item in cache if not item.get("pushed")]
        removed = len(cache) - len(remaining)
        return remaining

    _cache_store.update(update)
    logger.info("已从新闻缓存清理 %s 条已推送记录", removed)
    return removed


def filter_new_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter a batch with one cache read instead of one disk read per item."""
    cache = load_cache()
    existing_urls = {item.get("url") for item in cache if item.get("url")}
    existing_titles = {item.get("title") for item in cache if item.get("title")}
    return [
        item
        for item in items
        if item.get("url") not in existing_urls and item.get("title") not in existing_titles
    ]
