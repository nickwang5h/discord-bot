import discord
from discord.ext import commands

class Lifestyle(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.app_commands.command(name="recipe", description="输入食材，AI 给菜谱")
    async def recipe(self, interaction: discord.Interaction, ingredients: str):
        await interaction.response.defer()
        await interaction.followup.send(f"你冰箱里有：{ingredients}\n[此处为菜谱生成占位符]")

async def setup(bot):
    await bot.add_cog(Lifestyle(bot))
