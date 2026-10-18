import asyncio
import datetime as dt
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from cogs import ask as ask_cog
from cogs.ask import (
    ASK_MODE_GEMINI_SEARCH,
    ASK_MODE_QWEN,
    SYSTEM_PROMPT,
    WEATHER_UNAVAILABLE_NOTICE,
    Ask,
    _build_ask_reply,
    _extract_weather_slots,
    is_weather_question,
    parse_weather_slots,
)
from core import ai_client
from core.ai_providers import AIResult
from core.weather import CityNotFoundError

TODAY = dt.date(2026, 10, 18)
TORONTO_Q = "要去多伦多，告诉我19-21日的天气预报"


class FixedDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 18, 9, 0, tzinfo=tz)


def slots_json(**overrides):
    payload = {"intent": "weather", "city": "Toronto", "date_from": "2026-10-19", "date_to": "2026-10-21"}
    payload.update(overrides)
    return json.dumps(payload)


class KeywordFilterTests(unittest.TestCase):
    def test_weather_questions_hit(self):
        for question in (
            TORONTO_Q,
            "明天渥太华下雪吗",
            "周末穿什么",
            "What's the weather in Toronto?",
            "Will it RAIN tomorrow",
            "temperature in Sudbury",
        ):
            with self.subTest(question=question):
                self.assertTrue(is_weather_question(question))

    def test_unrelated_questions_miss(self):
        for question in (
            "解释量子纠缠",
            "推荐一本书",
            "how to train a brain model",  # rain/rain inside words must not match
            "风险投资是什么",  # bare 风 is deliberately not a keyword
        ):
            with self.subTest(question=question):
                self.assertFalse(is_weather_question(question))


