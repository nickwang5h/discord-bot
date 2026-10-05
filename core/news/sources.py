"""Explicit RSS groups; adding an existing source type is configuration only."""
from config import get_env
from core.feeds import FeedSource


def _rsshub(path, category, name):
    """A feed from the private RSSHub instance, present only when the runtime names it."""
    base, key = get_env('RSSHUB_URL'), get_env('RSSHUB_ACCESS_KEY')
    if not base or not key or '{uid}' in path and not get_env('BILIBILI_UID'):
        return ()
    path = path.format(uid=get_env('BILIBILI_UID'))
    return (FeedSource(category, f"{base.rstrip('/')}{path}?key={key}", name),)


GENERAL = (
    FeedSource('World', 'https://feeds.bbci.co.uk/news/world/rss.xml', 'BBC World'),
    FeedSource('World', 'https://feeds.npr.org/1004/rss.xml', 'NPR World'),
    FeedSource('World', 'https://www.aljazeera.com/xml/rss/all.xml', 'Al Jazeera'),
    FeedSource('Canada', 'https://globalnews.ca/canada/feed/', 'Global News'),
    FeedSource('Finance', 'https://feeds.a.dj.com/rss/RSSMarketsMain.xml', 'WSJ Markets'),
    FeedSource('Finance', 'https://search.cnbc.com/rs/search/combinedcms/view.xml?profile=120000000&id=100003114', 'CNBC'),
    FeedSource('Finance', 'https://finance.yahoo.com/news/rss', 'Yahoo Finance'),
    FeedSource('Tech', 'https://feeds.arstechnica.com/arstechnica/index', 'Ars Technica'),
    FeedSource('Tech', 'https://techcrunch.com/feed/', 'TechCrunch'),
)
DISCOVERY = (
    GENERAL[7], GENERAL[8],
    FeedSource('Tech', 'https://www.theverge.com/rss/index.xml', 'The Verge'),
    FeedSource('Tech', 'https://www.wired.com/feed/rss', 'Wired'),
    GENERAL[0],
    FeedSource('World', 'https://rss.nytimes.com/services/xml/rss/nyt/World.xml', 'NYT World'),
    GENERAL[4],
    FeedSource('Finance', 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664', 'CNBC Finance'),
    FeedSource('Science', 'https://www.nature.com/nature.rss', 'Nature'),
    FeedSource('AI', 'https://openai.com/blog/rss.xml', 'OpenAI'),
)
# Appended after the fixed lists so the positional references above stay valid.
GENERAL += _rsshub('/telegram/channel/cnwallstreet', 'Finance', '华尔街见闻')
DISCOVERY += _rsshub('/bilibili/followings/video/{uid}', 'Video', 'B站关注')
GROUPS = {'general': GENERAL, 'discovery': DISCOVERY}


def sources_for(groups):
    # The same RSS endpoint is fetched once even when multiple topics subscribe.
    return list({source.url: source for group in groups for source in GROUPS[group]}.values())
