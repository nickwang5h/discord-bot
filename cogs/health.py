import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_RELEASE, SCHEDULED_JOBS_ENABLED
from core import ai_client, settings


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
        ]
        cooldown = int(status["gemini_cooldown_seconds"])
        if cooldown:
            provider_lines.append(f"- Gemini cooldown: ⏳ {cooldown}s")

        task_specs = [
            ("AI 日报", "AIDaily", "ai_news_daily"),
            ("综合新闻", "NewsDigest", "daily"),
            ("高级资讯抓取", "AdvancedNews", "hourly_fetch"),
            ("高级精读", "AdvancedNews", "scheduled_digest"),
            ("每日阅读", "DailyReading", "reading_loop"),
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
            f"- 高级新闻频道: {'✅' if settings.get_setting('TEST_NEWS_CHANNEL_ID') else '➖'}",
            f"- 阅读频道: {'✅' if settings.get_setting('READING_CHANNEL_ID') else '➖'}",
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
