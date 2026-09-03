import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
import discord

from config import STATE_ROOT, TZ
from core.storage import JsonStore

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "DiscordAIBot/1.0 (+gaming deal monitor)",
    "Accept": "application/json",
}

CHEAPSHARK_STORES = {
    "1": "Steam",
    "2": "GamersGate",
    "3": "GreenManGaming",
    "7": "GOG",
    "11": "Humble Store",
    "25": "Epic Games Store",
    "31": "Blizzard",
}

DEFAULT_WATCHLIST = [
    {"title": "Cyberpunk 2077", "game_id": "202350", "steam_app_id": "1091500"},
    {"title": "Elden Ring", "game_id": "233379", "steam_app_id": "1245620"},
    {"title": "Baldur's Gate 3", "game_id": "214041", "steam_app_id": "1086940"},
    {"title": "Black Myth: Wukong", "game_id": "278278", "steam_app_id": "2358720"},
]

_watchlist_store = JsonStore(STATE_ROOT / "data/gaming_watchlist.json", list)
_deals_cache_store = JsonStore(STATE_ROOT / "data/gaming_deals_cache.json", dict)
_epic_cache_store = JsonStore(STATE_ROOT / "data/epic_free_cache.json", dict)


@dataclass(frozen=True, slots=True)
class EpicGame:
    title: str
    original_price: str
    discount_price: int
    start_date: str | None
    end_date: str | None
    page_url: str
    image_url: str | None
    is_free_now: bool


@dataclass(frozen=True, slots=True)
class GameDealInfo:
    game_id: str
    title: str
    steam_app_id: str | None
    sale_price: float
    normal_price: float
    savings_percent: float
    cheapest_price_ever: float | None
    cheapest_date: str | None
    is_historic_low: bool
    store_name: str
    deal_url: str
    steam_url: str | None
    thumb_url: str | None
    steam_rating_text: str | None
    steam_rating_percent: int | None
    steam_rating_count: int | None
    metacritic_score: int | None


def _format_date(date_str: str | None) -> str:
    if not date_str:
        return "未知"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00")).astimezone(TZ)
        return dt.strftime("%m月%d日 %H:%M")
    except Exception:
        return str(date_str)[:16]


async def fetch_epic_free_games(
    session: aiohttp.ClientSession,
    timeout_seconds: float = 15.0,
) -> tuple[list[EpicGame], list[EpicGame]]:
    """从 Epic 官方公开促销 API 抓取当前免费游戏与下周预告。"""
    url = (
        "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
        "?locale=zh-CN&country=CA&allowCountries=CA"
    )
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with session.get(url, headers=DEFAULT_HEADERS, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"Epic API 返回状态码 {response.status}")
        data = await response.json()

    elements = (
        data.get("data", {})
        .get("Catalog", {})
        .get("searchStore", {})
        .get("elements", [])
    )

    active_games: list[EpicGame] = []
    upcoming_games: list[EpicGame] = []

    for el in elements:
        title = el.get("title", "").strip()
        if not title:
            continue

        promotions = el.get("promotions") or {}
        offers = promotions.get("promotionalOffers") or []
        upcoming = promotions.get("upcomingPromotionalOffers") or []

        price_data = el.get("price", {}).get("totalPrice", {})
        original_price = (
            price_data.get("fmtPrice", {}).get("originalPrice") or "免费"
        )
        discount_price = price_data.get("discountPrice", 0)

        is_active = False
        start_date = None
        end_date = None
        for group in offers:
            for offer in group.get("promotionalOffers", []):
                ds = offer.get("discountSetting", {})
                if ds.get("discountPercentage") == 0:
                    is_active = True
                    start_date = offer.get("startDate")
                    end_date = offer.get("endDate")
                    break
            if is_active:
                break

        is_upcoming = False
        if not is_active:
            for group in upcoming:
                for offer in group.get("promotionalOffers", []):
                    ds = offer.get("discountSetting", {})
                    if ds.get("discountPercentage") == 0:
                        is_upcoming = True
                        start_date = offer.get("startDate")
                        end_date = offer.get("endDate")
                        break
                if is_upcoming:
                    break

        if not is_active and not is_upcoming:
            continue

        slug = el.get("productSlug") or el.get("urlSlug")
        if not slug:
            mappings = el.get("catalogNs", {}).get("mappings") or []
            if mappings:
                slug = mappings[0].get("pageSlug")

        if slug:
            clean_slug = slug.strip("/").split("/")[0]
            page_url = f"https://store.epicgames.com/zh-CN/p/{clean_slug}"
        else:
            page_url = "https://store.epicgames.com/zh-CN/free-games"

        thumb_url = None
        for img in el.get("keyImages", []):
            if img.get("type") in ("OfferImageWide", "DieselStoreFrontWide", "Thumbnail", "VaultClosed"):
                thumb_url = img.get("url")
                break
        if not thumb_url and el.get("keyImages"):
            thumb_url = el["keyImages"][0].get("url")

        game = EpicGame(
            title=title,
            original_price=original_price,
            discount_price=discount_price,
            start_date=start_date,
            end_date=end_date,
            page_url=page_url,
            image_url=thumb_url,
            is_free_now=is_active,
        )

        if is_active:
            active_games.append(game)
        elif is_upcoming:
            upcoming_games.append(game)

    return active_games, upcoming_games


