import asyncio
from core import ai_client
from dotenv import load_dotenv

load_dotenv()

async def run():
    print("Testing Gemini API (use_search=False)...")
    try:
        response = await ai_client.ask_ai("Say 'Hello, Gemini is working!'", use_search=False)
        print("Response:", response)
    except Exception as e:
        print("Error:", e)

    print("\nTesting Gemini API (use_search=True)...")
    try:
        response = await ai_client.ask_ai("What is the current version of Python?", use_search=True)
        print("Response:", response)
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    asyncio.run(run())
