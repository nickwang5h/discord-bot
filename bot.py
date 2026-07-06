import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv(override=True)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"[{bot.user}] 正在加载 Cogs...")
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py') and filename != '__init__.py':
            try:
                await bot.load_extension(f'cogs.{filename[:-3]}')
                print(f"成功加载: cogs.{filename[:-3]}")
            except Exception as e:
                print(f"加载 cogs.{filename[:-3]} 失败: {e}")
                
    try:
        await bot.tree.sync()
        print("斜杠命令同步完成")
    except Exception as e:
        print(f"斜杠命令同步失败: {e}")
        
    print(f"上线啦：{bot.user}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    if isinstance(error, discord.app_commands.CommandOnCooldown):
        await interaction.response.send_message(f"⏳ 技能冷却中，请在 {error.retry_after:.1f} 秒后再试。", ephemeral=True)
    else:
        print(f"Command Error: {error}")
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message("❌ 发生错误，无法执行命令。", ephemeral=True)
            except discord.errors.InteractionResponded:
                pass

@bot.tree.command(name="ping", description="测试 bot 是否存活")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong 🏓")

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token or token == "your_discord_bot_token_here":
        print("请在 .env 文件中配置有效的 DISCORD_TOKEN")
    else:
        bot.run(token)
