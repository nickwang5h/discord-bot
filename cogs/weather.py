import asyncio
import datetime
import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import SCHEDULED_JOBS_ENABLED, TZ
from core import settings
from core.jobs import run_delivery_job
from core.weather import build_morning_weather_embeds, get_weather_embed

logger = logging.getLogger(__name__)

DEFAULT_CITIES = ["Ottawa", "Sudbury"]


class Weather(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._delivery_lock = asyncio.Lock()
        if SCHEDULED_JOBS_ENABLED:
            self.weather_daily.start()
        else:
            logger.info("每日天气预报定时任务已通过部署配置禁用")

    def cog_unload(self):
        self.weather_daily.cancel()

    def _get_target_cities(self) -> list[str]:
        configured = settings.get_setting("WEATHER_CITIES", DEFAULT_CITIES)
        if isinstance(configured, str):
            cities = [c.strip() for c in configured.split(",") if c.strip()]
            return cities or DEFAULT_CITIES
        if isinstance(configured, list) and configured:
            return [str(c).strip() for c in configured if str(c).strip()]
        return DEFAULT_CITIES

    async def _build_daily_embeds(self) -> list[discord.Embed]:
        cities = self._get_target_cities()
        async with aiohttp.ClientSession() as session:
            embeds = await build_morning_weather_embeds(cities, session)
        if not embeds:
            raise RuntimeError("未能生成任何城市的天气预报 Embed")
        return embeds

    async def _run_daily(self, channel: discord.abc.Messageable):
        return await run_delivery_job(
            lock=self._delivery_lock,
            task_name="每日天气预报生成",
            build=self._build_daily_embeds,
            deliver=lambda embeds: channel.send(
                content="🌤️ **早安！今日天气播报：**",
                embeds=embeds,
            ),
        )

    @tasks.loop(time=datetime.time(hour=7, minute=0, tzinfo=TZ))
    async def weather_daily(self):
        logger.info("执行每日天气预报定时任务")
        channel_id = settings.get_setting("WEATHER_CHANNEL_ID") or settings.get_setting("NEWS_CHANNEL_ID")
        if not channel_id:
            logger.warning("未配置 WEATHER_CHANNEL_ID 或 NEWS_CHANNEL_ID，跳过每日天气推送")
            return

        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            logger.error("找不到配置的频道 ID: %s", channel_id)
            return

        await self._run_daily(channel)

    @weather_daily.before_loop
    async def before_weather_daily(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="weather", description="查询指定城市天气 (默认 Ottawa)")
    @app_commands.describe(city="城市名称，例如 Ottawa, Sudbury, Toronto, Beijing")
    async def weather(self, interaction: discord.Interaction, city: str = "Ottawa"):
        await interaction.response.defer()
        target_city = city.strip() or "Ottawa"
        async with aiohttp.ClientSession() as session:
            embed = await get_weather_embed(target_city, session)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="test_weather", description="[管理员] 立即测试天气播报")
    @app_commands.checks.has_permissions(administrator=True)
    async def test_weather(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cities = self._get_target_cities()
        async with aiohttp.ClientSession() as session:
            embeds = await build_morning_weather_embeds(cities, session)
        await interaction.followup.send(
            content="🌤️ **早安！今日天气播报：**",
            embeds=embeds,
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Weather(bot))
