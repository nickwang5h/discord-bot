import asyncio
import time

async def fetch_rss(url: str, category: str, max_age_seconds: int = 86400, max_items: int = 5):
    """
    Fetch and parse an RSS feed.
    """
    import feedparser
    try:
        feed = await asyncio.to_thread(feedparser.parse, url)
        current_time = time.time()
        valid_entries = []
        for entry in feed.entries:
            entry_time = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                entry_time = time.mktime(entry.published_parsed)
            elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                entry_time = time.mktime(entry.updated_parsed)
                
            if entry_time is None or (current_time - entry_time) <= max_age_seconds:
                valid_entries.append({
                    "title": entry.title,
                    "url": entry.link,
                    "content": entry.get("summary", ""),
                    "source": category
                })
                
            if len(valid_entries) >= max_items:
                break
        return valid_entries
    except Exception as e:
        print(f"Error fetching RSS {url}: {e}")
        return []

async def fetch_obsidian_notes():
    """
    Placeholder for future Obsidian notes ingestion.
    """
    # In the future, this can read from a local directory or API.
    return []

async def fetch_all_sources() -> list:
    """
    Aggregates data from all configured sources.
    Returns a list of dicts: {"title": str, "url": str, "content": str, "source": str}
    """
    # RSS Sources (can be moved to settings later)
    feeds = [
        ("Tech", "https://feeds.arstechnica.com/arstechnica/index"),
        ("Tech", "https://techcrunch.com/feed/"),
        ("World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Finance", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml") # WSJ
    ]
    
    tasks = [fetch_rss(url, cat) for cat, url in feeds]
    
    # Can append other sources here
    tasks.append(fetch_obsidian_notes())
    
    results = await asyncio.gather(*tasks)
    
    all_items = []
    for r in results:
        all_items.extend(r)
        
    return all_items
