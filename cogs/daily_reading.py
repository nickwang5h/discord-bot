import datetime
import zoneinfo
from discord.ext import commands, tasks
import discord
import asyncio
from core import settings, ai_client
from core.utils import create_ai_embed
import random

TZ = zoneinfo.ZoneInfo("America/Toronto")

class DailyReading(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.reading_loop.start()

    def cog_unload(self):
        self.reading_loop.cancel()

    @tasks.loop(time=[datetime.time(hour=7, minute=30, tzinfo=TZ)])
    async def reading_loop(self):
        print("执行每日英文阅读推送任务...")
        channel_id = settings.get_setting("READING_CHANNEL_ID")
        if not channel_id:
            print("未设置 READING_CHANNEL_ID，跳过每日英文阅读推送。")
            return
            
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            print(f"找不到配置的频道 ID: {channel_id}")
            return

        try:
            # 1. 实用场景
            scenario_task = self.generate_scenario()
            # 2. RSS 真实语料
            rss_task = self.generate_rss_reading()
            # 3. 名言金句
            quote_task = self.generate_quote()

            results = await asyncio.gather(scenario_task, rss_task, quote_task, return_exceptions=True)

            titles = ["🗣️ 每日英语：实用场景", "📰 每日英语：外刊精读", "💡 每日英语：金句赏析"]
            colors = [discord.Color.blue(), discord.Color.green(), discord.Color.purple()]

            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    print(f"生成阅读卡片 {i} 失败: {result}")
                    continue
                
                if result:
                    embed = create_ai_embed(
                        title=titles[i],
                        description=result,
                        color=colors[i]
                    )
                    message = await channel.send(embed=embed)
                    # 添加打卡 reaction
                    await message.add_reaction("✅")
                    await asyncio.sleep(1) # 避免速率限制
                    
        except Exception as e:
            print(f"执行阅读推送失败: {e}")

    async def generate_scenario(self):
        system_prompt = (
            "你是一个专业的英语老师。请生成一段简短的职场或生活实用英语对话/短文，长度约 150 词。\n"
            "要求：\n"
            "1. 每天选择一个不同的随机场景（例如：星巴克点单、委婉拒绝会议、请求延期、茶水间闲聊等）。首先用一句中文说明今天的场景。\n"
            "2. 提供纯英文的正文内容，难度控制在雅思 6.0 (CEFR B2) 左右，表达地道。\n"
            "3. 在文末提取 3-5 个核心实用词汇或短语，提供中文解释。\n"
            "4. 严格使用 Markdown 格式，不要加多余的寒暄语。"
        )
        return await ai_client.ask_ai("请生成今天的实用场景英语阅读素材。", system=system_prompt, use_search=False)

    async def generate_rss_reading(self):
        try:
            import feedparser
            # 这里选取 Lifehacker 或者 NPR
            urls = [
                "https://feeds.npr.org/1004/rss.xml", # NPR World
                "https://feeds.npr.org/1048/rss.xml", # NPR Science
                "https://feeds.npr.org/1046/rss.xml"  # NPR Pop Culture
            ]
            url = random.choice(urls)
            feed = await asyncio.to_thread(feedparser.parse, url)
            
            if not feed.entries:
                return None
                
            entry = feed.entries[0]
            raw_text = f"Title: {entry.title}\nLink: {entry.link}\nSummary: {entry.summary if hasattr(entry, 'summary') else ''}"
            
            system_prompt = (
                "你是一个专业的英语外刊精读老师。\n"
                "我给你提供了一篇真实外媒新闻的标题和摘要。请你：\n"
                "1. 在开头附上新闻标题和链接。\n"
                "2. 提取或改写一段约 150 词的精彩英文引言/正文，保持原汁原味。\n"
                "3. 在文末提供一句话的中文核心大意总结。\n"
                "4. 提取 2-3 个好词好句并作中文解释。\n"
                "5. 严格使用 Markdown 格式。"
            )
            return await ai_client.ask_ai(raw_text, system=system_prompt, use_search=False)
        except Exception as e:
            print(f"抓取或生成 RSS 阅读失败: {e}")
            return None

    async def generate_quote(self):
        system_prompt = (
            "你是一个充满智慧的英语老师。\n"
            "请挑选一句经典的英文名言、TED 演讲金句或名著摘录。\n"
            "要求：\n"
            "1. 提供英文原句和出处（作者/演讲者）。\n"
            "2. 提供优美的中文翻译。\n"
            "3. 写一段 100 词左右的英文赏析或启示（Insight），文字优美且富有启发性。\n"
            "4. 严格使用 Markdown 格式。"
        )
        return await ai_client.ask_ai("请生成今天的金句赏析素材。", system=system_prompt, use_search=False)

    @reading_loop.before_loop
    async def before_reading_loop(self):
        await self.bot.wait_until_ready()

    @discord.app_commands.command(name="test_reading", description="[管理员] 立即测试每日英文阅读推送")
    @discord.app_commands.checks.has_permissions(administrator=True)
    async def test_reading(self, interaction: discord.Interaction):
        await interaction.response.send_message("正在为您生成每日阅读材料，请稍等...", ephemeral=True)
        await self.reading_loop.coro(self)

async def setup(bot):
    await bot.add_cog(DailyReading(bot))
