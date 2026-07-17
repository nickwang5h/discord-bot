import asyncio
import os
import sys

# 添加主目录到系统路径，解决 core 模块找不到的问题
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
import core.ai_client as ai_client

load_dotenv(override=True)

async def main():
    # Force Gemini to fail by setting a bad key
    os.environ["GEMINI_API_KEY"] = "bad_key"
    ai_client.reload_client()
    
    try:
        print("Testing Zhipu fallback with real key...")
        res = await ai_client.ask_ai("你好，这是一次测试，请回答收到并告诉我你的模型版本。", "You are a helpful assistant.", use_search=False)
        print("\n--- Response ---")
        print(res)
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
