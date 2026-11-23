import asyncio
import calendar
import logging
import re
import time
from dataclasses import dataclass

import aiohttp
import feedparser

from core.web_fetcher import _validate_public_url, read_capped

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {"User-Agent": "DiscordDigestBot/1.0 (+RSS reader)"}
MAX_FEED_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FeedSource:
    category: str
    url: str
    name: str = ""
    # Personal sources are fetched the way /source_add probed them: no redirects, and an
    # owner-typed address must resolve to a public host (RSSHub is the deployment's own).
    follow_redirects: bool = True
    public_host_only: bool = False


@dataclass(frozen=True, slots=True)
class FeedItem:
    category: str
    source_name: str
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
                source_name=source.name or source.category,
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
        if source.public_host_only:
            await _validate_public_url(source.url)
        async with session.get(source.url, allow_redirects=source.follow_redirects) as response:
            if not source.follow_redirects and 300 <= response.status < 400:
                raise RuntimeError(f"HTTP {response.status}：个人信源不跟随重定向")
            response.raise_for_status()
            content = await read_capped(response, MAX_FEED_BYTES)
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
            # Private feeds carry an access key in the query string.
            detail = re.sub(r"key=[^&\s'\"]+", "key=***", f"{source.url}: {result}")
            logger.warning("抓取 RSS 失败 [%s] %s", source.category, detail)
            continue
        items.extend(result)
    return items
