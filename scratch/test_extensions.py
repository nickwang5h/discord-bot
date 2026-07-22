import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import discord
from discord.ext import commands

from config import PROJECT_ROOT
from core import news_cache
from core.storage import JsonStore


class ExtensionLoadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    async def test_all_cogs_load_and_unload(self):
        intents = discord.Intents.default()
        intents.message_content = True
        bot = commands.Bot(command_prefix="!", intents=intents)
        extensions = [
            f"cogs.{path.stem}"
            for path in sorted((PROJECT_ROOT / "cogs").glob("*.py"))
            if path.name != "__init__.py"
        ]

        with tempfile.TemporaryDirectory() as directory:
            cache_store = JsonStore(Path(directory) / "news.json", list)
            with patch.object(news_cache, "_cache_store", cache_store):
                try:
                    for extension in extensions:
                        await bot.load_extension(extension)
                    self.assertEqual(set(bot.extensions), set(extensions))
                finally:
                    for extension in list(bot.extensions):
                        await bot.unload_extension(extension)
                    await bot.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
