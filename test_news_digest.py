import asyncio
import feedparser

async def test_rss():
    feeds = {
        "World": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "Canada": "https://globalnews.ca/canada/feed/",
        "Finance": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"
    }
    
    news_items = []
    
    async def fetch_feed(category, url):
        try:
            print(f"开始抓取 {category} ...")
            feed = await asyncio.to_thread(feedparser.parse, url)
            entries = [f"[{category}] - {entry.title} ({entry.link})" for entry in feed.entries[:10]]
            print(f"成功抓取 {category} : {len(entries)} 条新闻")
            return entries
        except Exception as e:
            print(f"抓取 {category} 失败: {e}")
            return []

    tasks_list = [fetch_feed(cat, url) for cat, url in feeds.items()]
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
