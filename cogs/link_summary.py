import re
import discord
from discord.ext import commands

URL_RE = re.compile(r"https?://\S+")

class LinkSummary(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
            
        urls = URL_RE.findall(message.content)
        if urls:
            # 找到链接后可以调用抓取逻辑
            # 这里先打印以作占位
            print(f"发现链接，准备总结: {urls[0]}")

async def setup(bot):
    await bot.add_cog(LinkSummary(bot))
