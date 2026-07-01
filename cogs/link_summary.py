import re
import asyncio
import discord
from discord.ext import commands
import trafilatura
from core import ai_client
from core.utils import create_ai_embed

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
            url = urls[0]
            # 给出响应提示
            status_msg = await message.reply("👀 发现链接，正在抓取内容并总结...")
            
            try:
                # 抓取网页 (在单独的线程中以防阻塞)
                downloaded = await asyncio.to_thread(trafilatura.fetch_url, url)
                if not downloaded:
                    await status_msg.edit(content="❌ 无法抓取该网页的内容。")
                    return
                
                text = await asyncio.to_thread(trafilatura.extract, downloaded)
                if not text or len(text) < 50:
                    await status_msg.edit(content="❌ 网页内容太少或提取失败，无法总结。")
                    return
                
                # 调用 AI
                system_prompt = "你是一个专业的内容分析助手。请为用户提供这篇网页正文的中文摘要，提取出核心观点和结论，分点列出，保持客观简洁。"
                
                # 如果文本太长，截断它，防止超出 token 限制
                if len(text) > 20000:
                    text = text[:20000]
                    
                answer = await ai_client.summarize(text, system=system_prompt)
                
                embed = create_ai_embed(
                    title="🔗 网页内容总结",
                    description=answer,
                    color=discord.Color.blue()
                )
                
                await status_msg.edit(content=None, embed=embed)
                
            except Exception as e:
                print(f"总结链接时出错: {e}")
                await status_msg.edit(content="❌ 处理此链接时遇到错误。")

async def setup(bot):
    await bot.add_cog(LinkSummary(bot))
