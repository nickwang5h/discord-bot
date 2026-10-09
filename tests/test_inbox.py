import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from unittest.mock import AsyncMock, MagicMock, patch

from cogs.inbox import SAVE_EMOJI, Inbox, payload_from_message
from core.inbox import DONE, PENDING, InboxStore, canonical_url, extract_article, item_id

PAGE = (
    "<html><head><title>电网储能招标</title></head><body><article><h1>电网储能招标</h1><p>"
    + "安大略省独立电力系统运营商公布了新一轮长时储能采购结果。" * 12
    + "</p></article></body></html>"
)


def message(content="", *, bot=False, embeds=()):
    return SimpleNamespace(content=content, author=SimpleNamespace(bot=bot), embeds=list(embeds))


def embed(title="", description="", url=None, fields=()):
    return SimpleNamespace(title=title, description=description, url=url, fields=list(fields))


class InboxStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = InboxStore(Path(self.temp.name))
        self.now = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

    def test_item_file_holds_source_note_summary_and_body(self):
        item, created = self.store.save(
            url="https://example.com/a?utm_source=x#top", title="标题", body="正文内容",
            note="和储能有关", summary="摘要内容", now=self.now)
        self.assertTrue(created)
        self.assertEqual(item["url"], "https://example.com/a")
        text = self.store.path(item).read_text(encoding="utf-8")
        for expected in ("# 标题", "来源：https://example.com/a", "> 备注：和储能有关",
                         "## 摘要", "摘要内容", "## 正文", "正文内容"):
            self.assertIn(expected, text)
        self.assertTrue(item["file"].startswith("2026-10-05-标题-"))

    def test_same_source_is_one_item_and_reopens_with_the_new_note(self):
        first, _ = self.store.save(url="https://example.com/a", title="标题", body="正文", now=self.now)
        self.store.set_state(first["id"], DONE)
        again, created = self.store.save(
            url="https://example.com/a/?utm_medium=y", title="别的标题", body="别的", note="再看一次")
        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["state"], PENDING)
        self.assertEqual(len(list(self.store.items_dir.iterdir())), 1)
        self.assertIn("> 备注：再看一次", self.store.path(first).read_text(encoding="utf-8"))

    def test_pending_excludes_finished_items_and_cards_resolve_to_items(self):
        first, _ = self.store.save(url=None, title="想法一", body="想法一", now=self.now)
        second, _ = self.store.save(url=None, title="想法二", body="想法二", now=self.now)
        self.store.set_card(first["id"], 10, 111)
        self.store.set_state(self.store.by_card(111)["id"], DONE)
        self.assertEqual([item["id"] for item in self.store.pending()], [second["id"]])
        self.assertIsNone(self.store.by_card(999))

    def test_source_and_via_are_optional_attribution(self):
        item, _ = self.store.save(url="https://example.com/c", title="标题", body="正文",
                                  source="CBC Ottawa", via="button", now=self.now)
        self.assertEqual((item["source"], item["via"]), ("CBC Ottawa", "button"))
        plain, _ = self.store.save(url="https://example.com/d", title="标题", body="正文", now=self.now)
        self.assertNotIn("source", plain)
        self.assertNotIn("via", plain)

    def test_entries_written_before_attribution_still_read(self):
        ident = item_id("https://example.com/old", "")
        legacy = {"id": ident, "url": "https://example.com/old", "title": "旧条目", "saved_at": "2026-01-01T00:00:00+00:00",
                  "state": PENDING, "origin": "", "file": "old.md", "chars": 0, "card_message_id": 42}
        self.store._index.update(lambda index: {**index, ident: legacy})
        self.assertEqual(self.store.by_card(42)["id"], ident)
        self.assertEqual([item["id"] for item in self.store.pending()], [ident])
        again, created = self.store.save(url="https://example.com/old", title="旧条目", body="", source="X")
        self.assertFalse(created)
        self.assertNotIn("source", again)

    def test_canonical_url_keeps_meaningful_query(self):
        self.assertEqual(canonical_url("HTTPS://Example.com/watch/?v=1&spm=2"),
                         "https://example.com/watch?v=1")


