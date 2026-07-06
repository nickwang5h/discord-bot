import asyncio
from core import ai_client
from dotenv import load_dotenv

load_dotenv()

async def run():
    system_prompt = (
        "你是一个专业的新闻主编。请使用 Google 搜索获取过去 24 小时的最新重大新闻。\n"
        "必须严格按照以下四个板块进行分类：\n"
        "1. 🌍 国际要闻\n"
        "2. 🍁 加拿大新闻\n"
        "3. 💻 科技动态\n"
        "4. 📈 金融市场\n"
        "要求：\n"
        "1. 每个板块必须精选 5 条最具价值的新闻（总计严格为 20 条）。\n"
        "2. 每条新闻必须极度简短（控制在20字以内的核心一句话总结）。\n"
        "3. 每条新闻必须附带来源 URL，并使用 Markdown 语法：`- [新闻极简标题](URL)`。\n"
        "注意：总字数必须严格控制以适应 Discord 消息长度限制。"
    )
    raw_text = """这是今天抓取的新闻标题列表：
- Submarine showdown: Carney poised to choose Canada's next submarine fleet - CBC (https://news.google.com/rss/...)
- Poilievre calls out Trudeau over foreign interference - Global News (https://...)
- Markets surge to record highs - Financial Post (https://...)
- Apple announces new AI features - TechCrunch (https://...)
- World leaders gather for climate summit - Reuters (https://...)

请严格基于这些新闻为我生成今天的早间新闻简报。"""
    
    print("Testing offline Gemini generation...")
    try:
        digest = await ai_client.ask_ai(raw_text, system=system_prompt, use_search=False, fallback_offline=False)
        print("--- RESULT ---")
        print(repr(digest))
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(run())
