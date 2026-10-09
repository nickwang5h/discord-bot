"""Collect original evidence independently of every topic's model budget."""
import asyncio
import html
import math
import time
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit

from core.feeds import fetch_feeds
from core.news.models import Article, fingerprint
from core.news.sources import GROUP_NAMES, sources_for

RAW_WINDOW = 3 * 86400
MAX_PER_SOURCE = 40


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def clean_text(value, limit=4000):
    parser = _Text()
    parser.feed(str(value or '')[:20000])
    return ' '.join(html.unescape(' '.join(parser.parts)).split())[:limit]


def source_url(value):
    if not isinstance(value, str) or not value or len(value) > 280:
        return None
    try:
        parts = urlsplit(value)
        _ = parts.port
        if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
                or parts.password is not None or any(c.isspace() or ord(c) < 32 for c in value)):
            return None
    except ValueError:
        return None
    return quote(value, safe="/:?#[]@!$&'*+,;=%-._~")


def normalize(item, now):
    url = source_url(item.url)
    title, content = clean_text(item.title, 300), clean_text(item.summary)
    published = item.published_at
    if (published is not None and (type(published) not in (int, float)
            or not math.isfinite(published) or not now - RAW_WINDOW <= published <= now + 3600)):
        return None
    if not url or not title:
        return None
    return Article(fingerprint([item.source_name, url]), item.source_name, url, title,
                   content, item.category, published, now,
                   fingerprint([title.casefold(), content.casefold()]))


class Ingester:
    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()
        self.last_fetch = None

    async def collect(self, *, force=False):
        async with self.lock:
            now = time.time()
            if not force and self.last_fetch is not None and now - self.last_fetch < 3600:
                return 0
            items = await fetch_feeds(sources_for(GROUP_NAMES), max_age_seconds=RAW_WINDOW,
                                     max_items_per_source=MAX_PER_SOURCE)
            articles = [article for item in items if (article := normalize(item, now))]
            count = self.store.upsert_articles(articles, now=now)
            # Empty/failing feeds do not prevent a subsequent bounded collection attempt.
            if articles:
                self.last_fetch = now
            return count
