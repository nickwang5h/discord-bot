from core.feeds import FeedSource, fetch_feed, fetch_feeds

SOURCES = [
    FeedSource("Tech", "https://feeds.arstechnica.com/arstechnica/index", "Ars Technica"),
    FeedSource("Tech", "https://techcrunch.com/feed/", "TechCrunch"),
    FeedSource("Tech", "https://www.theverge.com/rss/index.xml", "The Verge"),
    FeedSource("Tech", "https://www.wired.com/feed/rss", "Wired"),
    FeedSource("World", "https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World"),
    FeedSource("World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "NYT World"),
    FeedSource("Finance", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "WSJ Markets"),
    FeedSource("Finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664", "CNBC Finance"),
    FeedSource("Science", "https://www.nature.com/nature.rss", "Nature"),
    FeedSource("AI", "https://openai.com/blog/rss.xml", "OpenAI"),
]


def _as_dict(item) -> dict:
    return {
        "title": item.title,
        "url": item.url,
        "content": item.summary,
        "source": item.category,
    }


async def fetch_rss(url: str, category: str, max_age_seconds: int = 86400, max_items: int = 5):
    """Compatibility wrapper for callers that fetch one RSS source."""
    items = await fetch_feed(
        FeedSource(category, url),
        max_age_seconds=max_age_seconds,
        max_items=max_items,
    )
    return [_as_dict(item) for item in items]


async def fetch_obsidian_notes() -> list[dict]:
    """Placeholder for future local-note ingestion."""
    return []


async def fetch_all_sources() -> list[dict]:
    items = await fetch_feeds(SOURCES, max_age_seconds=86400, max_items_per_source=4)
    result = [_as_dict(item) for item in items]
    result.extend(await fetch_obsidian_notes())
    return result