async def search_cheapshark_games(
    title: str,
    session: aiohttp.ClientSession,
    limit: int = 5,
    timeout_seconds: float = 12.0,
) -> list[dict[str, Any]]:
    """在 CheapShark 查询游戏列表。"""
    url = f"https://www.cheapshark.com/api/1.0/games?title={quote(title.strip())}&limit={limit}"
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with session.get(url, headers=DEFAULT_HEADERS, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"CheapShark 搜索 API 返回状态码 {response.status}")
        data = await response.json()
    return data if isinstance(data, list) else []


async def get_game_deal_info(
    game_id: str,
    session: aiohttp.ClientSession,
    timeout_seconds: float = 12.0,
) -> GameDealInfo | None:
    """查询指定 CheapShark 游戏 ID 的最新各商店折扣与历史史低。"""
    url = f"https://www.cheapshark.com/api/1.0/games?id={quote(str(game_id).strip())}"
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with session.get(url, headers=DEFAULT_HEADERS, timeout=timeout) as response:
        if response.status != 200:
            logger.warning("CheapShark 详情接口返回 %s: game_id=%s", response.status, game_id)
            return None
        data = await response.json()

    if not isinstance(data, dict) or not data.get("info"):
        return None

    info = data["info"]
    title = info.get("title", "未知游戏")
    steam_app_id = info.get("steamAppID")
    thumb_url = info.get("thumb")

    cheapest_price_ever_data = data.get("cheapestPriceEver") or {}
    cheapest_price_ever = (
        float(cheapest_price_ever_data["price"])
        if cheapest_price_ever_data.get("price")
        else None
    )
    cheapest_date_ts = cheapest_price_ever_data.get("date")
    cheapest_date = None
    if cheapest_date_ts:
        try:
            cheapest_date = datetime.fromtimestamp(int(cheapest_date_ts), tz=TZ).strftime("%Y-%m-%d")
        except Exception:
            pass

    deals = data.get("deals") or []
    if not deals:
        return None

    steam_deal = next((d for d in deals if d.get("storeID") == "1"), None)
    best_deal = steam_deal if (steam_deal and float(steam_deal.get("savings", 0)) > 0) else deals[0]

    sale_price = float(best_deal.get("price", 0.0))
    normal_price = float(best_deal.get("retailPrice", sale_price))
    savings_percent = float(best_deal.get("savings", 0.0))
    deal_id = best_deal.get("dealID", "")
    store_id = best_deal.get("storeID", "1")
    store_name = CHEAPSHARK_STORES.get(store_id, f"Store {store_id}")

    deal_url = f"https://www.cheapshark.com/redirect?dealID={deal_id}" if deal_id else ""
    steam_url = f"https://store.steampowered.com/app/{steam_app_id}" if steam_app_id else None

    is_historic_low = (
        cheapest_price_ever is not None
        and sale_price > 0
        and sale_price <= (cheapest_price_ever + 0.05)
    )

    steam_rating_text = best_deal.get("steamRatingText")
    steam_rating_percent = (
        int(best_deal["steamRatingPercent"])
        if best_deal.get("steamRatingPercent")
        else None
    )
    steam_rating_count = (
        int(best_deal["steamRatingCount"])
        if best_deal.get("steamRatingCount")
        else None
    )
    metacritic_score = (
        int(best_deal["metacriticScore"])
        if best_deal.get("metacriticScore") and int(best_deal["metacriticScore"]) > 0
        else None
    )

    return GameDealInfo(
        game_id=str(game_id),
        title=title,
        steam_app_id=steam_app_id,
        sale_price=sale_price,
        normal_price=normal_price,
        savings_percent=savings_percent,
        cheapest_price_ever=cheapest_price_ever,
        cheapest_date=cheapest_date,
        is_historic_low=is_historic_low,
        store_name=store_name,
        deal_url=deal_url,
        steam_url=steam_url,
        thumb_url=thumb_url,
        steam_rating_text=steam_rating_text,
        steam_rating_percent=steam_rating_percent,
        steam_rating_count=steam_rating_count,
        metacritic_score=metacritic_score,
    )


