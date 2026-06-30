import discord
from discord.ext import commands
import aiohttp

class CanadaLife(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.app_commands.command(name="fx", description="查 CAD 汇率")
    async def fx(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.get("https://api.frankfurter.app/latest?from=CAD&to=USD,CNY")
                d = await r.json()
            rates = d["rates"]
            await interaction.followup.send(f"💱 1 CAD = {rates['USD']} USD / {rates['CNY']} CNY")
        except Exception as e:
            await interaction.followup.send(f"获取汇率失败: {e}")

async def setup(bot):
    await bot.add_cog(CanadaLife(bot))
