import datetime
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo
from discord.ext import commands, tasks
import discord
import asyncio
from core import settings, ai_client
from core.utils import create_ai_embed

TZ = zoneinfo.ZoneInfo("America/Toronto")

class NewsDigest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily.start()

    def cog_unload(self):
        self.daily.cancel()

    @tasks.loop(time=[
        datetime.time(hour=8, tzinfo=TZ),
        datetime.time(hour=15, minute=30, tzinfo=TZ)
    ])
    async def daily(self):
        now = datetime.datetime.now(tz=TZ)
        is_morning = now.hour < 12
        time_name = "早间" if is_morning else "午后"
        greeting = "☀️ 早上好！早间新闻速递 ☕" if is_morning else "☕ 下午好！午后新闻速递 📰"
        
        print(f"执行{time_name}新闻抓取任务...")
        channel_id = settings.get_setting("NEWS_CHANNEL_ID")
        if not channel_id:
            print("未设置 NEWS_CHANNEL_ID，跳过新闻推送。")
            return
            
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            print(f"找不到配置的频道 ID: {channel_id}")
            return
            
        try:
            print("正在从高质量新闻源抓取新闻...")
            import feedparser
            
            # 高质量中立源 (去除科技类，因为已经在 ai_daily.py 处理)
            feeds = {
                "World": "https://feeds.bbci.co.uk/news/world/rss.xml",
                "Canada": "https://globalnews.ca/canada/feed/",
                "Finance": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"
            }
            
            news_items = []
            
            async def fetch_feed(category, url):
                try:
                    feed = await asyncio.to_thread(feedparser.parse, url)
                    # 每个源取前 10 条，保证 AI 有充足的高质量信息进行总结
                    return [f"[{category}] - {entry.title} ({entry.link})" for entry in feed.entries[:10]]
                except Exception as e:
                    print(f"抓取 {category} 失败: {e}")
                    return []

            tasks_list = [fetch_feed(cat, url) for cat, url in feeds.items()]
            results = await asyncio.gather(*tasks_list)
            
            for items in results:
                news_items.extend(items)
                
            raw_text = f"这是今天从各高质量信息源抓取的新闻列表：\n" + "\n".join(news_items) + f"\n\n请严格基于这些新闻为我生成今天的{time_name}新闻简报。"
            
            system_prompt = (
                "你是一个专业的新闻主编。\n"
                "必须严格按照以下三个板块进行分类（如果某板块没有新闻，可将其合并或略过）：\n"
                "1. 🌍 国际要闻\n"
                "2. 🍁 加拿大新闻\n"
                "3. 📈 金融市场\n"
                "要求：\n"
                "1. 每个板块精选最具价值的新闻。\n"
                "2. 每条新闻必须极度简短（控制在20字以内的核心一句话总结）。\n"
                "3. 每条新闻必须附带来源 URL，并使用 Markdown 语法：`- [新闻极简标题](URL)`。\n"
                "注意：总字数必须严格控制以适应 Discord 消息长度限制。"
            )
            
            digest = await ai_client.ask_ai(raw_text, system=system_prompt, use_search=False)
            
            embed = create_ai_embed(
                title=greeting,
                description=digest,
                color=discord.Color.gold()
            )
            
            await channel.send(embed=embed)
            
        except Exception as e:
            print(f"执行新闻推送失败: {e}")
        
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
