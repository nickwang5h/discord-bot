import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from cogs import news_digest
from cogs.news_digest import (
    DIGEST_HISTORY_TTL_SECONDS,
    MAX_MESSAGE_EMBED_CHARS,
    NewsDigest,
    _build_candidates,
    _embed_character_count,
    _filter_unchanged_candidates,
    _normalize_selection,
    _recent_history,
    _remember_delivered,
)
from core.ai_providers import AIResult
from core.feeds import FeedItem
from core.storage import JsonStore


def feed_items() -> list[FeedItem]:
    sources = (
        ("World", "BBC World"),
        ("World", "BBC World"),
        ("Canada", "Global News"),
        ("Canada", "Global News"),
        ("Finance", "WSJ Markets"),
        ("Finance", "WSJ Markets"),
        ("Finance", "CNBC"),
        ("Finance", "CNBC"),
    )
    return [
        FeedItem(
            category=category,
            source_name=publisher,
            title=f"Story {index}",
            url=f"https://example.com/story-{index}",
            summary=f"<p>RSS evidence {index}</p>",
            published_at=1_788_000_000.0 + index,
        )
        for index, (category, publisher) in enumerate(sources, start=1)
    ]


def model_payload(*ids: str) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "id": candidate_id,
                    "title": f"中文新闻标题 {index}",
                    "summary": f"依据 RSS 的摘要 {index}",
                }
                for index, candidate_id in enumerate(ids, start=1)
            ]
        },
        ensure_ascii=False,
    )


class NewsDigestContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_selects_variable_information_increments(self) -> None:
        items = feed_items()
        candidates = _build_candidates(items)
        chosen_ids = [str(item["id"]) for item in candidates[:4]]
        generate = AsyncMock(
            return_value=AIResult(model_payload(*chosen_ids), "Groq", "test-model")
        )
        cog = object.__new__(NewsDigest)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                news_digest,
                "_history_store",
                JsonStore(Path(directory) / "history.json", list),
            ),
            patch.object(news_digest, "fetch_feeds", AsyncMock(return_value=items)),
            patch.object(news_digest.ai_client, "generate_ai", generate),
        ):
            payload = await cog._build_news_digest(
                "早间",
                "早间新闻",
                use_history=True,
            )

        self.assertIsNotNone(payload)
        embeds, selected = payload
        model_input = json.loads(generate.await_args.args[0])
        self.assertEqual(len(selected), 4)
        self.assertIn("render_cost_budget", model_input)
        self.assertIn("render_cost", model_input["candidates"][0])
        self.assertEqual(model_input["candidates"][0]["publisher"], "BBC World")
        self.assertEqual(
            model_input["candidates"][0]["rss_summary"],
            "RSS evidence 1",
        )
        self.assertNotIn("url", model_input["candidates"][0])
        self.assertNotIn("evidence_hash", model_input["candidates"][0])
        self.assertIn("不要追求固定条数", generate.await_args.kwargs["system"])
        self.assertTrue(generate.await_args.kwargs["json_mode"])
        self.assertEqual(generate.await_args.kwargs["max_output_tokens"], 3000)

        self.assertEqual(len(embeds), 3)
        rendered = "\n".join(embed.description or "" for embed in embeds)
        self.assertEqual(rendered.count("]("), 4)
        for item in selected:
            self.assertIn(str(item["url"]), rendered)
            self.assertIn(str(item["digest_title"]), rendered)
            self.assertNotIn(str(item["title"]), rendered)
        self.assertLessEqual(
            sum(_embed_character_count(embed) for embed in embeds),
            MAX_MESSAGE_EMBED_CHARS,
        )
        self.assertEqual(embeds[0].footer.text, "✨ Powered by Groq (test-model)")

    def test_publishers_are_interleaved_before_model_selection(self) -> None:
        candidates = _build_candidates(feed_items())

        self.assertEqual(
            [item["publisher"] for item in candidates[:4]],
            ["BBC World", "Global News", "WSJ Markets", "CNBC"],
        )

    def test_selection_is_strict_but_has_no_fixed_count(self) -> None:
        candidates = _build_candidates(feed_items())
        selected = _normalize_selection(model_payload("N01", "N04"), candidates)
        self.assertEqual([item["id"] for item in selected], ["N01", "N04"])
        self.assertEqual(_normalize_selection('{"items": []}', candidates), [])

        unknown = json.loads(model_payload("N01", "N04"))
        unknown["items"][-1]["id"] = "N99"
        with self.assertRaisesRegex(ValueError, "未知或重复"):
            _normalize_selection(json.dumps(unknown), candidates)

        linked = json.loads(model_payload("N01"))
        linked["items"][0]["summary"] = "查看 https://forged.example 获取详情"
        with self.assertRaisesRegex(ValueError, "摘要无效"):
            _normalize_selection(json.dumps(linked), candidates)

        wrong_title = json.loads(model_payload("N01"))
        wrong_title["items"][0]["title"] = "English only"
        with self.assertRaisesRegex(ValueError, "中文标题无效"):
            _normalize_selection(json.dumps(wrong_title), candidates)

        oversized = _build_candidates(
            [
                FeedItem(
                    category="World",
                    source_name=f"Source {index}",
                    title=f"Long story {index}",
                    url=f"https://example.com/{'x' * 180}-{index}",
                    summary="Evidence",
                    published_at=None,
                )
                for index in range(20)
            ]
        )
        with self.assertRaisesRegex(ValueError, "容量"):
            _normalize_selection(
                model_payload(*(str(item["id"]) for item in oversized)),
                oversized,
            )

    def test_candidate_url_cannot_break_markdown_link(self) -> None:
        item = feed_items()[0]
        unsafe = FeedItem(
            category=item.category,
            source_name=item.source_name,
            title=item.title,
            url="https://example.com/story-(1)",
            summary=item.summary,
            published_at=item.published_at,
        )

        candidate = _build_candidates([unsafe])[0]

        self.assertEqual(candidate["url"], "https://example.com/story-%281%29")

    async def test_recent_delivery_is_filtered_and_provided_as_title_context(self) -> None:
        items = feed_items()
        candidates = _build_candidates(items)
        delivered = candidates[0]
        remaining = candidates[1]
        generate = AsyncMock(
            return_value=AIResult(
                model_payload(str(remaining["id"])),
                "Groq",
                "test-model",
            )
        )
        cog = object.__new__(NewsDigest)

        with tempfile.TemporaryDirectory() as directory:
            history_store = JsonStore(Path(directory) / "history.json", list)
            with patch.object(news_digest, "_history_store", history_store):
                _remember_delivered([delivered])
                with (
                    patch.object(
                        news_digest,
                        "fetch_feeds",
                        AsyncMock(return_value=items),
                    ),
                    patch.object(news_digest.ai_client, "generate_ai", generate),
                ):
                    await cog._build_news_digest(
                        "午后",
                        "午后新闻",
                        use_history=True,
                    )

        model_input = json.loads(generate.await_args.args[0])
        candidate_titles = {item["title"] for item in model_input["candidates"]}
        self.assertNotIn(delivered["title"], candidate_titles)
        self.assertEqual(
            model_input["recently_delivered"][0]["title"],
            delivered["title"],
        )
        self.assertNotIn("url", model_input["recently_delivered"][0])
        self.assertEqual(
            model_input["recently_delivered"][0]["rss_summary"],
            delivered["rss_summary"],
        )

    def test_same_url_can_return_only_when_its_evidence_changes(self) -> None:
        original_item = feed_items()[0]
        original = _build_candidates([original_item])[0]
        history = [
            {
                key: original[key]
                for key in (
                    "url",
                    "title",
                    "publisher",
                    "category",
                    "rss_summary",
                    "evidence_hash",
                )
            }
        ]
        history[0]["delivered_at"] = time.time()
        updated_item = FeedItem(
            category=original_item.category,
            source_name=original_item.source_name,
            title=original_item.title,
            url=original_item.url,
            summary="RSS evidence with a material update",
            published_at=original_item.published_at,
        )
        updated = _build_candidates([updated_item])[0]

        self.assertEqual(_filter_unchanged_candidates([original], history), [])
        self.assertEqual(_filter_unchanged_candidates([updated], history), [updated])

    def test_headline_rewrite_alone_is_not_a_news_update(self) -> None:
        original = _build_candidates(feed_items()[:1])[0]
        changed = {**original, "title": "Reworded headline", "evidence_hash": "changed"}
        self.assertEqual(_filter_unchanged_candidates([changed], [original]), [])
        changed["rss_summary"] = "  RSS EVIDENCE 1  "
        self.assertEqual(_filter_unchanged_candidates([changed], [original]), [])

    def test_delivery_history_expires_automatically(self) -> None:
        now = time.time()
        candidate = _build_candidates(feed_items())[0]

        with tempfile.TemporaryDirectory() as directory:
            history_store = JsonStore(Path(directory) / "history.json", list)
            with patch.object(news_digest, "_history_store", history_store):
                _remember_delivered([candidate], now=now)
                self.assertEqual(len(_recent_history(now=now + 1)), 1)
                self.assertEqual(
                    _recent_history(now=now + DIGEST_HISTORY_TTL_SECONDS + 1),
                    [],
                )

    async def test_test_delivery_does_not_change_scheduled_history(self) -> None:
        cog = object.__new__(NewsDigest)
        cog._delivery_lock = asyncio.Lock()
        candidate = {
            **_build_candidates(feed_items())[0],
            "digest_title": "中文标题",
            "digest_summary": "测试摘要",
        }
        embed = MagicMock()
        cog._build_news_digest = AsyncMock(return_value=([embed], [candidate]))
        channel = MagicMock()
        channel.send = AsyncMock()

        with tempfile.TemporaryDirectory() as directory:
            history_store = JsonStore(Path(directory) / "history.json", list)
            with patch.object(news_digest, "_history_store", history_store):
                await cog._run_news_digest(
                    channel,
                    "测试",
                    "测试新闻",
                    use_history=False,
                    record_delivery=False,
                )
                self.assertEqual(_recent_history(), [])

        channel.send.assert_awaited_once_with(embeds=[embed])

    async def test_scheduled_history_is_written_only_after_successful_send(self) -> None:
        candidate = {
            **_build_candidates(feed_items())[0],
            "digest_title": "中文标题",
            "digest_summary": "正式摘要",
        }
        embed = MagicMock()

        with tempfile.TemporaryDirectory() as directory:
            history_store = JsonStore(Path(directory) / "history.json", list)
            with patch.object(news_digest, "_history_store", history_store):
                successful = object.__new__(NewsDigest)
                successful._delivery_lock = asyncio.Lock()
                successful._build_news_digest = AsyncMock(
                    return_value=([embed], [candidate])
                )
                channel = MagicMock()
                channel.send = AsyncMock()

                await successful._run_news_digest(channel, "早间", "早间新闻")
                self.assertEqual(len(_recent_history()), 1)

                history_store.write([])
                failed = object.__new__(NewsDigest)
                failed._delivery_lock = asyncio.Lock()
                failed._build_news_digest = AsyncMock(
                    return_value=([embed], [candidate])
                )
                channel.send = AsyncMock(side_effect=RuntimeError("send failed"))

                with self.assertRaisesRegex(RuntimeError, "send failed"):
                    await failed._run_news_digest(channel, "午后", "午后新闻")
                self.assertEqual(_recent_history(), [])


if __name__ == "__main__":
    unittest.main()
