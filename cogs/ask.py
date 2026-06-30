import discord
from discord.ext import commands

class Ask(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.app_commands.command(name="ask", description="问 AI")
    async def ask(self, interaction: discord.Interaction, question: str):
        await interaction.response.defer()
        # 这里应当调用 core.ai_client
        # 目前返回一个 mock 的回复
        await interaction.followup.send(f"你问了：{question}\n[此处为 AI 回答占位符]")

async def setup(bot):
    await bot.add_cog(Ask(bot))
