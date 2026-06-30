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
        settings.set_setting("GEMINI_API_KEY", api_key)
        
        # 热更新 AI 客户端
        success = ai_client.reload_client()
        
        if success:
            await interaction.followup.send("✅ Gemini API Key 设置成功并已生效！", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ Key 已保存，但加载失败，请检查 Key 是否有效。", ephemeral=True)

    @set_gemini_key.error
    async def set_gemini_key_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 您没有权限使用此命令，仅限管理员使用。", ephemeral=True)
            else:
                await interaction.followup.send("❌ 您没有权限使用此命令，仅限管理员使用。", ephemeral=True)

    @app_commands.command(name="set_news_channel", description="[管理员] 设置定时新闻推送的频道")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_news_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        settings.set_setting("NEWS_CHANNEL_ID", str(channel.id))
        await interaction.followup.send(f"✅ 已将新闻推送频道设置为 {channel.mention}", ephemeral=True)

    @set_news_channel.error
    async def set_news_channel_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 您没有权限使用此命令，仅限管理员使用。", ephemeral=True)
            else:
                await interaction.followup.send("❌ 您没有权限使用此命令，仅限管理员使用。", ephemeral=True)
    @app_commands.command(name="set_model", description="[管理员] 设置全局 AI 模型 (例如 gemini-3.5-flash)")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_model(self, interaction: discord.Interaction, model_name: str):
        await interaction.response.defer(ephemeral=True)
        settings.set_setting("GEMINI_MODEL", model_name)
        await interaction.followup.send(f"✅ 已将默认 AI 模型全局切换为：`{model_name}`\n后续所有回复将使用该模型！", ephemeral=True)

    @set_model.error
    async def set_model_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ 您没有权限使用此命令，仅限管理员使用。", ephemeral=True)
            else:
                await interaction.followup.send("❌ 您没有权限使用此命令，仅限管理员使用。", ephemeral=True)

async def setup(bot):
    await bot.add_cog(SettingsCog(bot))
