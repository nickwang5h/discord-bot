import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anthropic
import httpx2

from cogs.health import claude_status_line
from core import ai_client, claude_budget
from core.ai_providers import AIResult

FREE = AIResult("free", "Groq", "qwen")
SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    "required": ["items"],
    "additionalProperties": False,
}
FAKE_KEY = "sk-ant-test-not-a-real-key-123456"


def _response(text="ok", stop_reason="end_turn", input_tokens=1000, output_tokens=500):
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        model=ai_client.CLAUDE_MODEL,
    )


def _status_error(cls, status, message="error", *, headers=None, body=None):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, headers=headers or {})
    return cls(message, response=response, body=body)


def _request():
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


class ClaudeTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.usage_file = Path(self.temp.name) / "data" / "claude_usage.json"
        for target in (
            patch.object(claude_budget, "USAGE_FILE", self.usage_file),
            patch.object(ai_client, "claude_registered", True),
            patch.object(ai_client, "gemini_cooldown_until", 0.0),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.create = AsyncMock(return_value=_response())
        self.fake = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        client_patch = patch.object(ai_client, "_claude_client", self.fake)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.groq = AsyncMock(return_value=FREE)
        groq_patch = patch.object(ai_client, "_ask_groq", self.groq)
        groq_patch.start()
        self.addCleanup(groq_patch.stop)

    def usage(self):
        return json.loads(self.usage_file.read_text(encoding="utf-8"))

    def today(self):
        return self.usage()["days"][claude_budget.day_key(time.time())]


class RoutingTests(ClaudeTestCase):
    async def test_no_route_never_calls_claude(self):
        result = await ai_client.generate_ai("hello", json_mode=True, json_schema=SCHEMA)
        self.assertIs(result, FREE)
        self.create.assert_not_awaited()
        self.assertFalse(self.usage_file.exists())

    async def test_unknown_route_and_search_route_use_free_chain(self):
        self.assertIs(await ai_client.generate_ai("hello", route="general.news"), FREE)
        with patch.object(ai_client, "_ask_gemini", AsyncMock(return_value=FREE)):
            self.assertIs(await ai_client.generate_ai("hi", use_search=True, route="recall.answer"), FREE)
        self.create.assert_not_awaited()

    async def test_free_chain_receives_unchanged_arguments_after_claude_failure(self):
        self.create.side_effect = _status_error(anthropic.InternalServerError, 500)
        await ai_client.generate_ai(
            "text", system="sys", json_mode=True, max_output_tokens=3000,
            route="personal.following", json_schema=SCHEMA,
        )
        self.groq.assert_awaited_once_with("text", "sys", json_mode=True, max_output_tokens=3000)

    async def test_request_shape_has_effort_schema_and_no_thinking_or_prefill(self):
        self.create.return_value = _response('{"items": []}')
        result = await ai_client.generate_ai(
            "text", system="sys", json_mode=True, route="personal.following", json_schema=SCHEMA,
        )
        self.assertEqual(result, AIResult('{"items": []}', "Claude", "claude-opus-5-5"))
        kwargs = self.create.await_args.kwargs
        self.assertEqual(kwargs["model"], "claude-opus-5-5")
        self.assertEqual(kwargs["max_tokens"], 6000)
        self.assertEqual(kwargs["system"], "sys")
        self.assertEqual(kwargs["messages"], [{"role": "user", "content": "text"}])
        self.assertEqual(
            kwargs["output_config"],
            {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        for forbidden in ("thinking", "tool_choice", "tools", "stream", "fallbacks", "temperature"):
            self.assertNotIn(forbidden, kwargs)
        self.assertNotIn("assistant", [message["role"] for message in kwargs["messages"]])
        self.groq.assert_not_awaited()

    async def test_every_route_sets_explicit_effort(self):
        for route, spec in ai_client.CLAUDE_ROUTES.items():
            self.create.reset_mock()
            self.create.return_value = _response(input_tokens=10, output_tokens=10)
            result = await ai_client.generate_ai("short", route=route)
            self.assertEqual(result.provider, "Claude", route)
            kwargs = self.create.await_args.kwargs
            self.assertEqual(kwargs["output_config"], {"effort": spec.effort})
            self.assertEqual(kwargs["max_tokens"], spec.max_tokens)
            self.assertIn(spec.effort, ("low", "medium"))

    async def test_json_mode_without_schema_skips_claude(self):
        result = await ai_client.generate_ai("text", json_mode=True, route="recall.plan")
        self.assertIs(result, FREE)
        self.create.assert_not_awaited()

    async def test_only_text_blocks_are_returned(self):
        result = await ai_client.generate_ai("q", route="recall.answer")
        self.assertEqual(result.text, "ok")

    async def test_input_over_route_limit_uses_free_chain(self):
        result = await ai_client.generate_ai("x" * 1001, route="watch.aliases")
        self.assertIs(result, FREE)
        self.create.assert_not_awaited()

    async def test_unregistered_provider_uses_free_chain(self):
        with patch.object(ai_client, "claude_registered", False), patch.object(ai_client, "_claude_client", None):
            self.assertIs(await ai_client.generate_ai("q", route="recall.answer"), FREE)
        self.create.assert_not_awaited()


class RegistrationTests(unittest.TestCase):
    def test_missing_key_does_not_register_or_warn(self):
        with (
            patch.object(ai_client, "claude_registered", True),
            patch.object(ai_client, "_claude_client", object()),
            patch.object(ai_client.settings, "get_secret", return_value=None),
            self.assertLogs(ai_client.logger, level="INFO") as logs,
        ):
            self.assertFalse(ai_client.reload_claude_client())
            self.assertFalse(ai_client.claude_registered)
            self.assertIsNone(ai_client._get_claude_client())
        self.assertTrue(all(record.levelno == logging.INFO for record in logs.records))

    def test_client_is_built_lazily_without_retries(self):
        with (
            patch.object(ai_client, "claude_registered", False),
            patch.object(ai_client, "_claude_client", None),
            patch.object(ai_client.settings, "get_secret", return_value=FAKE_KEY),
        ):
            self.assertTrue(ai_client.reload_claude_client())
            self.assertIsNone(ai_client._claude_client)
            claude = ai_client._get_claude_client()
            self.assertIsInstance(claude, anthropic.AsyncAnthropic)
            self.assertEqual(claude.max_retries, 0)
            self.assertEqual(claude.timeout, 90.0)


class FailureTests(ClaudeTestCase):
    async def _fallback(self, error=None, response=None, schema=None):
        if error is not None:
            self.create.side_effect = error
        if response is not None:
            self.create.return_value = response
        result = await ai_client.generate_ai("q", route="recall.answer", json_schema=schema)
        self.assertIs(result, FREE)
        self.groq.assert_awaited_once()
        return self.usage()

    async def test_credit_exhausted_disables_until_next_utc_day_and_books_zero(self):
        error = _status_error(
            anthropic.BadRequestError, 400,
            "Your credit balance is too low to access the Anthropic API.",
        )
        usage = await self._fallback(error)
        self.assertEqual(usage["disabled_reason"], "额度耗尽")
        self.assertEqual(usage["disabled_until"], claude_budget.next_utc_midnight(time.time()))
        self.assertEqual(self.today()["spent_usd"], 0.0)
        self.assertEqual(self.today()["reserved_usd"], 0.0)

        self.groq.reset_mock()
        self.create.reset_mock()
        self.assertIs(await ai_client.generate_ai("q", route="recall.answer"), FREE)
        self.create.assert_not_awaited()

    async def test_payment_required_status_disables(self):
        usage = await self._fallback(_status_error(anthropic.APIStatusError, 402, "payment"))
        self.assertEqual(usage["disabled_reason"], "额度耗尽")

    async def test_billing_error_type_disables(self):
        body = {"type": "error", "error": {"type": "billing_error", "message": "x"}}
        usage = await self._fallback(_status_error(anthropic.APIStatusError, 402, "x", body=body))
        self.assertEqual(usage["disabled_reason"], "额度耗尽")

    async def test_rate_limit_uses_retry_after(self):
        start = time.time()
        usage = await self._fallback(
            _status_error(anthropic.RateLimitError, 429, headers={"retry-after": "120"})
        )
        self.assertGreaterEqual(usage["cooldown_until"], start + 119)
        self.assertLess(usage["cooldown_until"], start + 130)
        self.assertEqual(usage["disabled_until"], 0.0)

    async def test_rate_limit_without_retry_after_cools_60_seconds(self):
        start = time.time()
        usage = await self._fallback(_status_error(anthropic.RateLimitError, 429))
        self.assertGreaterEqual(usage["cooldown_until"], start + 59)
        self.assertLess(usage["cooldown_until"], start + 70)

    async def test_server_errors_cool_down(self):
        for cls, status in ((anthropic.InternalServerError, 500), (anthropic.OverloadedError, 529)):
            with self.subTest(status=status):
                self.groq.reset_mock()
                self.usage_file.unlink(missing_ok=True)
                usage = await self._fallback(_status_error(cls, status))
                self.assertGreater(usage["cooldown_until"], time.time() + 50)

    async def test_network_and_timeouts_fall_through_without_cooldown(self):
        for error in (
            anthropic.APIConnectionError(request=_request()),
            anthropic.APITimeoutError(request=_request()),
            TimeoutError(),
        ):
            with self.subTest(error=type(error).__name__):
                self.groq.reset_mock()
                self.usage_file.unlink(missing_ok=True)
                usage = await self._fallback(error)
                self.assertEqual(usage["cooldown_until"], 0.0)
                self.assertEqual(usage["disabled_until"], 0.0)

    async def test_auth_permission_and_not_found_disable_for_the_day(self):
        for cls, status in (
            (anthropic.AuthenticationError, 401),
            (anthropic.PermissionDeniedError, 403),
            (anthropic.NotFoundError, 404),
        ):
            with self.subTest(status=status):
                self.groq.reset_mock()
                self.usage_file.unlink(missing_ok=True)
                with self.assertLogs(ai_client.logger, level="ERROR") as logs:
                    usage = await self._fallback(_status_error(cls, status))
                self.assertEqual(usage["disabled_until"], claude_budget.next_utc_midnight(time.time()))
                self.assertNotIn(FAKE_KEY, "\n".join(logs.output))

    async def test_other_bad_request_logs_error_without_disabling(self):
        with self.assertLogs(ai_client.logger, level="ERROR"):
            usage = await self._fallback(_status_error(anthropic.BadRequestError, 400, "invalid schema"))
        self.assertEqual(usage["disabled_until"], 0.0)
        self.assertEqual(usage["cooldown_until"], 0.0)

    async def test_refusal_and_truncation_fall_through_and_book_actual_usage(self):
        for stop_reason in ("refusal", "max_tokens"):
            with self.subTest(stop_reason=stop_reason):
                self.groq.reset_mock()
                self.usage_file.unlink(missing_ok=True)
                await self._fallback(response=_response(stop_reason=stop_reason))
                self.assertAlmostEqual(self.today()["spent_usd"], 1000 * 4e-6 + 500 * 20e-6)
                self.assertEqual(self.today()["reserved_usd"], 0.0)

    async def test_invalid_json_with_schema_falls_through(self):
        await self._fallback(response=_response("not json"), schema=SCHEMA)


class BudgetTests(ClaudeTestCase):
    async def test_success_settles_actual_cost_and_releases_reservation(self):
        await ai_client.generate_ai("q", route="recall.answer")
        day = self.today()
        self.assertAlmostEqual(day["spent_usd"], 0.014)
        self.assertEqual(day["reserved_usd"], 0.0)
        self.assertEqual(day["calls"], 1)
        self.assertEqual(day["routes"], {"recall.answer": 1})
        self.assertEqual(day["input_tokens"], 1000)
        self.assertEqual(day["output_tokens"], 500)
        self.assertAlmostEqual(self.usage()["months"][claude_budget.month_key(time.time())], 0.014)

    async def test_route_daily_call_limit(self):
        for _ in range(ai_client.CLAUDE_ROUTES["personal.following"].daily_calls):
            self.assertEqual((await ai_client.generate_ai("q", route="personal.following")).provider, "Claude")
        self.assertIs(await ai_client.generate_ai("q", route="personal.following"), FREE)
        self.assertEqual(self.create.await_count, 4)
        self.assertEqual((await ai_client.generate_ai("q", route="recall.plan")).provider, "Claude")

    async def test_daily_usd_limit_counts_reserved_worst_case(self):
        now = time.time()
        reservation = claude_budget.reserve("x", 0.50, 99, now=now)
        self.assertIsNotNone(reservation)
        # worst case for personal.following is >= 6000 * $20/M = $0.12, 0.50 + 0.12 > 0.60
        self.assertIs(await ai_client.generate_ai("q", route="personal.following"), FREE)
        self.create.assert_not_awaited()
        # recall.plan worst case ~ $0.02 still fits under the daily cap
        self.assertEqual((await ai_client.generate_ai("q", route="recall.plan")).provider, "Claude")

    async def test_monthly_usd_limit(self):
        now = time.time()
        self.usage_file.parent.mkdir(parents=True)
        self.usage_file.write_text(json.dumps({"months": {claude_budget.month_key(now): 18.99}}))
        self.assertIs(await ai_client.generate_ai("q", route="recall.plan"), FREE)
        self.create.assert_not_awaited()

    async def test_daily_calls_limit_from_settings(self):
        with patch.object(claude_budget.settings, "get_setting", return_value={"daily_calls": 1}):
            self.assertEqual((await ai_client.generate_ai("q", route="recall.plan")).provider, "Claude")
            self.assertIs(await ai_client.generate_ai("q", route="recall.answer"), FREE)
        self.assertEqual(self.create.await_count, 1)

    def test_limits_are_clamped_and_invalid_values_use_defaults(self):
        with patch.object(
            claude_budget.settings, "get_setting",
            return_value={"daily_usd": 9, "monthly_usd": "x", "daily_calls": 0, "other": 1},
        ):
            self.assertEqual(
                claude_budget.limits(),
                {"daily_usd": 1.5, "monthly_usd": 19.0, "daily_calls": 1},
            )
        with patch.object(claude_budget.settings, "get_setting", return_value=[]):
            self.assertEqual(claude_budget.limits()["daily_usd"], 0.60)

    def test_new_utc_day_resets_daily_usage_but_keeps_month(self):
        day1 = 1_791_000_000.0  # mid-month UTC timestamp
        reservation = claude_budget.reserve("recall.answer", 0.58, 15, now=day1)
        claude_budget.settle(reservation, 0, 25_000)  # $0.50
        self.assertIsNone(claude_budget.reserve("recall.answer", 0.2, 15, now=day1))
        day2 = day1 + 86400
        self.assertIsNotNone(claude_budget.reserve("recall.answer", 0.2, 15, now=day2))
        status = claude_budget.status(now=day2)
        self.assertEqual(status["today_usd"], 0.0)
        if claude_budget.month_key(day1) == claude_budget.month_key(day2):
            self.assertAlmostEqual(status["month_usd"], 0.5)

    def test_unreleased_reservation_after_crash_stays_conservative(self):
        now = time.time()
        claude_budget.reserve("recall.answer", 0.55, 15, now=now)  # never settled
        self.assertIsNone(claude_budget.reserve("recall.answer", 0.1, 15, now=now))
        self.assertTrue(claude_budget.status(now=now)["daily_exhausted"] is False)

    def test_old_days_are_pruned(self):
        old = time.time() - 50 * 86400
        claude_budget.settle(claude_budget.reserve("recall.plan", 0.01, 15, now=old), 1, 1)
        claude_budget.reserve("recall.plan", 0.01, 15)
        self.assertEqual(list(self.usage()["days"]), [claude_budget.day_key(time.time())])

    def test_estimate_counts_cjk_conservatively(self):
        self.assertEqual(claude_budget.estimate_input_tokens("储能关税"), 6)
        self.assertEqual(claude_budget.estimate_input_tokens("abcdef", "abc"), 3)


class HealthTests(ClaudeTestCase):
    def test_status_line_states_and_no_secret(self):
        with patch.object(ai_client.settings, "get_secret", return_value=FAKE_KEY):
            status = ai_client.get_provider_status()
        line = claude_status_line(status)
        self.assertEqual(line, "- Claude Opus 5.5: ✅ 可用 · 今日 $0.00/$0.60 · 0 次 · 本月 $0.00/$19")
        self.assertNotIn(FAKE_KEY, repr(status))

        claude_budget.cooldown_until(time.time() + 30)
        self.assertIn("⏳ 冷却", claude_status_line(ai_client.get_provider_status()))
        claude_budget.disable_until(claude_budget.next_utc_midnight(time.time()), "额度耗尽")
        self.assertIn("⛔ 额度耗尽至 00:00 UTC", claude_status_line(ai_client.get_provider_status()))

    def test_status_line_unconfigured_and_daily_full(self):
        self.assertEqual(claude_status_line({"claude": False}), "- Claude Opus 5.5: ➖ 未配置")
        claude_budget.reserve("recall.answer", 0.60, 15)
        self.assertIn("⏸️ 今日预算已满", claude_status_line(ai_client.get_provider_status()))


if __name__ == "__main__":
    unittest.main()
