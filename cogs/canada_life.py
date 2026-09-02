import datetime
import logging
from typing import Literal

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import TZ

logger = logging.getLogger(__name__)


def _get_easter(year: int) -> datetime.date:
    """计算指定年份的复活节星期日 (Anonymous Gregorian algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


def _get_nth_weekday_of_month(year: int, month: int, nth: int, weekday: int) -> datetime.date:
    """计算某月第 n 个周几 (weekday: 0=周一, 6=周日)."""
    first_day = datetime.date(year, month, 1)
    offset = (weekday - first_day.weekday()) % 7
    first_target = first_day + datetime.timedelta(days=offset)
    return first_target + datetime.timedelta(weeks=nth - 1)


def _get_monday_before(year: int, month: int, day: int) -> datetime.date:
    """计算指定日期之前的最近一个周一 (例如 Victoria Day 为 5 月 25 日前的周一)."""
    d = datetime.date(year, month, day)
    while d.weekday() != 0:
        d -= datetime.timedelta(days=1)
    return d


def get_canada_holidays(year: int) -> list[tuple[datetime.date, str, str]]:
    easter = _get_easter(year)
    good_friday = easter - datetime.timedelta(days=2)
    easter_monday = easter + datetime.timedelta(days=1)

    holidays = [
        (datetime.date(year, 1, 1), "元旦 (New Year's Day)", "联邦/安省"),
        (_get_nth_weekday_of_month(year, 2, 3, 0), "家庭日 (Family Day)", "安省法定"),
        (good_friday, "受难日 (Good Friday)", "联邦/安省"),
        (easter_monday, "复活节星期一 (Easter Monday)", "联邦公休"),
        (_get_monday_before(year, 5, 25), "维多利亚日 (Victoria Day)", "联邦/安省"),
        (datetime.date(year, 7, 1), "加拿大国庆日 (Canada Day)", "联邦/安省"),
        (_get_nth_weekday_of_month(year, 8, 1, 0), "市民节 / 拜沃德日 (Civic Holiday)", "安省/渥太华"),
        (_get_nth_weekday_of_month(year, 9, 1, 0), "劳动节 (Labour Day)", "联邦/安省"),
        (datetime.date(year, 9, 30), "国家真相与和解日 (Truth and Reconciliation)", "联邦法定"),
        (_get_nth_weekday_of_month(year, 10, 2, 0), "感恩节 (Thanksgiving)", "联邦/安省"),
        (datetime.date(year, 11, 11), "国殇日 (Remembrance Day)", "联邦法定"),
        (datetime.date(year, 12, 25), "圣诞节 (Christmas Day)", "联邦/安省"),
        (datetime.date(year, 12, 26), "节礼日 (Boxing Day)", "安省法定"),
    ]
    holidays.sort(key=lambda x: x[0])
    return holidays


class CanadaLife(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="fx", description="实时汇率换算 (CAD / USD / CNY)")
    @app_commands.describe(
        amount="换算金额 (默认 100)",
        currency="基础货币类型 (默认 CAD)",
    )
    async def fx(
        self,
        interaction: discord.Interaction,
        amount: float = 100.0,
        currency: Literal["CAD", "USD", "CNY"] = "CAD",
    ):
        await interaction.response.defer()
        if amount <= 0:
            await interaction.followup.send("❌ 金额必须大于 0。")
            return

        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                url = "https://api.frankfurter.dev/v1/latest?from=CAD&to=USD,CNY"
                async with session.get(url) as response:
                    response.raise_for_status()
                    d = await response.json()

            rates = d["rates"]
            cad_to_usd = float(rates["USD"])
            cad_to_cny = float(rates["CNY"])
            usd_to_cad = 1.0 / cad_to_usd
            cny_to_cad = 1.0 / cad_to_cny
            usd_to_cny = cad_to_cny / cad_to_usd
            rate_date = d.get("date", "最新")

            embed = discord.Embed(
                title="💱 货币汇率换算",
                color=discord.Color.gold(),
            )

            if currency == "CAD":
                conv_usd = amount * cad_to_usd
                conv_cny = amount * cad_to_cny
                embed.description = (
                    f"### 💵 **{amount:,.2f} CAD**\n"
                    f"≈ **{conv_usd:,.2f} USD**\n"
                    f"≈ **{conv_cny:,.2f} CNY (人民币)**"
                )
            elif currency == "USD":
                conv_cad = amount * usd_to_cad
                conv_cny = amount * usd_to_cny
                embed.description = (
                    f"### 💵 **{amount:,.2f} USD**\n"
                    f"≈ **{conv_cad:,.2f} CAD (加元)**\n"
                    f"≈ **{conv_cny:,.2f} CNY (人民币)**"
                )
            else:  # CNY
                conv_cad = amount * cny_to_cad
                conv_usd = amount * (cad_to_usd / cad_to_cny)
                embed.description = (
                    f"### 💴 **{amount:,.2f} CNY (人民币)**\n"
                    f"≈ **{conv_cad:,.2f} CAD (加元)**\n"
                    f"≈ **{conv_usd:,.2f} USD (美元)**"
                )

            embed.add_field(
                name="📊 核心参考基准",
                value=(
                    f"• 1 CAD = `{cad_to_usd:.4f}` USD *(1 USD = `{usd_to_cad:.4f}` CAD)*\n"
                    f"• 1 CAD = `{cad_to_cny:.4f}` CNY *(100 CNY = `{cny_to_cad * 100:.2f}` CAD)*\n"
                    f"• 1 USD = `{usd_to_cny:.4f}` CNY"
                ),
                inline=False,
            )
            embed.set_footer(text=f"数据基准: 欧洲央行 (ECB) 官方参考汇率 · {rate_date}")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.exception("获取汇率失败: %s", e)
            await interaction.followup.send("❌ 获取汇率失败，请稍后重试。")

    @app_commands.command(name="holidays", description="查询加拿大与安省法定节假日及长周末倒计时")
    async def holidays(self, interaction: discord.Interaction):
        await interaction.response.defer()
        today = datetime.datetime.now(TZ).date()
        year = today.year
        all_holidays = get_canada_holidays(year)

        upcoming = [(d, name, scope) for d, name, scope in all_holidays if d >= today]
        if not upcoming:
            # 如果今年已过完，加载下一年的节假日
            all_holidays = get_canada_holidays(year + 1)
            upcoming = [(d, name, scope) for d, name, scope in all_holidays if d >= today]

        next_d, next_name, next_scope = upcoming[0]
        days_left = (next_d - today).days

        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        next_weekday = weekday_names[next_d.weekday()]
        is_long_weekend = next_d.weekday() in (0, 4)  # 周一或周五即构成长周末

        embed = discord.Embed(
            title=f"🍁 加拿大与安省节假日日历 ({year})",
            color=discord.Color.red(),
        )

        lw_tag = " · 🎉 **长周末 (Long Weekend)**" if is_long_weekend else ""
        if days_left == 0:
            countdown_str = "🎉 **就是今天！享受假期！**"
        elif days_left == 1:
            countdown_str = f"⏳ **明天到来！** ({next_d.isoformat()} {next_weekday}{lw_tag})"
        else:
            countdown_str = f"⏳ 距离 **{next_name}** 还有 **{days_left}** 天 ({next_d.isoformat()} {next_weekday}{lw_tag})"

        embed.add_field(
            name="🔔 下一个法定节假日",
            value=f"### {next_name} [{next_scope}]\n{countdown_str}",
            inline=False,
        )

        lines = []
        for d, name, scope in all_holidays:
            diff = (d - today).days
            w = weekday_names[d.weekday()]
            if diff < 0:
                lines.append(f"~~`{d.strftime('%m-%d')}` {name} ({w})~~ *[已过]*")
            elif diff == 0:
                lines.append(f"👉 **`{d.strftime('%m-%d')}` {name} ({w}) [今天]**")
            else:
                star = "⭐ " if diff <= 30 else ""
                lines.append(f"{star}`{d.strftime('%m-%d')}` **{name}** ({w}) — *还有 {diff} 天*")

        embed.add_field(
            name="📅 全年节假日总览 (安省 / 联邦)",
            value="\n".join(lines),
            inline=False,
        )
        embed.set_footer(text="规则参考: Employment Standards Act (Ontario) & Canada Labour Code")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(CanadaLife(bot))
