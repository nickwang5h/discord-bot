import datetime
import logging
from discord.ext import commands, tasks
import discord
import asyncio
from config import TZ
from core import settings, ai_client
from core.feeds import FeedSource, fetch_feeds
from core.jobs import run_delivery_job
from core.utils import create_ai_embed

logger = logging.getLogger(__name__)

class NewsDigest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._delivery_lock = asyncio.Lock()
        self.daily.start()

    def cog_unload(self):
        self.daily.cancel()

    async def _build_news_digest(self, time_name, greeting):
        logger.info("正在从高质量新闻源抓取新闻")

        # 高质量中立源 (支持同类别多源比对)
        feeds = [
            FeedSource("World", "https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World"),
            FeedSource("Canada", "https://globalnews.ca/canada/feed/", "Global News"),
            FeedSource("Finance", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "WSJ Markets"),
            FeedSource("Finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml?profile=120000000&id=100003114", "CNBC"),
            FeedSource("Finance", "https://finance.yahoo.com/news/rss", "Yahoo Finance"),
        ]

        feed_items = await fetch_feeds(feeds, max_age_seconds=86400, max_items_per_source=8)
        news_items = [f"[{item.category}] - {item.title} ({item.url})" for item in feed_items]

        if not news_items:
            raise RuntimeError("所有新闻源均未返回可用条目")
            
        raw_text = f"这是今天从各高质量信息源抓取的新闻列表：\n" + "\n".join(news_items) + f"\n\n请严格基于这些新闻为我生成今天的{time_name}新闻简报。"
        
        system_prompt = (
            "你是一个专业的新闻主编。\n"
            "必须严格按照以下三个板块进行分类（如果某板块没有新闻，可将其合并或略过）：\n"
            "1. 🌍 国际要闻\n"
            "2. 🍁 加拿大新闻\n"
            "3. 📈 金融市场\n"
            "要求：\n"
            "1. 每个板块精选出 5 到 7 条最具价值的头条新闻。对于【金融市场】板块，列表中包含了多家不同媒体的交叉报道，请你综合比对去重，提取出最有共识的市场大事件（例如多家媒体同时报道的暴跌或收购）。\n"
            "2. 每条新闻必须附带来源 URL，并严格使用 Markdown 语法：`- [新闻极简标题](URL): 一句话新闻摘要`。\n"
            "3. 冒号后面的「一句话新闻摘要」要求信息量大、有深度，说明这起事件的影响或核心看点（类似 Hacker News 的硬核摘要风格），不要只重复标题。\n"
            "4. 每条新闻必须以 `- ` 开头。绝对禁止使用 Markdown 或 HTML 表格，只能使用板块标题和项目符号列表。\n"
            "5. 只输出一份完整简报，禁止重复板块、重复标题或在后面重写第二版。\n"
            "注意：总字数必须控制以适应 Discord 消息长度限制。"
        )
        
        digest = await ai_client.ask_ai(
            raw_text,
            system=system_prompt,
            use_search=False,
            raise_on_failure=True,
        )
        
        embed = create_ai_embed(
            title=greeting,
            description=digest,
            color=discord.Color.gold()
        )
        
        return embed

    async def _run_news_digest(self, channel, time_name, greeting):
        return await run_delivery_job(
            lock=self._delivery_lock,
            task_name=f"{time_name}新闻生成",
            build=lambda: self._build_news_digest(time_name, greeting),
            deliver=lambda embed: channel.send(embed=embed),
        )

    @tasks.loop(time=[
        datetime.time(hour=8, minute=45, tzinfo=TZ),
        datetime.time(hour=15, minute=30, tzinfo=TZ)
    ])
    async def daily(self):
        now = datetime.datetime.now(tz=TZ)
        is_morning = now.hour < 12
        time_name = "早间" if is_morning else "午后"
        greeting = "☀️ 早上好！早间新闻速递 ☕" if is_morning else "☕ 下午好！午后新闻速递 📰"
        
        logger.info("执行%s新闻抓取任务", time_name)
        channel_id = settings.get_setting("NEWS_CHANNEL_ID")
        if not channel_id:
            logger.warning("未设置 NEWS_CHANNEL_ID，跳过新闻推送")
            return
            
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            logger.error("找不到配置的频道 ID: %s", channel_id)
            return
            
        try:
            await self._run_news_digest(channel, time_name, greeting)
        except Exception:
            logger.exception("%s新闻任务执行失败", time_name)
        
    @daily.before_loop
    async def before_daily(self):
        await self.bot.wait_until_ready()

    @discord.app_commands.command(name="test_news", description="[管理员] 立即测试新闻推送")
    @discord.app_commands.checks.has_permissions(administrator=True)
    async def test_news(self, interaction: discord.Interaction):
        await interaction.response.send_message("正在为您抓取并生成新闻简报，请稍等...", ephemeral=True)
        # 手动调用 daily 的底层逻辑
        await self.daily.coro(self)

async def setup(bot):
    await bot.add_cog(NewsDigest(bot))
