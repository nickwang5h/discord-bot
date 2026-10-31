import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from config import TZ
from core.gaming import (
    EpicGame,
    GameDealInfo,
    add_to_watchlist,
    build_deal_embed,
    build_deals_digest_embed,
    build_epic_free_embed,
    fetch_epic_free_games,
    find_steam_historic_lows,
    get_game_deal_info,
    get_watchlist,
    record_notified_deal,
    record_notified_epic,
    remove_from_watchlist,
    search_cheapshark_games,
    should_notify_deal,
    should_notify_epic,
)
from core.storage import JsonStore


class GamingTests(unittest.TestCase):
    def test_epic_free_games_parsing(self):
        sample_epic_payload = {
            "data": {
                "Catalog": {
                    "searchStore": {
                        "elements": [
                            {
                                "title": "Test Game 1",
                                "price": {
                                    "totalPrice": {
                                        "discountPrice": 0,
                                        "fmtPrice": {"originalPrice": "CA$29.99"},
                                    }
                                },
                                "promotions": {
                                    "promotionalOffers": [
                                        {
                                            "promotionalOffers": [
                                                {
                                                    "startDate": "2026-09-03T15:00:00.000Z",
                                                    "endDate": "2026-09-10T15:00:00.000Z",
                                                    "discountSetting": {
                                                        "discountType": "PERCENTAGE",
                                                        "discountPercentage": 0,
                                                    },
                                                }
                                            ]
                                        }
                                    ],
                                    "upcomingPromotionalOffers": [],
                                },
                                "productSlug": "test-game-1",
                                "keyImages": [
                                    {"type": "OfferImageWide", "url": "https://img.test/wide.jpg"}
                                ],
                            },
                            {
                                "title": "Upcoming Game 2",
                                "price": {
                                    "totalPrice": {
                                        "discountPrice": 3999,
                                        "fmtPrice": {"originalPrice": "CA$39.99"},
                                    }
                                },
                                "promotions": {
                                    "promotionalOffers": [],
                                    "upcomingPromotionalOffers": [
                                        {
                                            "promotionalOffers": [
                                                {
                                                    "startDate": "2026-09-10T15:00:00.000Z",
                                                    "endDate": "2026-09-17T15:00:00.000Z",
                                                    "discountSetting": {
                                                        "discountType": "PERCENTAGE",
                                                        "discountPercentage": 0,
                                                    },
                                                }
                                            ]
                                        }
                                    ],
                                },
                                "urlSlug": "upcoming-game-2",
                                "keyImages": [],
                            },
                        ]
                    }
                }
            }
        }

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=sample_epic_payload)

        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__.return_value = mock_response

        active, upcoming = asyncio.run(fetch_epic_free_games(mock_session))
        self.assertEqual(len(active), 1)
        self.assertEqual(len(upcoming), 1)
        self.assertEqual(active[0].title, "Test Game 1")
        self.assertEqual(active[0].original_price, "CA$29.99")
        self.assertTrue(active[0].is_free_now)
        self.assertIn("test-game-1", active[0].page_url)
        self.assertEqual(active[0].image_url, "https://img.test/wide.jpg")

        self.assertEqual(upcoming[0].title, "Upcoming Game 2")
        self.assertFalse(upcoming[0].is_free_now)

    def test_cheapshark_deal_parsing_and_historic_low(self):
        sample_deal_payload = {
            "info": {
                "title": "Cyberpunk 2077",
                "steamAppID": "1091500",
                "thumb": "https://img.test/cp2077.jpg",
            },
            "cheapestPriceEver": {
                "price": "19.99",
                "date": 1780000000,
            },
            "deals": [
                {
                    "storeID": "1",
                    "dealID": "deal12345",
                    "price": "19.99",
                    "retailPrice": "59.99",
                    "savings": "66.6777",
                    "steamRatingText": "Very Positive",
                    "steamRatingPercent": "88",
                    "steamRatingCount": "650000",
                    "metacriticScore": "86",
                }
            ],
        }

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=sample_deal_payload)

        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__.return_value = mock_response

        deal = asyncio.run(get_game_deal_info("202350", mock_session))
        self.assertIsNotNone(deal)
        self.assertEqual(deal.title, "Cyberpunk 2077")
        self.assertEqual(deal.sale_price, 19.99)
        self.assertEqual(deal.normal_price, 59.99)
        self.assertTrue(deal.is_historic_low)
        self.assertEqual(deal.store_name, "Steam")
        self.assertEqual(deal.steam_rating_percent, 88)
        self.assertEqual(deal.metacritic_score, 86)
        self.assertIn("1091500", deal.steam_url)

    def test_build_epic_embed(self):
        active = [
            EpicGame(
                title="Free Game",
                original_price="CA$19.99",
                discount_price=0,
                start_date="2026-09-03T15:00:00.000Z",
                end_date="2026-09-10T15:00:00.000Z",
                page_url="https://store.epicgames.com/zh-CN/p/free-game",
                image_url="https://img.test/thumb.jpg",
                is_free_now=True,
            )
        ]
        upcoming = [
            EpicGame(
                title="Next Week Game",
                original_price="CA$29.99",
                discount_price=0,
                start_date="2026-09-10T15:00:00.000Z",
                end_date="2026-09-17T15:00:00.000Z",
                page_url="https://store.epicgames.com/zh-CN/p/next-game",
                image_url=None,
                is_free_now=False,
            )
        ]
        embed = build_epic_free_embed(active, upcoming)
        self.assertIn("Epic 喜加一", embed.title)
        field_text = "\n".join(f"{f.name} {f.value}" for f in embed.fields)
        self.assertIn("Free Game", field_text)
        self.assertIn("Next Week Game", field_text)
        self.assertIn("CA$19.99", field_text)

    def test_build_deal_embed(self):
        deal = GameDealInfo(
            game_id="123",
            title="Elden Ring",
            steam_app_id="1245620",
            sale_price=29.99,
            normal_price=59.99,
            savings_percent=50.0,
            cheapest_price_ever=29.99,
            cheapest_date="2026-06-20",
            is_historic_low=True,
            store_name="Steam",
            deal_url="https://cheapshark.com/redirect?dealID=abc",
            steam_url="https://store.steampowered.com/app/1245620",
            thumb_url="https://img.test/elden.jpg",
            steam_rating_text="Overwhelmingly Positive",
            steam_rating_percent=95,
            steam_rating_count=300000,
            metacritic_score=96,
        )
        embed = build_deal_embed(deal, is_broadcast=True)
        self.assertIn("史低", embed.title)
        field_text = "\n".join(f"{f.name} {f.value}" for f in embed.fields)
        self.assertIn("-50%", field_text)
        self.assertIn("$29.99", field_text)
        self.assertIn("平/破历史最低价", field_text)
        self.assertIn("95%", field_text)

    def test_watchlist_store_operations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_store = JsonStore(Path(temp_dir) / "watchlist.json", list)
            with patch("core.gaming._watchlist_store", temp_store):
                # 初始愿望单为空，不再填入示例游戏
                self.assertEqual(get_watchlist(), [])

                # 添加新游戏
                added = add_to_watchlist("Hades II", "300001", "1145320")
                self.assertTrue(added)

                # 重复添加应被拒绝
                added_again = add_to_watchlist("Hades II", "300001")
                self.assertFalse(added_again)

                # 移除游戏
                removed = remove_from_watchlist("Hades II")
                self.assertTrue(removed)

                # 再次移除不存在的游戏
                removed_again = remove_from_watchlist("NonExistentGame")
                self.assertFalse(removed_again)

    def test_deals_cache_and_notification_logic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_cache = JsonStore(Path(temp_dir) / "deals_cache.json", dict)
            with patch("core.gaming._deals_cache_store", temp_cache):
                deal = GameDealInfo(
                    game_id="999",
                    title="Disco Elysium",
                    steam_app_id="632470",
                    sale_price=9.99,
                    normal_price=39.99,
                    savings_percent=75.0,
                    cheapest_price_ever=9.99,
                    cheapest_date="2026-01-01",
                    is_historic_low=True,
                    store_name="Steam",
                    deal_url="https://cheapshark.com/redirect",
                    steam_url="https://store.steampowered.com/app/632470",
                    thumb_url=None,
                    steam_rating_text=None,
                    steam_rating_percent=None,
                    steam_rating_count=None,
                    metacritic_score=None,
                )

                # 第一次应触发推送
                self.assertTrue(should_notify_deal(deal))
                record_notified_deal(deal)

                # 记录后相同价格不应重复推送
                self.assertFalse(should_notify_deal(deal))

                # 同一价格 30 天后再次出现（下一轮促销），重新推送
                temp_cache.update(lambda data: {**data, "999": {**data["999"], "notified_date": "2026-01-01"}})
                self.assertTrue(should_notify_deal(deal))

    def test_epic_cache_and_notification_logic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_cache = JsonStore(Path(temp_dir) / "epic_cache.json", dict)
            with patch("core.gaming._epic_cache_store", temp_cache):
                active = [
                    EpicGame(
                        title="Free Game A",
                        original_price="CA$10.00",
                        discount_price=0,
                        start_date=None,
                        end_date=None,
                        page_url="url",
                        image_url=None,
                        is_free_now=True,
                    )
                ]

                # 第一次应当通知
                self.assertTrue(should_notify_epic(active))
                record_notified_epic(active)

                # 记录后同周相同游戏不应重复通知
                self.assertFalse(should_notify_epic(active))


