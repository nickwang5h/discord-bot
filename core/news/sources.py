"""Explicit RSS groups. Shared groups are code; personal groups come from the owner's JSON list."""
from core.feeds import FeedSource
from core.news import personal


BBC_WORLD = FeedSource('World', 'https://feeds.bbci.co.uk/news/world/rss.xml', 'BBC World')
ARS_TECHNICA = FeedSource('Tech', 'https://feeds.arstechnica.com/arstechnica/index', 'Ars Technica')
TECHCRUNCH = FeedSource('Tech', 'https://techcrunch.com/feed/', 'TechCrunch')
# CNBC (search.cnbc.com …id=100003114, HTTP 503) and Yahoo Finance (finance.yahoo.com/news/rss,
# HTTP 404) were removed on 2026-10-19 after failing every hourly fetch since at least 10-18.
GENERAL = (
    BBC_WORLD,
    FeedSource('World', 'https://feeds.npr.org/1004/rss.xml', 'NPR World'),
    FeedSource('World', 'https://www.aljazeera.com/xml/rss/all.xml', 'Al Jazeera'),
    FeedSource('Canada', 'https://globalnews.ca/canada/feed/', 'Global News'),
    ARS_TECHNICA,
    TECHCRUNCH,
)
DISCOVERY = (
    ARS_TECHNICA, TECHCRUNCH,
    FeedSource('Tech', 'https://www.theverge.com/rss/index.xml', 'The Verge'),
    FeedSource('Tech', 'https://www.wired.com/feed/rss', 'Wired'),
    BBC_WORLD,
    FeedSource('World', 'https://rss.nytimes.com/services/xml/rss/nyt/World.xml', 'NYT World'),
    FeedSource('Finance', 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664', 'CNBC Finance'),
    FeedSource('Science', 'https://www.nature.com/nature.rss', 'Nature'),
    FeedSource('AI', 'https://openai.com/blog/rss.xml', 'OpenAI'),
)
# Shared with friends. Personal sources never join these groups.
SHARED_GROUPS = {'general': GENERAL, 'discovery': DISCOVERY}
GROUP_NAMES = (*SHARED_GROUPS, *personal.PERSONAL_GROUPS)
SHARED_NAMES = frozenset(source.name for group in SHARED_GROUPS.values() for source in group)


def personal_entries():
    """Valid personal entries and errors, re-read from disk on every call."""
    return personal.load(SHARED_NAMES)


def group_sources(group):
    if group in SHARED_GROUPS:
        return SHARED_GROUPS[group]
    if group not in personal.PERSONAL_GROUPS:
        raise KeyError(group)
    entries, _ = personal_entries()
    return tuple(source for entry in entries if (source := personal.feed_source(entry)) is not None)


def sources_for(groups):
    # The same RSS endpoint is fetched once even when multiple topics subscribe.
    return list({source.url: source for group in groups for source in group_sources(group)}.values())
