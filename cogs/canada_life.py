import discord
from discord.ext import commands
import aiohttp
import logging

logger = logging.getLogger(__name__)

class CanadaLife(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.app_commands.command(name="fx", description="查 CAD 汇率")
    async def fx(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get("https://api.frankfurter.app/latest?from=CAD&to=USD,CNY") as response:
                    response.raise_for_status()
                    d = await response.json()
            rates = d["rates"]
            await interaction.followup.send(f"💱 1 CAD = {rates['USD']} USD / {rates['CNY']} CNY")
        except Exception as e:
            logger.exception("获取汇率失败: %s", e)
            await interaction.followup.send("❌ 获取汇率失败，请稍后重试。")

async def setup(bot):
    await bot.add_cog(CanadaLife(bot))
