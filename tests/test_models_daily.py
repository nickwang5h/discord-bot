import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from cogs import models_daily
from core.storage import JsonStore


def sample_day():
    return {
        "date": "2026-10-23",
        "id": "five-whys",
        "name": "五个为什么",
        "en": "Five Whys",
        "one_line": "出了问题，把每个答案再问一遍为什么。",
        "url": "https://models.wuhua.app/m/five-whys",
        "question": {
            "stem": "药店每周发错两三次药袋。",
            "question": "这时最该做什么？",
            "explain": "回到第二层，查同姓药袋为什么难分。",
            "options": [
                {"text": "培训全体店员", "correct": False, "feedback": "不对着条件。"},
                {"text": "查同姓药袋为何难分", "correct": True, "feedback": "找到了能改的地方。"},
                {"text": "申请加人", "correct": False, "feedback": "没有证据。"},
            ],
        },
    }


class ModelsDailyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        store = JsonStore(Path(self.directory.name) / "models_daily.json", dict)
        self.store_patch = patch.object(models_daily, "_sent_store", store)
        self.store_patch.start()

    def tearDown(self):
        self.store_patch.stop()
        self.directory.cleanup()

    def test_embed_is_only_the_model_card(self):
        embed = models_daily._build_embed(models_daily._validate_day(sample_day()))
        self.assertEqual(embed.title, "🧠 今日模型：五个为什么（Five Whys）")
        self.assertEqual(embed.url, "https://models.wuhua.app/m/five-whys")
        self.assertIn("A. 培训全体店员\nB. 查同姓药袋为何难分\nC. 申请加人", embed.description)
        self.assertIn("||答案 B：回到第二层，查同姓药袋为什么难分。||", embed.description)
        # Only the card links out: none of the owner's other practice sites.
        for other in ("日课", "learn.nickwang", "en.wuhua", "fr.wuhua", "英语", "法语"):
            self.assertNotIn(other, embed.description)

    def test_rejects_malformed_days(self):
        day = sample_day()
        day["question"]["options"][0]["correct"] = True
        with self.assertRaises(ValueError):
            models_daily._validate_day(day)
        day = sample_day()
        day["url"] = "https://example.com/m/five-whys"
        with self.assertRaises(ValueError):
            models_daily._validate_day(day)

    async def test_scheduled_push_sends_once_per_utc_day(self):
        bot = MagicMock()
        channel = MagicMock()
        channel.send = AsyncMock()
        bot.get_channel.return_value = channel
        date = datetime.date(2026, 10, 23)
        with (
            patch.object(models_daily, "SCHEDULED_JOBS_ENABLED", False),
            patch.object(models_daily, "_utc_today", return_value=date),
            patch.object(models_daily, "fetch_day", AsyncMock(return_value=sample_day())),
            patch.object(models_daily.settings, "get_setting", side_effect=lambda key, default=None: {"READING_CHANNEL_ID": "42"}.get(key, default)),
        ):
            cog = models_daily.ModelsDaily(bot)
            await cog.models_daily.coro(cog)
            await cog.models_daily.coro(cog)

        bot.get_channel.assert_called_once_with(42)
        channel.send.assert_awaited_once()
        self.assertEqual(models_daily._sent_store.read(), {"2026-10-23": "five-whys"})


if __name__ == "__main__":
    unittest.main()
