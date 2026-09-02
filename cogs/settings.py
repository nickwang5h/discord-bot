import discord
from discord.ext import commands
from discord import app_commands
from core import settings, ai_client

class SettingsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="set_gemini_key", description="[管理员] 设置 Gemini API Key")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_gemini_key(self, interaction: discord.Interaction, api_key: str):
        # 延迟响应以免超时
        await interaction.response.defer(ephemeral=True)
        
        # 保存设置
        settings.set_secret("GEMINI_API_KEY", api_key)
        
        # 热更新 AI 客户端
        success = ai_client.reload_client()
        
        if success:
            await interaction.followup.send(
                "✅ Gemini API Key 已安全保存并重新加载；可使用 `/health` 检查实时有效性。",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("⚠️ Key 已保存，但加载失败，请检查 Key 是否有效。", ephemeral=True)

    @app_commands.command(name="set_news_channel", description="[管理员] 设置定时新闻推送的频道")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_news_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        settings.set_setting("NEWS_CHANNEL_ID", str(channel.id))
        await interaction.followup.send(f"✅ 已将新闻推送频道设置为 {channel.mention}", ephemeral=True)

    @app_commands.command(name="set_test_news_channel", description="[管理员] 设置高级新闻 (Test News) 推送的频道")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_test_news_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        settings.set_setting("TEST_NEWS_CHANNEL_ID", str(channel.id))
        await interaction.followup.send(f"✅ 已将高级新闻推送频道设置为 {channel.mention}", ephemeral=True)

    @app_commands.command(name="set_model", description="[管理员] 设置全局 AI 模型 (例如 gemini-3.5-flash)")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_model(self, interaction: discord.Interaction, model_name: str):
        await interaction.response.defer(ephemeral=True)
        settings.set_setting("GEMINI_MODEL", model_name)
        await interaction.followup.send(f"✅ 已将默认 AI 模型全局切换为：`{model_name}`\n后续所有回复将使用该模型！", ephemeral=True)

    @app_commands.command(name="set_reading_channel", description="[管理员] 设置每日英文阅读推送的频道")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_reading_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        settings.set_setting("READING_CHANNEL_ID", str(channel.id))
        await interaction.followup.send(f"✅ 已将每日英文阅读推送频道设置为 {channel.mention}", ephemeral=True)

    @app_commands.command(name="set_weather_channel", description="[管理员] 设置每日天气预报推送的频道")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_weather_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        settings.set_setting("WEATHER_CHANNEL_ID", str(channel.id))
        await interaction.followup.send(f"✅ 已将每日天气预报推送频道设置为 {channel.mention}", ephemeral=True)

    @app_commands.command(name="set_weather_cities", description="[管理员] 设置每日天气播报的城市列表 (用逗号分隔)")
    @app_commands.describe(cities="城市列表，例如: Ottawa, Sudbury 或 渥太华, 萨德伯里")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_weather_cities(self, interaction: discord.Interaction, cities: str):
        await interaction.response.defer(ephemeral=True)
        city_list = [c.strip() for c in cities.replace("，", ",").split(",") if c.strip()]
        if not city_list:
            await interaction.followup.send("❌ 城市列表不能为空。", ephemeral=True)
            return
        settings.set_setting("WEATHER_CITIES", city_list)
        formatted = ", ".join(city_list)
        await interaction.followup.send(f"✅ 已将每日天气播报城市更新为：`{formatted}`", ephemeral=True)

    @app_commands.command(name="settings", description="[管理员] 查看当前机器人各项运行配置")
    @app_commands.checks.has_permissions(administrator=True)
    async def view_settings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        current = settings.load_settings()

        def format_channel(key: str) -> str:
            cid = current.get(key)
            if not cid:
                return "➖ *未配置*"
            ch = self.bot.get_channel(int(cid))
            return ch.mention if ch else f"`{cid}` (未缓存)"

        cities = current.get("WEATHER_CITIES", ["Ottawa", "Sudbury"])
        cities_str = ", ".join(str(c) for c in cities) if isinstance(cities, list) else str(cities)
        model = current.get("GEMINI_MODEL", "gemini-3.6-flash")

        weather_cid = current.get("WEATHER_CHANNEL_ID")
        if weather_cid:
            wch = self.bot.get_channel(int(weather_cid))
            weather_val = wch.mention if wch else f"`{weather_cid}`"
        else:
            weather_val = f"{format_channel('NEWS_CHANNEL_ID')} *(继承新闻)*"

        embed = discord.Embed(
            title="⚙️ 机器人当前运行配置",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="📢 频道绑定",
            value=(
                f"- **综合新闻**: {format_channel('NEWS_CHANNEL_ID')}\n"
                f"- **高级精读**: {format_channel('TEST_NEWS_CHANNEL_ID')}\n"
                f"- **每日阅读**: {format_channel('READING_CHANNEL_ID')}\n"
                f"- **天气预报**: {weather_val}"
            ),
            inline=False,
        )
        embed.add_field(
            name="🌤️ 天气播报城市",
            value=f"`{cities_str}`",
            inline=False,
        )
        embed.add_field(
            name="🤖 全局 AI 模型",
            value=f"`{model}`",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(SettingsCog(bot))
