import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import discord
from discord.ext import commands

from cogs.ai_daily import AIDaily
from cogs.daily_reading import DailyReading
from cogs.help import _build_help_embed
from cogs.news import News
from cogs.weather import Weather
from config import PROJECT_ROOT


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
            with patch('cogs.news.STATE_ROOT', Path(directory)):
                try:
                    for extension in extensions:
                        await bot.load_extension(extension)
                    self.assertEqual(set(bot.extensions), set(extensions))

                    embed = _build_help_embed(bot.tree.get_commands())
                    rendered = "\n".join(field.value or '' for field in embed.fields)
                    for command in bot.tree.get_commands():
                        self.assertIn(f"`/{command.name}`", rendered)
                finally:
                    for extension in list(bot.extensions):
                        await bot.unload_extension(extension)
                    await bot.close()

    async def test_scheduled_jobs_can_be_disabled_without_removing_manual_commands(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch('cogs.news.STATE_ROOT', Path(directory)),
                patch.dict(News.__init__.__globals__, {"SCHEDULED_JOBS_ENABLED": False}),
                patch.dict(AIDaily.__init__.__globals__, {"SCHEDULED_JOBS_ENABLED": False}),
                patch.dict(DailyReading.__init__.__globals__, {"SCHEDULED_JOBS_ENABLED": False}),
                patch.dict(Weather.__init__.__globals__, {"SCHEDULED_JOBS_ENABLED": False}),
            ):
                cogs = [News(bot), AIDaily(bot), DailyReading(bot), Weather(bot)]
                try:
                    loop_specs = (
                        (cogs[0], "hourly_fetch"),
                        (cogs[0], "dispatch"),
                        (cogs[1], "ai_news_daily"),
                        (cogs[2], "reading_loop"),
                        (cogs[3], "weather_daily"),
                    )
                    for cog, loop_name in loop_specs:
                        self.assertFalse(getattr(cog, loop_name).is_running(), loop_name)

                    command_names = {
                        command.name
                        for cog in cogs
                        for command in cog.get_app_commands()
                    }
                    self.assertTrue(
                        {"test_hourly_fetch", "test_ai_news", "test_reading", "test_news", "test_weather"}
                        <= command_names
                    )
                finally:
                    await cogs[0].cog_unload()
                    for cog in cogs[1:]:
                        cog.cog_unload()
                    await bot.close()

    async def test_help_groups_commands_by_audience(self):
        class FakeCommand:
            def __init__(self, name: str, description: str):
                self.name = name
                self.qualified_name = name
                self.description = description

        embed = _build_help_embed(
            [
                FakeCommand("ask", "向 AI 提问任何问题"),
                FakeCommand("debug", "[Dev] 分析错误日志"),
                FakeCommand("health", "[管理员] 查看健康状态"),
                FakeCommand("test_fetch", "[实验] 手动抓取"),
            ]
        )

        fields = {field.name: field.value or '' for field in embed.fields}
        self.assertIn("`/ask`", fields["常用命令"])
        self.assertIn("`/debug`", fields["开发工具"])
        self.assertIn("`/health`", fields["管理员命令"])
        self.assertIn("`/test_fetch`", fields["实验功能"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