class ExtractionTests(unittest.TestCase):
    def test_article_text_and_title_are_extracted(self):
        article = extract_article(PAGE)
        self.assertEqual(article.title, "电网储能招标")
        self.assertIn("长时储能采购结果", article.text)

    def test_page_without_real_text_is_not_an_article(self):
        self.assertIsNone(extract_article("<html><body><p>登录后查看</p></body></html>"))


class PayloadTests(unittest.TestCase):
    def test_person_message_gives_link_and_their_words_as_note(self):
        payload = payload_from_message(message("这个值得细看 https://example.com/a"))
        self.assertEqual(payload.url, "https://example.com/a")
        self.assertEqual(payload.note, "这个值得细看")

    def test_person_message_without_link_is_saved_as_a_thought(self):
        payload = payload_from_message(message("储能招标和电价的关系要查一下"))
        self.assertIsNone(payload.url)
        self.assertEqual(payload.summary, "储能招标和电价的关系要查一下")

    def test_bot_summary_card_takes_the_link_from_the_message_it_answered(self):
        card = message(bot=True, embeds=[embed("🔗 网页内容总结", "- 要点一\n- 要点二")])
        payload = payload_from_message(card, message("看看 https://example.com/b"))
        self.assertEqual(payload.url, "https://example.com/b")
        self.assertEqual(payload.title, "🔗 网页内容总结")
        self.assertIn("要点一", payload.summary)

    def test_bot_news_card_without_single_link_keeps_its_text(self):
        field = SimpleNamespace(name="科技", value="某公司发布新模型")
        payload = payload_from_message(message(bot=True, embeds=[embed("综合新闻", "今日要点", fields=[field])]))
        self.assertIsNone(payload.url)
        self.assertIn("**科技**\n某公司发布新模型", payload.summary)


class OwnerOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_reactions_from_anyone_but_the_owner_are_ignored(self):
        bot = MagicMock()
        bot.is_owner = AsyncMock(side_effect=lambda user: user.id == 1)
        with tempfile.TemporaryDirectory() as directory, patch("cogs.inbox.STATE_ROOT", Path(directory)):
            cog = Inbox(bot)
            await cog.on_raw_reaction_add(SimpleNamespace(emoji=SAVE_EMOJI, user_id=2, channel_id=5, message_id=6))
            bot.get_channel.assert_not_called()
            bot.get_channel.return_value = None
            await cog.on_raw_reaction_add(SimpleNamespace(emoji=SAVE_EMOJI, user_id=1, channel_id=5, message_id=6))
            bot.get_channel.assert_called_once_with(5)


class SavePayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_payload_posts_card_and_keeps_source(self):
        bot = MagicMock()
        bot.get_channel.return_value = None
        card = SimpleNamespace(id=77, channel=SimpleNamespace(id=5))
        fallback = SimpleNamespace(send=AsyncMock(return_value=card))
        with tempfile.TemporaryDirectory() as directory, patch("cogs.inbox.STATE_ROOT", Path(directory)), \
                patch("cogs.inbox.settings.get_setting", return_value=None):
            cog = Inbox(bot)
            cog._article = AsyncMock(return_value=None)
            payload = payload_from_message(message("https://example.com/e"))
            item = await cog.save_payload(payload, origin="jump", fallback=fallback, source="BBC", via="button")
            self.assertEqual((item["source"], item["via"]), ("BBC", "button"))
            fallback.send.assert_awaited_once()
            self.assertEqual(cog.store.by_card(77)["id"], item["id"])
            again = await cog.save_payload(payload, origin="jump", fallback=fallback)
            self.assertEqual(again["id"], item["id"])
            fallback.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
