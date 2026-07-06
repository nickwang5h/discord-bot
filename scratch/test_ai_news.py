import asyncio
import aiohttp
import os
from dotenv import load_dotenv
from core import ai_client

load_dotenv()

async def test_ai_news():
    print("Testing HN fetching...")
    try:
        async with aiohttp.ClientSession() as session:
            # 1. 获取 HN Top 50 的 ID
            async with session.get("https://hacker-news.firebaseio.com/v0/topstories.json") as response:
                story_ids = await response.json()
                story_ids = story_ids[:50]
            
            # 2. 并发获取文章详情
            async def fetch_story(story_id):
                async with session.get(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json") as res:
                    return await res.json()
            
            tasks_list = [fetch_story(sid) for sid in story_ids]
            stories = await asyncio.gather(*tasks_list)
            
        # 3. 过滤有效数据并提取标题和链接
        news_items = []
        for i, item in enumerate(stories):
            if item and 'title' in item:
                # 获取原链接，如果没有则使用 HN 内部链接
                url = item.get('url', f"https://news.ycombinator.com/item?id={item.get('id')}")
                score = item.get('score', 0)
                news_items.append(f"[{i+1}] Title: {item.get('title')} | Score: {score} | URL: {url}")
        
        raw_text = "\n".join(news_items)
        print("Fetched top 50 stories. Requesting AI summary...")
        
        system_prompt = (
            "你是一个面向开发者的硬核 AI 技术观察员。用户提供了一批今日 Hacker News 的热门文章列表（包含标题、链接）。\n"
            "请你完成以下两项总结，并使用客观专业的中文回复：\n"
            "1. 【🔥 HN 社区当前最热 Top 5】：直接挑选列表中排在最前面的 5 条新闻，翻译标题并一句话简介。\n"
            "2. 【🤖 开发者 AI 动态】：从列表中，重点筛选出与 AI 开发相关的干货（最多 10 条），例如：大模型发布、开源 AI 项目、机器学习工具更新等。\n\n"
            "重要要求：\n"
            "1. 每条新闻必须严格保留其对应的原始 URL 链接。\n"
            "2. 请严格使用 Markdown 的链接语法，例如：`- [中文翻译标题](URL): 一句话简介`。"
        )
        
        digest = await ai_client.summarize(raw_text, system=system_prompt)
        print("\n=== AI DIGEST RESULT ===")
        print(digest)
        print("========================")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_ai_news())
