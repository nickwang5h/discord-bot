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
            
            system_prompt = "你是一个专业的 AI 观察员。用户会提供几条今天最新的 AI 相关新闻标题。请你重点筛选出关于【新AI工具】、【大模型进展】或【AI行业动态】的 3-5 条新闻。请用简明扼要、客观专业的中文进行总结。\\n请严格使用结构化的简报格式，例如：\\n- **[新闻主题]**：一两句话概括核心内容。\\n不要使用过度夸张的语气词，直接输出结构化的新闻列表即可。"
            
            digest = await ai_client.summarize(raw_text, system=system_prompt)
            
            embed = create_ai_embed(
                title="🤖 AI 前沿工具快报 (每日更新) 🚀",
                description=digest,
                color=discord.Color.brand_green()
            )
            
            await channel.send(embed=embed)
            
        except Exception as e:
            print(f"执行 AI 日报推送失败: {e}")
        
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
