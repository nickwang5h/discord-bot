import discord
from discord.ext import commands
from core import ai_client

class Ask(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.app_commands.command(name="ask", description="问 AI")
    async def ask(self, interaction: discord.Interaction, question: str):
        await interaction.response.defer()
        # 调用 AI 客户端获取回答
        system_prompt = "你是一个智能、有用的 Discord 机器人助手。请用清晰友好的中文回答用户的问题。"
        answer = await ai_client.summarize(question, system=system_prompt)
        
        # 避免 Discord 字数限制报错，最多 2000 字
        if len(answer) > 1900:
            answer = answer[:1900] + "\n...(回答过长被截断)"
            
        await interaction.followup.send(f"**你问了**：{question}\n\n{answer}")
async def setup(bot):
    await bot.add_cog(Ask(bot))
