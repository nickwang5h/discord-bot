import asyncio
from cogs.daily_reading import DailyReading
import sys

class MockBot:
    def __init__(self):
        pass

async def test():
    try:
        cog = DailyReading(MockBot())
        cog.reading_loop.cancel()
        res = await cog.generate_ted_reading()
        print("----- RESULT -----")
        if res is None:
            print("NONE")
        else:
            print(res[:200])
    except Exception as e:
        print(f"Exception: {e}")

asyncio.run(test())