if __name__ == "__main__":
    unittest.main()


def _routed_session(routes):
    """A session whose get(url) answers with the first route whose key is in the url."""
    def get(url, **kwargs):
        body, headers = next(v for k, v in routes.items() if k in url)
        response = AsyncMock()
        response.status = 200
        response.headers = headers
        response.json = AsyncMock(return_value=body)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        return context
    session = MagicMock()
    session.get.side_effect = get
    return session


class HistoricLowScanTests(unittest.TestCase):
    def test_keeps_well_reviewed_games_at_their_all_time_low(self):
        def row(game_id, title, price, reviews):
            return {"gameID": game_id, "title": title, "salePrice": price, "normalPrice": "29.99",
                    "savings": "70.0", "steamAppID": "1" + game_id, "dealID": "d" + game_id,
                    "steamRatingText": "Overwhelmingly Positive", "steamRatingPercent": "97",
                    "steamRatingCount": str(reviews), "metacriticScore": "0", "thumb": None}
        session = _routed_session({
            "pageNumber=0": ([row("1", "At Low", "8.99", 170000), row("2", "Above Low", "6.24", 77000),
                              row("3", "Niche", "1.24", 772)], {"X-Total-Page-Count": "2"}),
            "pageNumber=1": ([row("4", "Also Low", "4.99", 14000)], {"X-Total-Page-Count": "2"}),
            "games?ids=": ({
                "1": {"cheapestPriceEver": {"price": "8.99", "date": 1780000000}},
                "2": {"cheapestPriceEver": {"price": "2.49", "date": 1780000000}},
                "4": {"cheapestPriceEver": {"price": "4.99", "date": 1780000000}},
            }, {}),
        })

        lows = asyncio.run(find_steam_historic_lows(session, pause_seconds=0))

        self.assertEqual([d.title for d in lows], ["At Low", "Also Low"])
        self.assertTrue(all(d.is_historic_low and d.store_name == "Steam" for d in lows))
        self.assertEqual(lows[0].steam_rating_count, 170000)
        self.assertIsNone(lows[0].metacritic_score)
        self.assertTrue((lows[1].steam_url or "").endswith("/app/14"))
        asked_ids = [c.args[0] for c in session.get.call_args_list if "games?ids=" in c.args[0]]
        self.assertEqual(len(asked_ids), 1)
        self.assertNotIn("3", asked_ids[0].split("ids=")[1].split(","))


