import asyncio
import feedparser

async def test_rss():
    feeds = [
        ("World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Canada", "https://globalnews.ca/canada/feed/"),
        ("Finance", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
        ("Finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml?profile=120000000&id=100003114"),
        ("Finance", "https://finance.yahoo.com/news/rss")
    ]
    
    news_items = []
    import time
    current_time = time.time()
    
    async def fetch_feed(category, url):
        try:
            print(f"开始抓取 {url} ...")
            feed = await asyncio.to_thread(feedparser.parse, url)
            valid_entries = []
            for entry in feed.entries:
                entry_time = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    entry_time = time.mktime(entry.published_parsed)
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    entry_time = time.mktime(entry.updated_parsed)
                
                if entry_time is None or (current_time - entry_time) <= 86400:
                    valid_entries.append(f"[{category}] - {entry.title} ({entry.link})")
                    
                if len(valid_entries) >= 12:
                    break
            print(f"成功抓取 {category} : {len(valid_entries)} 条新闻")
            return valid_entries
        except Exception as e:
            print(f"抓取 {category} 失败: {e}")
            return []

    tasks_list = [fetch_feed(cat, url) for cat, url in feeds]
    results = await asyncio.gather(*tasks_list)
    
    for items in results:
        news_items.extend(items)
        
    print("\n--- 抓取到的完整列表示例 ---")
    for item in news_items[:5]:
        print(item)
    print("...")
    for item in news_items[-5:]:
        print(item)
    print(f"\n总计抓取: {len(news_items)} 条")

if __name__ == "__main__":
    asyncio.run(test_rss())
