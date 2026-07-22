import logging

import discord
from discord.ext import commands

from config import DISCORD_TOKEN, LOG_LEVEL, PROJECT_ROOT
from core.logging_config import configure_logging

configure_logging(LOG_LEVEL)
logger = logging.getLogger(__name__)


class DiscordBot(commands.Bot):
    async def setup_hook(self) -> None:
        """Load extensions once per process, before Discord dispatches ready events."""
        cogs_dir = PROJECT_ROOT / "cogs"
        for path in sorted(cogs_dir.glob("*.py")):
            if path.name == "__init__.py":
                continue
            extension = f"cogs.{path.stem}"
            try:
                await self.load_extension(extension)
                logger.info("成功加载扩展: %s", extension)
            except Exception:
                logger.exception("加载扩展失败: %s", extension)

        try:
            synced = await self.tree.sync()
            logger.info("已同步 %s 个斜杠命令", len(synced))
        except Exception:
            logger.exception("斜杠命令同步失败")

    async def on_ready(self) -> None:
        logger.info("机器人已上线: %s (guilds=%s)", self.user, len(self.guilds))


intents = discord.Intents.default()
intents.message_content = True
bot = DiscordBot(command_prefix="!", intents=intents)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: discord.app_commands.AppCommandError,
) -> None:
    if isinstance(error, discord.app_commands.CommandOnCooldown):
        message = f"⏳ 技能冷却中，请在 {error.retry_after:.1f} 秒后再试。"
    elif isinstance(error, discord.app_commands.MissingPermissions):
        message = "❌ 您没有权限执行这个命令。"
    else:
        logger.error(
            "斜杠命令执行失败",
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "❌ 发生错误，无法执行命令。"

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.exception("向用户发送命令错误提示失败")


@bot.tree.command(name="ping", description="测试 bot 是否存活")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"pong 🏓 {round(bot.latency * 1000)}ms")


def main() -> int:
    if not DISCORD_TOKEN or DISCORD_TOKEN == "your_discord_bot_token_here":
        logger.error("请在 .env 文件中配置有效的 DISCORD_TOKEN")
        return 1
    bot.run(DISCORD_TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
