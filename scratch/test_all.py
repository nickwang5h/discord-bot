import asyncio
import os
from unittest.mock import patch
from core import ai_client
from dotenv import load_dotenv

# Ensure override works
load_dotenv(override=True)

async def run_tests():
    print("="*50)
    print("🚀 INTEGRATION TEST SUITE STARTED")
    print("="*50)

    # 1. Test standard Gemini generation (No Search)
    print("\n[Test 1] Standard Gemini Generation (ask_ai, use_search=False)")
    try:
        ans1 = await ai_client.ask_ai("What is 1+1? Reply with just the number.", use_search=False)
        print("✅ SUCCESS. Output:", repr(ans1))
    except Exception as e:
        print("❌ FAILED:", repr(e))

    # 2. Test OpenRouter fallback on Gemini Crash
    print("\n[Test 2] OpenRouter Fallback Logic (Simulating Gemini failure)")
    try:
        # Patch the _try_gemini to throw an exception
        original_try_gemini = ai_client._try_gemini
        
        async def fake_try_gemini(with_search):
            raise Exception("429 RESOURCE_EXHAUSTED Fake Error")
            
        ai_client._try_gemini = fake_try_gemini
        ans2 = await ai_client.ask_ai("This should be answered by OpenRouter.", use_search=False)
        print("✅ SUCCESS (Fallback triggered). Output:", repr(ans2))
        
        # Restore
        ai_client._try_gemini = original_try_gemini
    except Exception as e:
        print("❌ FAILED:", repr(e))
        ai_client._try_gemini = original_try_gemini

    # 3. Test RSS News Digest Logic (Simulating news_digest.py)
    print("\n[Test 3] RSS News Fetching & Summarization (Simulating news_digest.py)")
    try:
        import feedparser
        print("Fetching RSS...")
        rss_url = "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        feed = await asyncio.to_thread(feedparser.parse, rss_url)
        news_items = [f"- {entry.title}" for entry in feed.entries[:3]] # Take only 3 for speed
        raw_text = "这是新闻标题列表：\n" + "\n".join(news_items) + "\n\n请总结这3条新闻。"
        ans3 = await ai_client.ask_ai(raw_text, system="你是一个新闻编辑，只返回极简中文总结。", use_search=False)
        print("✅ SUCCESS. Output:", repr(ans3))
    except Exception as e:
        print("❌ FAILED:", repr(e))

    print("\n" + "="*50)
    print("🏁 ALL TESTS COMPLETED")
    print("="*50)

if __name__ == "__main__":
    asyncio.run(run_tests())
