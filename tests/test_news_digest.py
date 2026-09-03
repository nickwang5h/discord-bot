import json
import unittest
from unittest.mock import AsyncMock, patch

from cogs.news_digest import (
    MAX_DIGEST_DESCRIPTION_CHARS,
    NewsDigest,
    _build_candidates,
    _normalize_selection,
)
from core.ai_providers import AIResult
from core.feeds import FeedItem


def feed_items() -> list[FeedItem]:
    categories = (
        ("World", "BBC World"),
        ("World", "BBC World"),
        ("Canada", "Global News"),
        ("Canada", "Global News"),
        ("Finance", "WSJ Markets"),
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
        for index, (category, publisher) in enumerate(categories, start=1)
    ]


def model_payload() -> str:
    return json.dumps(
        {
            "items": [
                {"id": f"N{index:02d}", "summary": f"依据 RSS 的摘要 {index}"}
                for index in range(1, 7)
            ]
        },
        ensure_ascii=False,
    )


class NewsDigestContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_selects_ids_while_source_fields_are_restored(self) -> None:
        items = feed_items()
        generate = AsyncMock(
            return_value=AIResult(model_payload(), "Groq", "test-model")
        )
        cog = object.__new__(NewsDigest)

        with (
            patch("cogs.news_digest.fetch_feeds", AsyncMock(return_value=items)),
            patch("cogs.news_digest.ai_client.generate_ai", generate),
        ):
            embed = await cog._build_news_digest("早间", "早间新闻")

        model_input = generate.await_args.args[0]
        self.assertIn('"publisher":"BBC World"', model_input)
        self.assertIn('"rss_summary":"RSS evidence 1"', model_input)
        self.assertNotIn("https://example.com/story-1", model_input)
        self.assertTrue(generate.await_args.kwargs["json_mode"])
        self.assertEqual(generate.await_args.kwargs["max_output_tokens"], 1200)

        self.assertEqual(embed.description.count("]("), 6)
        for index in range(1, 7):
            self.assertIn(f"https://example.com/story-{index}", embed.description)
            self.assertIn(f"Story {index}", embed.description)
        self.assertIn("BBC World", embed.description)
        self.assertIn("Global News", embed.description)
        self.assertIn("WSJ Markets", embed.description)
        self.assertLessEqual(len(embed.description), MAX_DIGEST_DESCRIPTION_CHARS)
        self.assertNotIn("内容过长", embed.description)
        self.assertEqual(embed.footer.text, "✨ Powered by Groq (test-model)")

    def test_selection_rejects_unknown_ids_and_model_links(self) -> None:
        candidates = _build_candidates(feed_items())
        unknown = json.loads(model_payload())
        unknown["items"][-1]["id"] = "N99"
        with self.assertRaisesRegex(ValueError, "未知或重复"):
            _normalize_selection(json.dumps(unknown), candidates)

        linked = json.loads(model_payload())
        linked["items"][0]["summary"] = "查看 https://forged.example 获取详情"
        with self.assertRaisesRegex(ValueError, "摘要无效"):
            _normalize_selection(json.dumps(linked), candidates)

        wrong_type = json.loads(model_payload())
        wrong_type["items"][0]["id"] = 1
        with self.assertRaisesRegex(ValueError, "条目结构无效"):
            _normalize_selection(json.dumps(wrong_type), candidates)

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


if __name__ == "__main__":
    unittest.main()