def build_epic_free_embed(
    active_games: list[EpicGame],
    upcoming_games: list[EpicGame],
) -> discord.Embed:
    """构建 Epic 每周限免与预告 Embed 卡片。"""
    embed = discord.Embed(
        title="🎁 Epic 喜加一：本周限时免费游戏已解锁",
        description=(
            f"本周共有 **{len(active_games)}** 款游戏限免赠送，入库即可永久保留！"
            if active_games
            else "本周暂未检测到生效中的限时免费游戏。"
        ),
        color=discord.Color.from_rgb(0, 120, 242),
    )

    for game in active_games:
        end_str = _format_date(game.end_date)
        value_lines = [
            f"• **原价**: ~~{game.original_price}~~ ➔ **FREE ($0.00)**",
            f"• **截止时间**: `{end_str}`",
            f"• **领取入口**: [👉 点击直达 Epic 商城]({game.page_url})",
        ]
        embed.add_field(
            name=f"🎮 《{game.title}》",
            value="\n".join(value_lines),
            inline=False,
        )

    if upcoming_games:
        preview_lines = []
        for g in upcoming_games:
            start_str = _format_date(g.start_date)
            preview_lines.append(f"• **《{g.title}》** (原价 {g.original_price}，`{start_str}` 解锁)")
        embed.add_field(
            name="⏰ 下周限免预告",
            value="\n".join(preview_lines),
            inline=False,
        )

    for g in active_games:
        if g.image_url:
            embed.set_thumbnail(url=g.image_url)
            break

    embed.set_footer(text="Epic Games Store • 换免时间每周四 11:00 AM (美东)")
    return embed


def build_deal_embed(
    deal: GameDealInfo,
    is_broadcast: bool = False,
) -> discord.Embed:
    """构建 Steam / PC 游戏折扣 Embed 卡片。"""
    color = discord.Color.green() if deal.is_historic_low else discord.Color.blue()
    title_prefix = "🔥 [史低特惠]" if deal.is_historic_low else "🏷️ [降价特惠]"
    if not is_broadcast and deal.savings_percent <= 0:
        title_prefix = "🎮 [价格查询]"
        color = discord.Color.light_grey()

    embed = discord.Embed(
        title=f"{title_prefix} 《{deal.title}》",
        color=color,
    )

    price_lines = []
    if deal.savings_percent > 0:
        price_lines.append(
            f"• **当前价格**: **${deal.sale_price:.2f} USD** (原价 ~~${deal.normal_price:.2f}~~)"
        )
        price_lines.append(f"• **折扣力度**: 🟩 **-{deal.savings_percent:.0f}% OFF**")
    else:
        price_lines.append(f"• **当前价格**: **${deal.sale_price:.2f} USD** (暂无折扣)")

    if deal.cheapest_price_ever is not None:
        if deal.is_historic_low:
            price_lines.append(f"• **史低状态**: 🔥 **平/破历史最低价！** (历史最低: ${deal.cheapest_price_ever:.2f})")
        else:
            date_info = f" ({deal.cheapest_date})" if deal.cheapest_date else ""
            price_lines.append(f"• **历史史低**: `${deal.cheapest_price_ever:.2f}`{date_info}")

    embed.add_field(name="💰 价格与折扣", value="\n".join(price_lines), inline=False)

    rating_lines = [f"• **优惠平台**: `{deal.store_name}`"]
    if deal.steam_rating_text:
        count_str = f", {deal.steam_rating_count:,} 篇评价" if deal.steam_rating_count else ""
        rating_lines.append(
            f"• **Steam 评价**: {deal.steam_rating_text} ({deal.steam_rating_percent}% 正面{count_str})"
        )
    if deal.metacritic_score:
        rating_lines.append(f"• **Metacritic 媒体均分**: `{deal.metacritic_score}`/100")

    embed.add_field(name="📊 平台与评价", value="\n".join(rating_lines), inline=False)

    link_parts = []
    if deal.steam_url:
        link_parts.append(f"[Steam 商店页面]({deal.steam_url})")
    if deal.deal_url:
        link_parts.append(f"[{deal.store_name} 购买直达]({deal.deal_url})")
    if deal.steam_app_id:
        link_parts.append(f"[SteamDB 数据](https://steamdb.info/app/{deal.steam_app_id}/)")

    if link_parts:
        embed.add_field(name="🔗 直达链接", value=" • ".join(link_parts), inline=False)

    if deal.thumb_url:
        embed.set_thumbnail(url=deal.thumb_url)

    embed.set_footer(text="数据来源: CheapShark & Steam • 每日 13:30 自动监测愿望单变动")
    return embed


