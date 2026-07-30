import asyncio
import os
import sys
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
import core.ai_client as ai_client

load_dotenv(override=True)

async def main():
    try:
        print("Testing Groq fallback...")
        with (
            patch.object(ai_client, "model_available", False),
            patch.object(ai_client, "client", None),
        ):
            res = await ai_client.ask_ai(
                "你好，这是一次测试，请回答收到。",
                "You are a helpful assistant.",
                use_search=False,
            )
        print("\n--- Response ---")
        print(res)
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
