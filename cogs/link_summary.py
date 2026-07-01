import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from urllib.parse import urlparse, parse_qs
import trafilatura
from youtube_transcript_api import YouTubeTranscriptApi
from core import ai_client
from core.utils import create_ai_embed

URL_RE = re.compile(r"https?://\S+")

def extract_video_id(url):
    try:
        query = urlparse(url)
        if query.hostname == 'youtu.be':
            return query.path[1:]
        if query.hostname in ('www.youtube.com', 'youtube.com'):
            if query.path == '/watch':
                return parse_qs(query.query)['v'][0]
            if query.path.startswith('/embed/'):
                return query.path.split('/')[2]
            if query.path.startswith('/v/'):
                return query.path.split('/')[2]
    except Exception:
        pass
    return None

async def fetch_and_summarize(url: str) -> tuple[bool, discord.Embed | str]:
    # 尝试解析 YouTube ID
    video_id = extract_video_id(url)
    text = ""
    is_youtube = False
    
    if video_id:
        is_youtube = True
        try:
            # 抓取 YouTube 字幕
            if hasattr(YouTubeTranscriptApi, 'get_transcript'):
                transcript_list = await asyncio.to_thread(
                    YouTubeTranscriptApi.get_transcript, 
                    video_id, 
                    languages=['zh-Hans', 'zh-Hant', 'en', 'ja', 'ko']
                )
                text = " ".join([i['text'] for i in transcript_list])
            else:
                api = YouTubeTranscriptApi()
                transcript_list = await asyncio.to_thread(
                    api.fetch, 
                    video_id, 
                    languages=['zh-Hans', 'zh-Hant', 'en', 'ja', 'ko']
                )
                text = " ".join([i.text for i in transcript_list])
        except Exception as e:
            print(f"获取 YouTube 字幕失败: {e}")
            return False, "❌ 无法获取该 YouTube 视频的字幕。可能该视频未提供可选字幕。"
    else:
        # 普通网页抓取
        try:
            downloaded = await asyncio.to_thread(trafilatura.fetch_url, url)
            if not downloaded:
                return False, "❌ 无法抓取该网页的内容。"
            
            text = await asyncio.to_thread(trafilatura.extract, downloaded)
        except Exception as e:
            print(f"网页抓取失败: {e}")
            return False, "❌ 抓取网页内容时发生错误。"

    if not text or len(text) < 50:
        return False, "❌ 提取到的内容太少或提取失败，无法进行总结。"

    # 如果文本太长，截断它，防止超出 token 限制
    if len(text) > 20000:
        text = text[:20000]

    # 调用 AI
    prompt_type = "视频" if is_youtube else "网页"
    system_prompt = f"你是一个专业的内容分析助手。请为用户提供这篇{prompt_type}的中文摘要，提取出核心观点和结论，分点列出，保持客观简洁。"
    
    try:
        answer = await ai_client.summarize(text, system=system_prompt)
        embed = create_ai_embed(
            title=f"🔗 {prompt_type}内容总结",
            description=answer,
            color=discord.Color.red() if is_youtube else discord.Color.blue()
        )
        return True, embed
    except Exception as e:
        print(f"AI 总结失败: {e}")
        return False, "❌ AI 总结过程中发生未知错误。"

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
            status_msg = await message.reply("👀 发现链接，正在抓取内容并总结...")
            
            success, result = await fetch_and_summarize(url)
            
            if success:
                await status_msg.edit(content=None, embed=result)
            else:
                await status_msg.edit(content=result)

    @app_commands.command(name="summary", description="一键总结网页长文或 YouTube 视频内容")
    async def summary(self, interaction: discord.Interaction, url: str):
        await interaction.response.send_message("👀 正在尝试获取内容并生成总结，请稍候...")
        
        success, result = await fetch_and_summarize(url)
        
        if success:
            await interaction.edit_original_response(content=f"**提取来源:** {url}", embed=result)
        else:
            await interaction.edit_original_response(content=result)

async def setup(bot):
    await bot.add_cog(LinkSummary(bot))
