import json
import time
import unittest

from core.news.topics import general as news_digest
from core.news.topics.general import (
    _build_candidates, _filter_unchanged_candidates, _normalize_selection,
)
from core.feeds import FeedItem


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


class NewsDigestContractTests(unittest.TestCase):
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


    def test_publisher_cap_keeps_other_sources_and_validates_dropped_items(self) -> None:
        items = [
            FeedItem(
                category="World" if index < 4 else "Tech",
                source_name="BBC World" if index < 4 else "Ars Technica",
                title=f"Story {index}",
                url=f"https://example.com/diverse-{index}",
                summary="Evidence",
                published_at=None,
            )
            for index in range(5)
        ]
        candidates = _build_candidates(items)
        bbc = [str(item["id"]) for item in candidates if item["publisher"] == "BBC World"]
        tech = next(str(item["id"]) for item in candidates if item["category"] == "Tech")
        selected = _normalize_selection(model_payload(*bbc, tech), candidates)
        self.assertEqual([item["id"] for item in selected], bbc[:3] + [tech])
        embeds = news_digest._build_digest_embeds("早间新闻", selected, "test")
        self.assertEqual(len(embeds), 2)
        self.assertIn("科技与 AI", embeds[1].title)
        self.assertIn("Ars Technica", embeds[1].description)
        with self.assertRaisesRegex(ValueError, "未知或重复"):
            _normalize_selection(model_payload(*bbc, bbc[-1]), candidates)
        invalid = json.loads(model_payload(*bbc))
        invalid["items"][-1]["summary"] = "https://forged.example/"
        with self.assertRaisesRegex(ValueError, "摘要无效"):
            _normalize_selection(json.dumps(invalid), candidates)


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