class SlotValidationTests(unittest.TestCase):
    def parse(self, raw, question=TORONTO_Q):
        return parse_weather_slots(raw, today=TODAY, question=question)

    def test_valid_range(self):
        slots = self.parse(slots_json())
        self.assertEqual(slots.city, "Toronto")
        self.assertEqual((slots.date_from, slots.date_to), (dt.date(2026, 10, 19), dt.date(2026, 10, 21)))

    def test_single_day_and_default_dates(self):
        slots = self.parse(slots_json(date_from="2026-10-19", date_to=None), question="明天多伦多天气")
        self.assertEqual(slots.date_from, slots.date_to)
        slots = self.parse(slots_json(city=None, date_from=None, date_to=None), question="天气怎么样")
        self.assertIsNone(slots.city)
        self.assertEqual((slots.date_from, slots.date_to), (TODAY, TODAY + dt.timedelta(days=2)))

    def test_day_without_month_rolls_to_nearest_future(self):
        # Today 10-28 and the user asks "3-5日"; a model anchored to the past current month.
        slots = parse_weather_slots(
            slots_json(date_from="2026-10-03", date_to="2026-10-05"),
            today=dt.date(2026, 10, 28),
            question="多伦多3-5日天气",
        )
        self.assertEqual((slots.date_from, slots.date_to), (dt.date(2026, 11, 3), dt.date(2026, 11, 5)))

    def test_explicit_past_month_is_not_rolled(self):
        self.assertIsNone(
            parse_weather_slots(
                slots_json(date_from="2026-10-03", date_to="2026-10-05"),
                today=dt.date(2026, 10, 28),
                question="多伦多10月3-5日天气",
            )
        )

    def test_invalid_payloads_become_other(self):
        for raw in (
            "not json",
            "[]",
            slots_json(intent="other"),
            slots_json(city=42),
            slots_json(date_from="10/19"),
            slots_json(date_from="2026-13-01"),
            slots_json(date_from="2026-10-21", date_to="2026-10-19"),  # reversed
            slots_json(date_from="2026-10-17", date_to="2026-10-17"),  # yesterday
            slots_json(date_from="2026-10-18", date_to="2026-11-05"),  # beyond 16 days
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(self.parse(raw))


class ExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_extraction_uses_free_chain_json_mode_without_route(self):
        generate = AsyncMock(return_value=AIResult(slots_json(), "Groq", "qwen"))
        with patch("cogs.ask.ai_client.generate_ai", generate):
            slots = await _extract_weather_slots(TORONTO_Q, TODAY)

        self.assertEqual(slots.city, "Toronto")
        kwargs = generate.await_args.kwargs
        self.assertTrue(kwargs["json_mode"])
        self.assertFalse(kwargs["use_search"])
        self.assertEqual(kwargs["max_output_tokens"], 200)
        self.assertNotIn("route", kwargs)
        self.assertIn("2026-10-18", kwargs["system"])

    async def test_extraction_failure_and_timeout_return_none(self):
        with patch(
            "cogs.ask.ai_client.generate_ai",
            AsyncMock(side_effect=ai_client.AIServiceUnavailable("down")),
        ):
            self.assertIsNone(await _extract_weather_slots(TORONTO_Q, TODAY))

        async def slow(*_args, **_kwargs):
            await asyncio.sleep(1)

        with (
            patch("cogs.ask.ai_client.generate_ai", slow),
            patch.object(ask_cog, "WEATHER_SLOT_TIMEOUT_SECONDS", 0.01),
        ):
            self.assertIsNone(await _extract_weather_slots(TORONTO_Q, TODAY))


class AskReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.object(ask_cog, "datetime", FixedDatetime)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_non_weather_question_keeps_ask_ai_call_unchanged(self):
        generate = AsyncMock(side_effect=AssertionError("no slot extraction"))
        ask = AsyncMock(return_value="answer<!--MODEL:Groq (qwen)-->")
        with (
            patch("cogs.ask.ai_client.generate_ai", generate),
            patch("cogs.ask.ai_client.ask_ai", ask),
        ):
            embed = await _build_ask_reply("解释量子纠缠", ASK_MODE_QWEN)

        ask.assert_awaited_once_with("解释量子纠缠", system=SYSTEM_PROMPT, use_search=False)
        generate.assert_not_awaited()
        self.assertEqual(embed.title, "❓ 提问: 解释量子纠缠")
        self.assertEqual(embed.description, "answer")
        self.assertEqual(embed.footer.text, "✨ Powered by Groq (qwen)")

    async def test_weather_intent_returns_forecast_embed_without_model_answer(self):
        forecast = discord.Embed(title="📍 Toronto 10/19–10/21 天气预报")
        fetch = AsyncMock(return_value=forecast)
        ask = AsyncMock(side_effect=AssertionError("model must not write the weather answer"))
        with (
            patch("cogs.ask.ai_client.generate_ai", AsyncMock(return_value=AIResult(slots_json(), "Groq", "q"))),
            patch("cogs.ask.ai_client.ask_ai", ask),
            patch("cogs.ask.weather.fetch_forecast_range", fetch),
        ):
            embed = await _build_ask_reply(TORONTO_Q, ASK_MODE_QWEN)

        self.assertIs(embed, forecast)
        args, kwargs = fetch.await_args
        self.assertEqual(args[:3], ("Toronto", dt.date(2026, 10, 19), dt.date(2026, 10, 21)))
        self.assertEqual(kwargs["today"], TODAY)
        self.assertEqual(kwargs["preface"], f"❓ 提问: {TORONTO_Q}")

    async def test_missing_city_defaults_to_ottawa_with_note(self):
        fetch = AsyncMock(return_value=discord.Embed(title="x"))
        with (
            patch("cogs.ask.ai_client.generate_ai", AsyncMock(return_value=AIResult(slots_json(city=None), "G", "q"))),
            patch("cogs.ask.weather.fetch_forecast_range", fetch),
        ):
            await _build_ask_reply("19-21日天气预报", ASK_MODE_QWEN)

        self.assertEqual(fetch.await_args.args[0], "Ottawa")
        self.assertIn("未指明城市，按 Ottawa", fetch.await_args.kwargs["preface"])

    async def test_extraction_failure_falls_back_to_original_path(self):
        ask = AsyncMock(return_value="普通回答")
        fetch = AsyncMock(side_effect=AssertionError("no forecast"))
        with (
            patch("cogs.ask.ai_client.generate_ai", AsyncMock(side_effect=ai_client.AIServiceUnavailable("x"))),
            patch("cogs.ask.ai_client.ask_ai", ask),
            patch("cogs.ask.weather.fetch_forecast_range", fetch),
        ):
            embed = await _build_ask_reply(TORONTO_Q, ASK_MODE_QWEN)

        ask.assert_awaited_once_with(TORONTO_Q, system=SYSTEM_PROMPT, use_search=False)
        self.assertEqual(embed.description, "普通回答")
        self.assertNotIn("天气数据暂不可用", embed.description)

    async def test_other_intent_falls_back_to_original_path(self):
        ask = AsyncMock(return_value="普通回答")
        with (
            patch("cogs.ask.ai_client.generate_ai", AsyncMock(return_value=AIResult('{"intent":"other"}', "G", "q"))),
            patch("cogs.ask.ai_client.ask_ai", ask),
        ):
            embed = await _build_ask_reply("天气预报模型是怎么训练的", ASK_MODE_QWEN)

        self.assertEqual(embed.description, "普通回答")

    async def test_open_meteo_failure_prefixes_notice_to_ordinary_answer(self):
        ask = AsyncMock(return_value="普通回答")
        with (
            patch("cogs.ask.ai_client.generate_ai", AsyncMock(return_value=AIResult(slots_json(), "G", "q"))),
            patch("cogs.ask.ai_client.ask_ai", ask),
            patch("cogs.ask.weather.fetch_forecast_range", AsyncMock(side_effect=asyncio.TimeoutError())),
        ):
            embed = await _build_ask_reply(TORONTO_Q, ASK_MODE_QWEN)

        self.assertTrue(embed.description.startswith(WEATHER_UNAVAILABLE_NOTICE))
        self.assertIn("普通回答", embed.description)
        ask.assert_awaited_once_with(TORONTO_Q, system=SYSTEM_PROMPT, use_search=False)

    async def test_city_not_found_shows_explicit_text(self):
        ask = AsyncMock(side_effect=AssertionError("no model answer"))
        with (
            patch("cogs.ask.ai_client.generate_ai", AsyncMock(return_value=AIResult(slots_json(city="Atlantis"), "G", "q"))),
            patch("cogs.ask.ai_client.ask_ai", ask),
            patch("cogs.ask.weather.fetch_forecast_range", AsyncMock(side_effect=CityNotFoundError("x"))),
        ):
            embed = await _build_ask_reply("Atlantis 明天天气", ASK_MODE_QWEN)

        self.assertIn("找不到城市「Atlantis」", embed.description)
        self.assertEqual(embed.footer.text, "数据来源: Open-Meteo")

    async def test_search_modes_skip_weather_intent(self):
        generate = AsyncMock(side_effect=AssertionError("no slot extraction in search modes"))
        ask = AsyncMock(return_value="gemini")
        with (
            patch("cogs.ask.ai_client.generate_ai", generate),
            patch("cogs.ask.ai_client.ask_ai", ask),
        ):
            await _build_ask_reply(TORONTO_Q, ASK_MODE_GEMINI_SEARCH)
        generate.assert_not_awaited()

    async def test_command_sends_built_embed_once(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            user=SimpleNamespace(id=1),
        )
        embed = discord.Embed(title="t")
        with patch.dict(Ask.ask.callback.__globals__, {"_build_ask_reply": AsyncMock(return_value=embed)}):
            await Ask.ask.callback(Ask(MagicMock()), interaction, TORONTO_Q)

        interaction.followup.send.assert_awaited_once_with(embed=embed)


if __name__ == "__main__":
    unittest.main()
