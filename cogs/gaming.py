import asyncio
from datetime import datetime
import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import SCHEDULED_JOBS_ENABLED, TZ
from core import settings
from core.gaming import (
    add_to_watchlist,
    build_deal_embed,
    build_epic_free_embed,
    fetch_epic_free_games,
    get_game_deal_info,
    get_watchlist,
    record_notified_deal,
    record_notified_epic,
    remove_from_watchlist,
    search_cheapshark_games,
    should_notify_deal,
    should_notify_epic,
)
from core.jobs import run_delivery_job

logger = logging.getLogger(__name__)


class Gaming(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._delivery_lock = asyncio.Lock()
        if SCHEDULED_JOBS_ENABLED:
            self.epic_weekly.start()
            self.steam_daily.start()
        else:
            logger.info("游戏折扣定时任务已通过部署配置禁用")

    def cog_unload(self):
        self.epic_weekly.cancel()
        self.steam_daily.cancel()

    def _get_target_channel(self) -> discord.abc.Messageable | None:
        channel_id = settings.get_setting("GAMING_CHANNEL_ID") or settings.get_setting("NEWS_CHANNEL_ID")
        if not channel_id:
            logger.warning("未配置 GAMING_CHANNEL_ID 或 NEWS_CHANNEL_ID，跳过游戏特惠推送")
            return None
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            logger.error("找不到配置的频道 ID: %s", channel_id)
            return None
        return channel

    # 每周四 11:30 (美东时间) 触发 Epic 每周喜加一检查与推送
    @tasks.loop(time=[datetime.strptime("11:30", "%H:%M").time().replace(tzinfo=TZ)])
    async def epic_weekly(self):
        now = datetime.now(tz=TZ)
        if now.weekday() != 3:  # 0=周一, 3=周四
            return

        channel = self._get_target_channel()
        if not channel:
            return

        async def build_embed():
            async with aiohttp.ClientSession() as session:
                active, upcoming = await fetch_epic_free_games(session)
            if not active or not should_notify_epic(active):
                return None
            embed = build_epic_free_embed(active, upcoming)
            return embed, active

        async def deliver_payload(payload):
            if not payload:
                return
            embed, active = payload
            await channel.send(
                content="🎁 **Epic 每周限时喜加一提醒已到达！**",
                embed=embed,
            )
            record_notified_epic(active)

        await run_delivery_job(
            lock=self._delivery_lock,
            task_name="Epic 每周喜加一检查",
            build=build_embed,
            deliver=deliver_payload,
        )

    @epic_weekly.before_loop
    async def before_epic_weekly(self):
        await self.bot.wait_until_ready()

    # 每日 13:30 (美东时间) 检查 Steam 愿望单降价与史低变动
    @tasks.loop(time=[datetime.strptime("13:30", "%H:%M").time().replace(tzinfo=TZ)])
    async def steam_daily(self):
        channel = self._get_target_channel()
        if not channel:
            return

        async def check_deals():
            watchlist = get_watchlist()
            notify_deals = []
            async with aiohttp.ClientSession() as session:
                for item in watchlist:
                    game_id = item.get("game_id")
                    if not game_id:
                        continue
                    deal = await get_game_deal_info(game_id, session)
                    if deal and should_notify_deal(deal):
                        notify_deals.append(deal)
                    await asyncio.sleep(0.5)  # 礼貌并发限速
            return notify_deals

        async def deliver_deals(deals):
            if not deals:
                return
            for deal in deals:
                embed = build_deal_embed(deal, is_broadcast=True)
                prefix = "🔥 **愿望单史低特惠！**" if deal.is_historic_low else "🏷️ **愿望单降价通知：**"
                await channel.send(content=prefix, embed=embed)
                record_notified_deal(deal)
                await asyncio.sleep(1.0)

        await run_delivery_job(
            lock=self._delivery_lock,
            task_name="Steam 愿望单折扣检查",
            build=check_deals,
            deliver=deliver_deals,
        )

    @steam_daily.before_loop
    async def before_steam_daily(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="epic_free", description="查询 Epic 游戏商城当前免费游戏及下周预告")
    async def epic_free(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            async with aiohttp.ClientSession() as session:
                active, upcoming = await fetch_epic_free_games(session)
            embed = build_epic_free_embed(active, upcoming)
            await interaction.followup.send(embed=embed)
        except Exception as error:
            logger.exception("获取 Epic 免费游戏失败: %s", error)
            await interaction.followup.send("❌ 获取 Epic 免费游戏信息失败，请稍后再试。")

    @app_commands.command(name="deal", description="查询指定 Steam / PC 游戏最新折扣与历史史低")
    @app_commands.describe(game="游戏英文或常用名称，例如 Cyberpunk 2077, Elden Ring")
    async def deal(self, interaction: discord.Interaction, game: str):
        await interaction.response.defer()
        try:
            async with aiohttp.ClientSession() as session:
                results = await search_cheapshark_games(game, session, limit=1)
                if not results:
                    await interaction.followup.send(
                        f"❌ 未在 CheapShark/Steam 找到名称包含 `{game}` 的游戏，请尝试更准确的英文名。"
                    )
                    return
                game_id = results[0].get("gameID")
                deal_info = await get_game_deal_info(game_id, session)

            if not deal_info:
                await interaction.followup.send(f"❌ 获取游戏 `{game}` 的价格详情失败。")
                return

            embed = build_deal_embed(deal_info, is_broadcast=False)
            await interaction.followup.send(embed=embed)
        except Exception as error:
            logger.exception("查询游戏折扣失败: %s", error)
            await interaction.followup.send(f"❌ 查询游戏折扣出错: {error}")

    @app_commands.command(name="watchlist", description="查看当前监控的 Steam 游戏愿望单与折扣状态")
    async def watchlist(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            items = get_watchlist()
            if not items:
                await interaction.followup.send("当前愿望单为空，管理员可使用 `/watch_game` 添加监控。")
                return

            embed = discord.Embed(
                title="🎮 游戏折扣监控愿望单",
                description=f"当前共监控 **{len(items)}** 款游戏。每日 13:30 自动轮询降价与史低变动。",
                color=discord.Color.dark_purple(),
            )

            async with aiohttp.ClientSession() as session:
                for item in items[:10]:  # 限制最多单次展示前10款
                    game_id = item.get("game_id")
                    title = item.get("title", "未知游戏")
                    deal = await get_game_deal_info(game_id, session) if game_id else None
                    if deal:
                        if deal.savings_percent > 0:
                            status = f"**${deal.sale_price:.2f}** (~~${deal.normal_price:.2f}~~, `-{deal.savings_percent:.0f}%`)"
                            if deal.is_historic_low:
                                status += " 🔥 **史低**"
                        else:
                            status = f"${deal.sale_price:.2f} (原价无折)"
                        hist_str = f" | 史低: ${deal.cheapest_price_ever:.2f}" if deal.cheapest_price_ever else ""
                        value_str = f"• 当前: {status}{hist_str}\n• 平台: `{deal.store_name}` [购买直达]({deal.deal_url})"
                    else:
                        value_str = "• 状态: 无法获取最新价格"
                    embed.add_field(name=f"《{title}》", value=value_str, inline=False)
                    await asyncio.sleep(0.2)

            embed.set_footer(text="提示: 管理员可通过 /watch_game 与 /unwatch_game 增删监控")
            await interaction.followup.send(embed=embed)
        except Exception as error:
            logger.exception("获取愿望单失败: %s", error)
            await interaction.followup.send(f"❌ 获取愿望单失败: {error}")

    @app_commands.command(name="watch_game", description="[管理员] 添加指定游戏到折扣监控愿望单")
    @app_commands.describe(game="游戏英文或标准名称，例如 Hades II, Monster Hunter")
    @app_commands.checks.has_permissions(administrator=True)
    async def watch_game(self, interaction: discord.Interaction, game: str):
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession() as session:
                results = await search_cheapshark_games(game, session, limit=1)
                if not results:
                    await interaction.followup.send(
                        f"❌ 未能检索到匹配的游戏 `{game}`，请尝试更准确的英文名。",
                        ephemeral=True,
                    )
                    return
                matched = results[0]
                title = matched.get("external") or matched.get("internalName") or game
                game_id = matched.get("gameID")
                steam_app_id = matched.get("steamAppID")

                added = add_to_watchlist(title, game_id, steam_app_id)
                if added:
                    await interaction.followup.send(
                        f"✅ 成功将 **《{title}》** (GameID: `{game_id}`) 加入折扣监控愿望单！",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        f"⚠️ **《{title}》** 已经在监控愿望单中，无需重复添加。",
                        ephemeral=True,
                    )
        except Exception as error:
            logger.exception("添加监控游戏失败: %s", error)
            await interaction.followup.send(f"❌ 添加失败: {error}", ephemeral=True)

    @app_commands.command(name="unwatch_game", description="[管理员] 从折扣监控愿望单移除指定游戏")
    @app_commands.describe(game="愿望单中的游戏名称或 GameID")
    @app_commands.checks.has_permissions(administrator=True)
    async def unwatch_game(self, interaction: discord.Interaction, game: str):
        await interaction.response.defer(ephemeral=True)
        try:
            removed = remove_from_watchlist(game)
            if removed:
                await interaction.followup.send(
                    f"✅ 已成功将 `{game}` 从监控愿望单移除。",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"⚠️ 未在监控愿望单中找到匹配 `{game}` 的游戏。",
                    ephemeral=True,
                )
        except Exception as error:
            logger.exception("移除监控游戏失败: %s", error)
            await interaction.followup.send(f"❌ 移除失败: {error}", ephemeral=True)

    @app_commands.command(name="test_gaming", description="[管理员] 立即测试游戏折扣与 Epic 喜加一播报")
    @app_commands.checks.has_permissions(administrator=True)
    async def test_gaming(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession() as session:
                active, upcoming = await fetch_epic_free_games(session)
                epic_embed = build_epic_free_embed(active, upcoming)

                watchlist = get_watchlist()
                sample_deal_embed = None
                if watchlist:
                    sample_id = watchlist[0].get("game_id")
                    sample_deal = await get_game_deal_info(sample_id, session) if sample_id else None
                    if sample_deal:
                        sample_deal_embed = build_deal_embed(sample_deal, is_broadcast=True)

            embeds = [epic_embed]
            if sample_deal_embed:
                embeds.append(sample_deal_embed)

            await interaction.followup.send(
                content="🎮 **游戏特惠与 Epic 喜加一测试预览：**",
                embeds=embeds,
                ephemeral=True,
            )
        except Exception as error:
            logger.exception("测试游戏播报失败: %s", error)
            await interaction.followup.send(f"❌ 测试失败: {error}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Gaming(bot))
