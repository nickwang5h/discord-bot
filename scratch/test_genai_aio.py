import asyncio
from google import genai

async def run():
    key = "AQ.Ab8RN6IPZGjUmO6tDHUGjiNL_2HDOianNzJAaNDchGwyovYKhg"
    try:
        print("Testing aio generation...")
        client = genai.Client(api_key=key)
        response = await client.aio.models.generate_content(
            model='gemini-3.5-flash',
            contents='Say hello.'
        )
        print("Response:", response.text)
    except Exception as e:
        print("Error:", repr(e))

if __name__ == "__main__":
    asyncio.run(run())
