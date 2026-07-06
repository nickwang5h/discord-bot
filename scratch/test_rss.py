import asyncio
import feedparser

async def run():
    print("Fetching RSS...")
    rss_url = "https://news.google.com/rss?hl=en-CA&gl=CA&ceid=CA:en"
    feed = await asyncio.to_thread(feedparser.parse, rss_url)
    
    news_items = []
    for entry in feed.entries[:25]:
        news_items.append(f"- {entry.title} ({entry.link})")
        
    print(f"Got {len(news_items)} items")
    if len(news_items) > 0:
        print(news_items[0])

if __name__ == "__main__":
    asyncio.run(run())
