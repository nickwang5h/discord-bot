import asyncio
import os
from dotenv import load_dotenv
from core.ai_client import _ask_openrouter

load_dotenv()

async def test_or():
    # If API key is missing, it will raise an Exception
    if not os.getenv("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set. Skipping real API call.")
        return
        
    try:
        resp = await _ask_openrouter("Say hello world", "You are a helpful assistant.")
        print(resp)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_or())
