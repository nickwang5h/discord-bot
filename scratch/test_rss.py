import feedparser
import time

feeds = [
    ("Tech", "https://feeds.arstechnica.com/arstechnica/index"),
    ("Tech", "https://techcrunch.com/feed/"),
    ("World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Finance", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("Tech", "https://www.theverge.com/rss/index.xml"),
    ("Tech", "https://www.wired.com/feed/rss"),
    ("World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ("Finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
    ("Science", "https://www.nature.com/nature.rss"),
    ("AI", "https://openai.com/blog/rss.xml")
]

for cat, url in feeds:
    start = time.time()
    try:
        f = feedparser.parse(url)
        if f.entries:
            print(f"[OK] {cat} - {url} ({len(f.entries)} entries) in {time.time()-start:.2f}s")
        else:
            print(f"[FAIL] {cat} - {url} (No entries) in {time.time()-start:.2f}s")
    except Exception as e:
        print(f"[ERR] {cat} - {url}: {e}")