class DealsDigestTests(unittest.TestCase):
    @staticmethod
    def _deal(game_id, title, *, low=True, store="Steam"):
        return GameDealInfo(
            game_id=game_id, title=title, steam_app_id="1" + game_id, sale_price=4.99,
            normal_price=19.99, savings_percent=75.0, cheapest_price_ever=4.99 if low else 2.99,
            cheapest_date=None, is_historic_low=low, store_name=store,
            deal_url="https://www.cheapshark.com/redirect?dealID=d" + game_id,
            steam_url=f"https://store.steampowered.com/app/1{game_id}", thumb_url=None,
            steam_rating_text="Very Positive", steam_rating_percent=94, steam_rating_count=123456,
            metacritic_score=None,
        )

    def test_many_deals_become_one_compact_list(self):
        deals = [self._deal(str(i), f"Game {i}") for i in range(10)]
        embed, included = build_deals_digest_embed(deals, title="🔥 Steam 好评游戏史低", mark_low=False)
        self.assertEqual(included, deals)
        self.assertIn("10 款", embed.title)
        lines = embed.description.splitlines()
        self.assertEqual(len(lines), 10)
        self.assertIn("[Game 0](https://store.steampowered.com/app/10)", lines[0])
        self.assertIn("$4.99", lines[0])
        self.assertIn("-75%", lines[0])
        self.assertIn("好评 94%（12.3 万评测）", lines[0])
        self.assertNotIn("史低", lines[0])

    def test_watchlist_lines_mark_low_and_store(self):
        embed, _ = build_deals_digest_embed(
            [self._deal("1", "A"), self._deal("2", "B", low=False, store="GOG")],
            title="🏷️ 愿望单降价", mark_low=True,
        )
        first, second = embed.description.splitlines()
        self.assertIn("🔥 史低", first)
        self.assertIn("史低 $2.99", second)
        self.assertIn("GOG", second)

    def test_overflow_is_left_for_the_next_run(self):
        deals = [self._deal(str(i), "X" * 200) for i in range(10)]
        embed, included = build_deals_digest_embed(deals, title="t", mark_low=False, max_chars=1000)
        self.assertLess(len(included), 10)
        self.assertLessEqual(len(embed.description), 1000)
        self.assertEqual(len(embed.description.splitlines()), len(included))
