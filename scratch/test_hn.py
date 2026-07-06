import asyncio
import aiohttp
import json

async def fetch_hn_top_stories():
    async with aiohttp.ClientSession() as session:
        # Fetch top 50 stories
        async with session.get("https://hacker-news.firebaseio.com/v0/topstories.json") as response:
            story_ids = await response.json()
            story_ids = story_ids[:50] # Take top 50
        
        stories = []
        async def fetch_story(story_id):
            async with session.get(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json") as response:
                return await response.json()
        
        tasks = [fetch_story(sid) for sid in story_ids]
        results = await asyncio.gather(*tasks)
        
        for item in results:
            if item and 'title' in item:
                stories.append(f"- {item.get('title')} (Score: {item.get('score', 0)})")
                
        print("\n".join(stories))

if __name__ == "__main__":
    asyncio.run(fetch_hn_top_stories())
