from datetime import datetime, timezone
from typing import Any, cast

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_RELEASE, INFO_CURATOR_SERVICE_URL, SCHEDULED_JOBS_ENABLED
from core import ai_client, settings


def claude_status_line(status: dict[str, Any]) -> str:
    """Render Claude state and usage numbers; never includes credentials."""
    if not status.get("claude"):
        return "- Claude Opus 5.5: ➖ 未配置"
    usage = cast(dict[str, Any], status.get("claude_usage") or {})
    if usage.get("disabled_until"):
        until = datetime.fromtimestamp(float(usage["disabled_until"]), timezone.utc).strftime("%H:%M")
        state = f"⛔ {usage.get('disabled_reason') or '停用'}至 {until} UTC"
    elif usage.get("cooldown_seconds"):
        state = f"⏳ 冷却 {int(usage['cooldown_seconds'])}s"
    elif usage.get("monthly_exhausted"):
        state = "⏸️ 本月预算已满"
    elif usage.get("daily_exhausted"):
        state = "⏸️ 今日预算已满"
    else:
        state = "✅ 可用"
    return (
        f"- Claude Opus 5.5: {state}"
        f" · 今日 ${float(usage.get('today_usd', 0)):.2f}/${float(usage.get('daily_usd', 0)):.2f}"
        f" · {int(usage.get('today_calls', 0))} 次"
        f" · 本月 ${float(usage.get('month_usd', 0)):.2f}/${float(usage.get('monthly_usd', 0)):g}"
    )


class Health(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="health", description="[管理员] 查看机器人、AI 和定时任务健康状态")
    @app_commands.checks.has_permissions(administrator=True)
    async def health(self, interaction: discord.Interaction):
        status = ai_client.get_provider_status()
        provider_lines = [
            f"- Gemini: {'✅' if status['gemini'] else '➖'} `{status['gemini_model']}`",
            f"- Groq: {'✅' if status['groq'] else '➖'}",
            f"- Zhipu: {'✅' if status['zhipu'] else '➖'}",
            f"- OpenRouter: {'✅' if status['openrouter'] else '➖'}",
            claude_status_line(status),
            f"- Video sidecar: {'✅ 已配置' if INFO_CURATOR_SERVICE_URL else '➖ 未配置'}",
        ]
        cooldown = int(cast(float, status["gemini_cooldown_seconds"]))
        if cooldown:
            provider_lines.append(f"- Gemini cooldown: ⏳ {cooldown}s")

        task_specs = [
            ("AI 日报", "AIDaily", "ai_news_daily"),
            ("新闻订阅调度", "News", "dispatch"),
            ("共享新闻素材", "News", "hourly_fetch"),
            ("反馈曝光同步", "Feedback", "sync_loop"),
            ("每日阅读", "DailyReading", "reading_loop"),
            ("天气预报", "Weather", "weather_daily"),
            ("Epic 喜加一", "Gaming", "epic_weekly"),
            ("Steam 折扣监控", "Gaming", "steam_daily"),
        ]
        task_lines = []
        for label, cog_name, attr_name in task_specs:
            cog = self.bot.get_cog(cog_name)
            loop = getattr(cog, attr_name, None) if cog else None
            if loop is None:
                task_lines.append(f"- {label}: ❌ 未加载")
            elif not SCHEDULED_JOBS_ENABLED:
                task_lines.append(f"- {label}: ⏸️ 部署配置禁用")
            elif loop.failed():
                task_lines.append(f"- {label}: ❌ 已停止")
            elif loop.is_running():
                task_lines.append(f"- {label}: ✅ 运行中")
            else:
                task_lines.append(f"- {label}: ⚠️ 未运行")

        channel_lines = [
            f"- 新闻频道: {'✅' if settings.get_setting('NEWS_CHANNEL_ID') else '➖'}",
            f"- 视野拾遗频道: {'✅' if settings.get_setting('TEST_NEWS_CHANNEL_ID') else '➖'}",
            f"- 阅读频道: {'✅' if settings.get_setting('READING_CHANNEL_ID') else '➖'}",
            f"- 天气频道: {'✅' if settings.get_setting('WEATHER_CHANNEL_ID') else '➖'}",
            f"- 游戏特惠频道: {'✅' if settings.get_setting('GAMING_CHANNEL_ID') else '➖'}",
        ]

        embed = discord.Embed(
            title="🩺 Bot Health",
            description=(
                f"Gateway latency: `{round(self.bot.latency * 1000)}ms`\n"
                f"Release: `{BOT_RELEASE}`"
            ),
            color=discord.Color.green(),
        )
        embed.add_field(name="AI Providers", value="\n".join(provider_lines), inline=False)
        embed.add_field(name="Scheduled Tasks", value="\n".join(task_lines), inline=False)
        embed.add_field(name="Channels", value="\n".join(channel_lines), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Health(bot))
