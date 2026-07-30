import asyncio
import calendar
import logging
import time
from dataclasses import dataclass

import aiohttp
import feedparser

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {"User-Agent": "DiscordDigestBot/1.0 (+RSS reader)"}
MAX_FEED_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FeedSource:
    category: str
    url: str
    name: str = ""


@dataclass(frozen=True, slots=True)
class FeedItem:
    category: str
    title: str
    url: str
    summary: str
    published_at: float | None


def _entry_timestamp(entry) -> float | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return float(calendar.timegm(parsed)) if parsed else None


def _parse_feed(
    content: bytes,
    source: FeedSource,
    *,
    max_age_seconds: int | None,
    max_items: int,
) -> list[FeedItem]:
    feed = feedparser.parse(content)
    if getattr(feed, "bozo", False) and not feed.entries:
        raise ValueError(f"RSS 解析失败: {getattr(feed, 'bozo_exception', 'unknown error')}")

    now = time.time()
    items: list[FeedItem] = []
    for entry in feed.entries:
        title = str(entry.get("title", "")).strip()
        url = str(entry.get("link", "")).strip()
        if not title or not url:
            continue

        published_at = _entry_timestamp(entry)
        if max_age_seconds is not None and published_at is not None:
            if now - published_at > max_age_seconds:
                continue

        items.append(
            FeedItem(
                category=source.category,
                title=title,
                url=url,
                summary=str(entry.get("summary", "")).strip(),
                published_at=published_at,
            )
        )
        if len(items) >= max_items:
            break
    return items


async def fetch_feed(
    source: FeedSource,
    *,
    max_age_seconds: int | None = 86400,
    max_items: int = 5,
    session: aiohttp.ClientSession | None = None,
) -> list[FeedItem]:
    """Fetch one feed with a bounded HTTP timeout, then parse it off the event loop."""
    owns_session = session is None
    if session is None:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers=DEFAULT_HEADERS,
        )

    try:
        async with session.get(source.url) as response:
            response.raise_for_status()
            content = await response.content.read(MAX_FEED_BYTES + 1)
            if len(content) > MAX_FEED_BYTES:
                raise RuntimeError("RSS 内容超过 5 MB 限制")
        return await asyncio.to_thread(
            _parse_feed,
            content,
            source,
            max_age_seconds=max_age_seconds,
            max_items=max_items,
        )
    finally:
        if owns_session:
            await session.close()


async def fetch_feeds(
    sources: list[FeedSource],
    *,
    max_age_seconds: int | None = 86400,
    max_items_per_source: int = 5,
) -> list[FeedItem]:
    """Fetch feeds concurrently; one broken source does not discard healthy sources."""
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers=DEFAULT_HEADERS) as session:
        results = await asyncio.gather(
            *(
                fetch_feed(
                    source,
                    max_age_seconds=max_age_seconds,
                    max_items=max_items_per_source,
                    session=session,
                )
                for source in sources
            ),
            return_exceptions=True,
        )

    items: list[FeedItem] = []
    for source, result in zip(sources, results):
        if isinstance(result, BaseException):
            logger.warning("抓取 RSS 失败 [%s] %s: %s", source.category, source.url, result)
            continue
        items.extend(result)
    return items