def get_watchlist() -> list[dict[str, str]]:
    """获取当前监控的愿望单游戏列表。"""
    data = _watchlist_store.read()
    if not isinstance(data, list) or not data:
        _watchlist_store.write(DEFAULT_WATCHLIST)
        return list(DEFAULT_WATCHLIST)
    return data


def add_to_watchlist(title: str, game_id: str, steam_app_id: str | None = None) -> bool:
    """向愿望单新增游戏。若已存在则返回 False。"""
    def mutator(items: list[dict[str, str]]) -> list[dict[str, str]]:
        if not isinstance(items, list):
            items = []
        for it in items:
            if str(it.get("game_id")) == str(game_id) or str(it.get("title", "")).lower() == title.lower():
                return items
        items.append({
            "title": title,
            "game_id": str(game_id),
            "steam_app_id": str(steam_app_id) if steam_app_id else "",
        })
        return items

    before_len = len(get_watchlist())
    after = _watchlist_store.update(mutator)
    return len(after) > before_len


def remove_from_watchlist(query: str) -> bool:
    """根据名称或 game_id 从愿望单移除游戏。"""
    q = query.strip().lower()

    def mutator(items: list[dict[str, str]]) -> list[dict[str, str]]:
        if not isinstance(items, list):
            return []
        return [
            it for it in items
            if str(it.get("game_id")) != q and str(it.get("title", "")).lower() != q
        ]

    before_len = len(get_watchlist())
    after = _watchlist_store.update(mutator)
    return len(after) < before_len


def should_notify_deal(deal: GameDealInfo) -> bool:
    """判定是否应当触发降价广播推送（新打折或价格更低）。"""
    if deal.savings_percent <= 0:
        return False

    cache = _deals_cache_store.read()
    if not isinstance(cache, dict):
        cache = {}

    entry = cache.get(deal.game_id)
    if not entry:
        return True

    last_price = float(entry.get("last_price", 999999.0))
    if deal.sale_price < (last_price - 0.05):
        return True
    if deal.is_historic_low and not entry.get("was_historic_low", False):
        return True

    return False


def record_notified_deal(deal: GameDealInfo) -> None:
    """记录已推送的打折状态。"""
    today = datetime.now(tz=TZ).strftime("%Y-%m-%d")

    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            data = {}
        data[deal.game_id] = {
            "title": deal.title,
            "last_price": deal.sale_price,
            "savings": deal.savings_percent,
            "was_historic_low": deal.is_historic_low,
            "notified_date": today,
        }
        return data

    _deals_cache_store.update(mutator)


def should_notify_epic(active_games: list[EpicGame]) -> bool:
    """判定是否需要向频道推送本周 Epic 喜加一。"""
    if not active_games:
        return False

    now = datetime.now(tz=TZ)
    current_week = now.strftime("%Y-W%W")

    cache = _epic_cache_store.read()
    if not isinstance(cache, dict):
        cache = {}

    last_week = cache.get("last_notified_week")
    last_titles = set(cache.get("game_titles", []))
    current_titles = {g.title for g in active_games}

    if last_week == current_week and last_titles == current_titles:
        return False
    return True


def record_notified_epic(active_games: list[EpicGame]) -> None:
    """记录已推送的 Epic 周免状态。"""
    now = datetime.now(tz=TZ)
    current_week = now.strftime("%Y-W%W")

    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            data = {}
        data["last_notified_week"] = current_week
        data["game_titles"] = [g.title for g in active_games]
        data["notified_at"] = now.isoformat()
        return data

    _epic_cache_store.update(mutator)
