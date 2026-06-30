import datetime
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo
from discord.ext import commands, tasks
import asyncio
import feedparser
from core import settings, ai_client

TZ = zoneinfo.ZoneInfo("America/Toronto")

class AIDaily(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ai_news_daily.start()

    def cog_unload(self):
        self.ai_news_daily.cancel()

    @tasks.loop(time=datetime.time(hour=9, tzinfo=TZ))
    async def ai_news_daily(self):
        print("执行 AI 资讯日报任务...")
        channel_id = settings.get_setting("NEWS_CHANNEL_ID")
        if not channel_id:
            print("未设置 NEWS_CHANNEL_ID，跳过 AI 日报推送。")
            return
            
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            print(f"找不到配置的频道 ID: {channel_id}")
            return
            
        # 抓取 Google 资讯 (英文源：AI工具/模型/概念，相对客观)
        rss_url = "https://news.google.com/rss/search?q=Artificial+Intelligence+tools+OR+models+OR+AI+concepts+when:24h&hl=en-US&gl=US&ceid=US:en"
        
        try:
            feed = await asyncio.to_thread(feedparser.parse, rss_url)
            if not feed.entries:
                return
                
            # 提取前 10 条新闻的标题，供 AI 筛选
            news_items = []
            for entry in feed.entries[:10]:
                news_items.append(f"- {entry.title}")
                
            raw_text = "\n".join(news_items)
            
            system_prompt = "你是一个紧跟潮流的 AI 极客体验官。用户会提供几条今天最新的 AI 新闻标题。请你重点筛选出关于【新诞生的AI工具】、【热门大模型表现】或【新奇有趣AI概念】的 3-5 条新闻，写成一篇深入浅出的中文“AI 前沿快报”。排版要清晰现代，用生动幽默、充满好奇心的语气为大家点评这些新玩意儿到底怎么样。"
            
            digest = await ai_client.summarize(raw_text, system=system_prompt)
            
            await channel.send(f"🤖 **AI 前沿工具快报 (每日更新)** 🚀\n\n{digest}")
            
        except Exception as e:
            print(f"执行 AI 日报推送失败: {e}")
        
    @ai_news_daily.before_loop
    async def before_ai_news_daily(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(AIDaily(bot))
