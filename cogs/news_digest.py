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

class NewsDigest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily.start()

    def cog_unload(self):
        self.daily.cancel()

    @tasks.loop(time=datetime.time(hour=8, tzinfo=TZ))
    async def daily(self):
        print("执行新闻抓取任务...")
        channel_id = settings.get_setting("NEWS_CHANNEL_ID")
        if not channel_id:
            print("未设置 NEWS_CHANNEL_ID，跳过新闻推送。")
            return
            
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            print(f"找不到配置的频道 ID: {channel_id}")
            return
            
        # 抓取 Google 资讯 (英文源：国际与加拿大新闻，相对客观)
        rss_url = "https://news.google.com/rss/search?q=World+OR+Canada+when:24h&hl=en-CA&gl=CA&ceid=CA:en"
        
        try:
            feed = await asyncio.to_thread(feedparser.parse, rss_url)
            if not feed.entries:
                return
                
            # 提取前 8 条新闻的标题
            news_items = []
            for entry in feed.entries[:8]:
                news_items.append(f"- {entry.title}")
                
            raw_text = "\n".join(news_items)
            
            system_prompt = "你是一个专业的新闻编辑。用户会提供几条今天最新的国际重点事件和加拿大新闻标题。请你筛选其中最重要、最受关注的 3-5 条新闻，用通俗易懂的中文写成一篇简短的“早间新闻速递”。加上适当的 Emoji 并且排版清晰，以友好的口吻向大家问好。"
            
            digest = await ai_client.summarize(raw_text, system=system_prompt)
            
            await channel.send(f"☀️ **大家早上好！这是今天的早间新闻速递** ☕\n\n{digest}")
            
        except Exception as e:
            print(f"执行新闻推送失败: {e}")
        
    @daily.before_loop
    async def before_daily(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(NewsDigest(bot))
