import datetime
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo
from discord.ext import commands, tasks
import discord
import asyncio
import feedparser
from core import settings, ai_client
from core.utils import create_ai_embed

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
            
        # 抓取 Google 资讯 (加拿大与国际的 Top Stories)
        rss_url = "https://news.google.com/rss?hl=en-CA&gl=CA&ceid=CA:en"
        
        try:
            feed = await asyncio.to_thread(feedparser.parse, rss_url)
            if not feed.entries:
                return
                
            # 提取前 8 条新闻的标题
            news_items = []
            for entry in feed.entries[:8]:
                news_items.append(f"- {entry.title}")
                
            raw_text = "\n".join(news_items)
            
            system_prompt = "你是一个专业的新闻编辑。用户会提供几条今天的头条新闻标题（包含国际大事和加拿大新闻）。请你挑选其中最重要的 3-5 条，用客观、简练的中文写成早报。\\n请严格使用结构化的简报格式，例如：\\n- **[新闻主题]**：用一两句话总结核心内容。\\n不要写长篇大论，直接输出结构化的新闻列表，可以带少量 Emoji。"
            
            digest = await ai_client.summarize(raw_text, system=system_prompt)
            
            embed = create_ai_embed(
                title="☀️ 早上好！早间新闻速递 ☕",
                description=digest,
                color=discord.Color.gold()
            )
            
            await channel.send(embed=embed)
            
        except Exception as e:
            print(f"执行新闻推送失败: {e}")
        
    @daily.before_loop
    async def before_daily(self):
        await self.bot.wait_until_ready()

    @discord.app_commands.command(name="test_news", description="[管理员] 立即测试早间新闻推送")
    @discord.app_commands.checks.has_permissions(administrator=True)
    async def test_news(self, interaction: discord.Interaction):
        await interaction.response.send_message("正在为您抓取并生成早间新闻，请稍等...", ephemeral=True)
        # 手动调用 daily 的底层逻辑
        await self.daily.coro(self)

async def setup(bot):
    await bot.add_cog(NewsDigest(bot))
