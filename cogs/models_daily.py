"""今日思维模型：每天把 models.wuhua.app 的「今日一卡」和一道小题推到 Discord。

只推模型卡本身，不带其他练习的链接。卡片取自站点的 /today.json?date=<UTC 日期>，与 Telegram 推送、/today 页面和每日邮件是同一张。
不调用模型；投递按 UTC 日期记账，同一天只自动推送一次。
"""

import asyncio
import datetime
import logging
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import SCHEDULED_JOBS_ENABLED, STATE_ROOT, TZ
from core import settings
from core.jobs import run_delivery_job
from core.storage import JsonStore

logger = logging.getLogger(__name__)

SITE = "https://models.wuhua.app"
# Cloudflare 会拒绝 aiohttp 默认的 User-Agent。
USER_AGENT = "discord-bot-models-daily/1 (+https://models.wuhua.app)"
KEYS = "ABCD"
SENT_HISTORY_DAYS = 60

_sent_store = JsonStore(STATE_ROOT / "data/models_daily.json", dict)


def _utc_today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


def _validate_day(day: Any) -> dict[str, Any]:
    """只接受字段齐全、恰好一个正确选项的 /today.json 回答。"""
    if not isinstance(day, dict):
        raise ValueError("today.json 不是对象")
    for key in ("id", "name", "one_line", "url"):
        if not isinstance(day.get(key), str) or not day[key].strip():
            raise ValueError(f"today.json 缺少 {key}")
    if not day["url"].startswith(f"{SITE}/"):
        raise ValueError("today.json 的卡片链接不在站点内")
    question = day.get("question")
    if not isinstance(question, dict):
        raise ValueError("today.json 缺少 question")
    options = question.get("options")
    if not isinstance(options, list) or not 2 <= len(options) <= len(KEYS):
        raise ValueError("today.json 的选项数量无效")
    if sum(1 for option in options if isinstance(option, dict) and option.get("correct") is True) != 1:
        raise ValueError("today.json 必须恰好有一个正确选项")
    if not all(isinstance(option, dict) and isinstance(option.get("text"), str) for option in options):
        raise ValueError("today.json 的选项缺少文字")
    return day


def _build_embed(day: dict[str, Any]) -> discord.Embed:
    question = day["question"]
    options = question["options"]
    right = next(index for index, option in enumerate(options) if option["correct"])
    answer = question.get("explain") or options[right].get("feedback") or ""
    listed = "\n".join(f"{KEYS[index]}. {option['text']}" for index, option in enumerate(options))
    parts = [
        day["one_line"],
        f"{question.get('stem', '')}\n**{question.get('question', '')}**".strip(),
        listed,
        f"||答案 {KEYS[right]}：{answer}||",
        f"[读整张卡]({day['url']})",
    ]
    title = f"🧠 今日模型：{day['name']}"
    if day.get("en"):
        title += f"（{day['en']}）"
    embed = discord.Embed(
        title=title[:256],
        url=day["url"],
        description="\n\n".join(part for part in parts if part)[:4096],
        color=discord.Color.teal(),
    )
    embed.set_footer(text=f"models.wuhua.app · {day.get('date', '')}".rstrip(" ·"))
    return embed


async def fetch_day(date: datetime.date) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        async with session.get(f"{SITE}/today.json", params={"date": date.isoformat()}, allow_redirects=False) as response:
            response.raise_for_status()
            return _validate_day(await response.json(content_type=None))


def _record_sent(date: datetime.date, card_id: str) -> None:
    def update(data: dict[str, str]) -> dict[str, str]:
        data = data if isinstance(data, dict) else {}
        data[date.isoformat()] = card_id
        return dict(sorted(data.items())[-SENT_HISTORY_DAYS:])

    _sent_store.update(update)


def _already_sent(date: datetime.date) -> bool:
    data = _sent_store.read()
    return isinstance(data, dict) and date.isoformat() in data


def _channel_id() -> str | None:
    """未单独设置时沿用每日阅读频道。"""
    return settings.get_setting("MODELS_CHANNEL_ID") or settings.get_setting("READING_CHANNEL_ID")


class ModelsDaily(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._delivery_lock = asyncio.Lock()
        if SCHEDULED_JOBS_ENABLED:
            self.models_daily.start()
        else:
            logger.info("今日思维模型定时任务已通过部署配置禁用")

    def cog_unload(self):
        self.models_daily.cancel()

    async def _run(self, channel: discord.abc.Messageable, date: datetime.date, *, record: bool):
        day: dict[str, Any] = {}

        async def build() -> discord.Embed:
            day.update(await fetch_day(date))
            return _build_embed(day)

        return await run_delivery_job(
            lock=self._delivery_lock,
            task_name="今日思维模型",
            build=build,
            deliver=lambda embed: channel.send(embed=embed),
            on_delivered=(lambda _: _record_sent(date, day["id"])) if record else None,
        )

    @tasks.loop(time=datetime.time(hour=8, minute=0, tzinfo=TZ))
    async def models_daily(self):
        date = _utc_today()
        if _already_sent(date):
            logger.info("今日思维模型 %s 已推送，跳过", date)
            return
        channel_id = _channel_id()
        if not channel_id:
            logger.warning("未设置 MODELS_CHANNEL_ID 或 READING_CHANNEL_ID，跳过今日思维模型推送")
            return
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            logger.error("找不到配置的频道 ID: %s", channel_id)
            return
        try:
            await self._run(channel, date, record=True)
        except Exception:
            logger.exception("今日思维模型推送失败")

    @models_daily.before_loop
    async def before_models_daily(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="test_models", description="[管理员] 在当前频道预览今日思维模型（不计入每日推送）")
    @app_commands.checks.has_permissions(administrator=True)
    async def test_models(self, interaction: discord.Interaction):
        await interaction.response.send_message("正在获取今日思维模型...", ephemeral=True)
        try:
            await self._run(interaction.channel, _utc_today(), record=False)
        except Exception:
            logger.exception("今日思维模型预览失败")
            await interaction.followup.send("❌ 获取今日思维模型失败，请查看日志。", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModelsDaily(bot))
