import discord
from discord.ext import commands
from core import ai_client

class Lifestyle(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.app_commands.command(name="recipe", description="输入食材，AI 给菜谱")
    async def recipe(self, interaction: discord.Interaction, ingredients: str):
        await interaction.response.defer()
        system_prompt = "你是一位米其林三星大厨，精通各种中西餐料理。用户会提供他们现有的食材，请为他们构思一道美味的菜谱，包含菜名、食材用量估算和清晰的步骤。语言要亲切、幽默，格式要清晰易读。"
        
        answer = await ai_client.summarize(ingredients, system=system_prompt)
        
        if len(answer) > 1900:
            answer = answer[:1900] + "\n...(菜谱过长被截断)"
            
        await interaction.followup.send(f"👨‍🍳 **为你量身定制的食谱** (食材: {ingredients})\n\n{answer}")

async def setup(bot):
    await bot.add_cog(Lifestyle(bot))
