import asyncio
import os
import sys

# Add project root to sys.path so we can import core
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from cogs.daily_reading import DailyReading
import discord

class MockBot:
    def __init__(self):
        pass

async def test_reading():
    print("Initializing MockBot and DailyReading Cog...")
    bot = MockBot()
    cog = DailyReading(bot)
    
    # We don't want the background loop to run during our test, so cancel it
    cog.reading_loop.cancel()
    
    print("\n--- Testing Scenario Generation ---")
    scenario = await cog.generate_scenario()
    print("Result:")
    print(scenario)
    
    print("\n--- Testing RSS Reading Generation ---")
    rss = await cog.generate_rss_reading()
    print("Result:")
    print(rss)
    
    print("\n--- Testing Quote Generation ---")
    quote = await cog.generate_quote()
    print("Result:")
    print(quote)

if __name__ == "__main__":
    asyncio.run(test_reading())
