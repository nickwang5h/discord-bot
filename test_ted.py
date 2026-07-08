import asyncio
from core import ai_client
import feedparser
import random
from dotenv import load_dotenv
import time
load_dotenv()

async def test():
    try:
        url = "https://www.ted.com/talks/rss"
        print("Parsing feed...")
        t0 = time.time()
        feed = await asyncio.to_thread(feedparser.parse, url)
        print(f"Parsed feed in {time.time() - t0:.2f}s, entries: {len(feed.entries)}")
        if not feed.entries:
            print("No entries")
            return
        entry = random.choice(feed.entries[:20])
        raw_text = f"Title: {entry.title}\nLink: {entry.link}\nSummary: {entry.summary if hasattr(entry, 'summary') else ''}"
        system_prompt = "Hello"
        print("Calling AI...")
        t0 = time.time()
        res = await ai_client.ask_ai(raw_text, system=system_prompt, use_search=False)
        print(f"AI replied in {time.time() - t0:.2f}s")
    except Exception as e:
        print("Exception:", e)

asyncio.run(test())
