import datetime
import logging
from discord.ext import commands, tasks
import discord
import asyncio
import aiohttp
from config import SCHEDULED_JOBS_ENABLED, TZ
from core import settings, ai_client
from core.jobs import run_delivery_job
from core.utils import create_ai_embed

logger = logging.getLogger(__name__)

class AIDaily(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._delivery_lock = asyncio.Lock()
        if SCHEDULED_JOBS_ENABLED:
            self.ai_news_daily.start()
        else:
            logger.info("AI 资讯日报定时任务已通过部署配置禁用")

    def cog_unload(self):
        self.ai_news_daily.cancel()

    async def _build_daily_embed(self):
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 1. 获取 HN Top 50 的 ID
            async with session.get("https://hacker-news.firebaseio.com/v0/topstories.json") as response:
                response.raise_for_status()
                story_ids = await response.json()
                if not isinstance(story_ids, list) or not story_ids:
                    raise RuntimeError("Hacker News 未返回 story ID")
                story_ids = story_ids[:30]
            
            # 2. 并发获取文章详情
            async def fetch_story(story_id):
                try:
                    async with session.get(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json") as res:
                        res.raise_for_status()
                        return await res.json()
                except Exception as error:
                    logger.warning("抓取 HN 条目 %s 失败: %s", story_id, error)
                    return None
            
            tasks_list = [fetch_story(sid) for sid in story_ids]
            stories = await asyncio.gather(*tasks_list)
            
        # 3. 过滤有效数据并提取标题和链接
        news_items = []
        for i, item in enumerate(stories):
            if item and 'title' in item:
                # 获取原链接，如果没有则使用 HN 内部链接
                url = item.get('url', f"https://news.ycombinator.com/item?id={item.get('id')}")
                score = item.get('score', 0)
                news_items.append(f"[{i+1}] Title: {item.get('title')} | Score: {score} | URL: {url}")

        if not news_items:
            raise RuntimeError("Hacker News 未返回可用文章")
        
        raw_text = "\n".join(news_items)
        
        system_prompt = (
            "你是一个面向开发者的硬核 AI 技术观察员。用户提供了一批今日 Hacker News 的热门文章列表（包含标题、链接）。\n"
            "请你完成以下两项总结，并使用客观专业的中文回复：\n"
            "1. 【🔥 HN 社区当前最热 Top 5】：直接挑选列表中排在最前面的 5 条新闻，翻译标题并一句话简介。\n"
            "2. 【🤖 开发者 AI 动态】：从列表中，重点筛选出与 AI 开发相关的干货（最多 10 条），例如：大模型发布、开源 AI 项目、机器学习工具更新等。\n\n"
            "重要要求：\n"
            "1. 每条新闻必须严格保留其对应的原始 URL 链接。\n"
            "2. 每条内容必须以 `- ` 开头，严格使用 Markdown 链接语法，例如：`- [中文翻译标题](URL): 一句话简介`。\n"
            "3. 绝对禁止使用 Markdown 表格，也不要使用 HTML 表格；只能使用标题和项目符号列表。\n"
            "4. 只输出一份完整简报，禁止重复板块、重复标题或在后面重写第二版。"
        )
        
        digest = await ai_client.ask_ai(
            raw_text,
            system=system_prompt,
            raise_on_failure=True,
        )
        
        embed = create_ai_embed(
            title="🤖 AI 前沿工具快报 (每日更新) 🚀",
            description=digest,
            color=discord.Color.brand_green()
        )
        
        return embed

    async def _run_daily(self, channel):
        return await run_delivery_job(
            lock=self._delivery_lock,
            task_name="AI 资讯日报生成",
            build=self._build_daily_embed,
            deliver=lambda embed: channel.send(embed=embed),
        )

    @tasks.loop(time=datetime.time(hour=8, minute=15, tzinfo=TZ))
    async def ai_news_daily(self):
        logger.info("执行 AI 资讯日报任务")
        channel_id = settings.get_setting("NEWS_CHANNEL_ID")
        if not channel_id:
            logger.warning("未设置 NEWS_CHANNEL_ID，跳过 AI 日报推送")
            return
            
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            logger.error("找不到配置的频道 ID: %s", channel_id)
            return
            
        try:
            await self._run_daily(channel)
        except Exception:
            logger.exception("AI 资讯日报执行失败")

        
    @ai_news_daily.before_loop
    async def before_ai_news_daily(self):
        await self.bot.wait_until_ready()

    @discord.app_commands.command(name="test_ai_news", description="[管理员] 立即测试 AI 日报推送")
    @discord.app_commands.checks.has_permissions(administrator=True)
    async def test_ai_news(self, interaction: discord.Interaction):
        await interaction.response.send_message("正在为您抓取并生成 AI 日报，请稍等...", ephemeral=True)
        # 手动调用 ai_news_daily 的底层逻辑
        await self.ai_news_daily.coro(self)

async def setup(bot):
    await bot.add_cog(AIDaily(bot))
