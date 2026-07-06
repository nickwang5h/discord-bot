import discord
from discord.ext import commands
from core import ai_client
from core.utils import create_ai_embed

class Lifestyle(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.app_commands.command(name="recipe", description="输入食材，AI 给菜谱")
    async def recipe(self, interaction: discord.Interaction, ingredients: str, search_online: bool = False):
        await interaction.response.defer()
        
        if search_online:
            system_prompt = "你是一位美食家。用户会提供他们现有的食材，请为他们去网上搜索真实的食谱，并挑选一份最推荐的做法。必须在回答中附上你找到的正宗菜谱网页链接，说明详细的做法步骤。"
        else:
            system_prompt = "你是一位米其林三星大厨，精通各种中西餐料理。用户会提供他们现有的食材，请为他们构思一道美味的菜谱，包含菜名、食材用量估算和清晰的步骤。语言要亲切、幽默，格式要清晰易读。"
        
        answer = await ai_client.ask_ai(ingredients, system=system_prompt, use_search=search_online)
        
        embed = create_ai_embed(
            title=f"👨‍🍳 量身定制的食谱 (食材: {ingredients})",
            description=answer,
            color=discord.Color.yellow()
        )
        
        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Lifestyle(bot))
