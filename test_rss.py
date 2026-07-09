import feedparser
import random
url = "https://www.ted.com/talks/rss"
feed = feedparser.parse(url)
print("Entries:", len(feed.entries))
for entry in feed.entries[:3]:
    print("-----")
    print(entry.title)
    print(entry.link)
    print(entry.get("summary", "")[:100])
