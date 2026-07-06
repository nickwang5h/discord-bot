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
            
        try:
            raw_text = "请为我生成今天的早间新闻简报。"
            
            system_prompt = (
                "你是一个专业的新闻主编。请使用 Google 搜索获取过去 24 小时的最新重大新闻。\n"
                "必须严格按照以下四个板块进行分类：\n"
                "1. 🌍 国际要闻\n"
                "2. 🍁 加拿大新闻\n"
                "3. 💻 科技动态\n"
                "4. 📈 金融市场\n"
                "要求：\n"
                "1. 每个板块必须精选 5 条最具价值的新闻（总计严格为 20 条）。\n"
                "2. 每条新闻必须极度简短（控制在20字以内的核心一句话总结）。\n"
                "3. 每条新闻必须附带来源 URL，并使用 Markdown 语法：`- [新闻极简标题](URL)`。\n"
                "注意：总字数必须严格控制以适应 Discord 消息长度限制。"
            )
            
            digest = await ai_client.ask_ai(raw_text, system=system_prompt, use_search=True)
            
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
