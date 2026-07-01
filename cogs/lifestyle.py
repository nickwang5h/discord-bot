import discord
from discord.ext import commands
from core import ai_client
from core.utils import create_ai_embed

class Lifestyle(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.app_commands.command(name="recipe", description="输入食材，AI 给菜谱")
    async def recipe(self, interaction: discord.Interaction, ingredients: str):
        await interaction.response.defer()
        system_prompt = "你是一位米其林三星大厨，精通各种中西餐料理。用户会提供他们现有的食材，请为他们构思一道美味的菜谱，包含菜名、食材用量估算和清晰的步骤。语言要亲切、幽默，格式要清晰易读。"
        
        answer = await ai_client.summarize(ingredients, system=system_prompt)
        
        embed = create_ai_embed(
            title=f"👨‍🍳 量身定制的食谱 (食材: {ingredients})",
            description=answer,
            color=discord.Color.yellow()
        )
        
        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Lifestyle(bot))
