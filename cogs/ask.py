import discord
from discord.ext import commands
from core import ai_client
from core.utils import create_ai_embed

class Ask(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.app_commands.command(name="ask", description="向 AI 提问任何问题")
    @discord.app_commands.checks.cooldown(1, 60.0, key=lambda i: i.user.id)
    async def ask(self, interaction: discord.Interaction, question: str, use_search: bool = True):
        await interaction.response.defer()
        # 调用 AI 客户端获取回答
        system_prompt = "你是一个智能的 Discord 机器人助手。请用清晰友好的中文回答用户的问题。\n重要规则：不要在开头说“你好”之类的寒暄，也不要在结尾加任何诸如“你还有什么想了解的吗？”之类的追问，直接给出精准的答案即可。"
        answer = await ai_client.ask_ai(question, system=system_prompt, use_search=use_search)
        
        embed = create_ai_embed(
            title=f"❓ 提问: {question}",
            description=answer,
            color=discord.Color.green()
        )
        
        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Ask(bot))
