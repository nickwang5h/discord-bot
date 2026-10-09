"""Copy news material and inbox entries into the knowledge index; keep pins current.

No model is involved. Every step is idempotent (``KnowledgeStore.upsert_docs`` skips
identical rows), so a crash between a write and its cursor update only replays work.
All functions are synchronous; async callers use ``asyncio.to_thread``.

- ``sync_news``: the news pool keeps raw material for seven days; the knowledge index
  keeps it for 90 (pinned 365). Read incrementally by ``first_seen`` (``news_cursor``).
- ``sync_inbox``: inbox Markdown entries with their state; skipped while the index file
  and the item files are unchanged (``inbox_fingerprint``).
- ``sync_pins``: news that was shown, judged or saved is pinned (design §9 Q6).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import datetime
from typing import Any

from core.feedback.boards import board_of
from core.inbox import DROPPED, canonical_url, state_time
from core.knowledge.store import DROPPED_INBOX_TTL, KnowledgeDoc

logger = logging.getLogger(__name__)

NEWS_CURSOR = "news_cursor"
INBOX_FINGERPRINT = "inbox_fingerprint"
NEWS_PAGE = 500
NEWS_MAX_PAGES = 20          # at most 10,000 articles per call; the next call continues
NEWS_MAX_PAGE = 16 * NEWS_PAGE
INBOX_SYNC_VERSION = "1"     # bump when the inbox document shape changes, to force a rewrite
_HEADER_PREFIXES = ("来源：", "保存：")
_NOTE_PREFIX = "> 备注："


# -- news ------------------------------------------------------------------------


def news_doc(article) -> KnowledgeDoc:
    """One pool article (``core.news.models.Article``) as a knowledge document."""
    return KnowledgeDoc(
        id=f"news:{article.id}", kind="news", title=article.title, body=article.content,
        added_at=article.first_seen, source=article.source, category=article.category,
        board=board_of(article.source, article.category), url=article.url,
        canonical_url=canonical_url(article.url) if article.url else "",
        published_at=article.published_at, version=article.version)


def sync_news(knowledge, pool_reader, *, now: float | None = None, page: int = NEWS_PAGE,
              max_pages: int = NEWS_MAX_PAGES) -> dict[str, Any]:
    """Copy articles first seen at or after the cursor; returns ``{read, changed, cursor}``.

    One ingest batch shares one ``first_seen`` and may be committed in parts, so each
    page re-reads the cursor timestamp (``>=``) and relies on idempotent writes. A full
    page advances the cursor to its last timestamp; a full page made of that single
    timestamp is re-read with a larger page instead of being split. Each page's
    documents are written before its cursor.
    """
    cursor = float(knowledge.get_meta(NEWS_CURSOR) or 0)
    read = changed = 0
    limit = page
    for _ in range(max_pages):
        # PoolReader.since is strict (>); one ulp below the cursor makes it inclusive.
        articles = pool_reader.since(math.nextafter(cursor, -math.inf), limit=limit)
        if not articles:
            break
        last = articles[-1].first_seen
        full = len(articles) >= limit
        if full and last <= cursor:
            if limit < NEWS_MAX_PAGE:
                limit *= 2
                continue
            # A single batch larger than the biggest page: take it and step past it.
            logger.warning("知识库同步：同一批素材超过 %s 条，跳过该批其余部分", limit)
        changed += knowledge.upsert_docs(news_doc(article) for article in articles)
        read += len(articles)
        if last > cursor:
            cursor = last
            knowledge.set_meta(NEWS_CURSOR, repr(cursor))
        elif full:
            cursor = math.nextafter(cursor, math.inf)
            knowledge.set_meta(NEWS_CURSOR, repr(cursor))
        if not full:
            break
        limit = page
    knowledge.set_meta("news_synced_at", repr(time.time() if now is None else now))
    return {"read": read, "changed": changed, "cursor": cursor}


# -- inbox -----------------------------------------------------------------------


def _timestamp(value: Any, default: float) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return default


def inbox_body(markdown: str) -> str:
    """The item file without its header (title, 来源, 保存 lines), notes first.

    Notes added on a later save are appended at the end of the file; moving every note
    line to the front keeps them inside the index's body limit.
    """
    lines = markdown.splitlines()
    start = 0
    if lines and lines[0].startswith("# "):
        start = 1
    while start < len(lines) and (not lines[start].strip() or lines[start].startswith(_HEADER_PREFIXES)):
        start += 1
    rest = lines[start:]
    notes = [line for line in rest if line.startswith(_NOTE_PREFIX)]
    others = [line for line in rest if not line.startswith(_NOTE_PREFIX)]
    parts = ["\n".join(notes), "\n".join(others).strip()]
    return "\n\n".join(part for part in parts if part)


def inbox_doc(item: dict[str, Any], markdown: str, *, now: float) -> KnowledgeDoc:
    saved_at = _timestamp(item.get("saved_at"), now)
    url = item.get("url") or ""
    origin = item.get("origin") or ""
    source = item.get("source") or ""
    category = item.get("category") or ""
    return KnowledgeDoc(
        id=f"inbox:{item['id']}", kind="inbox", title=item.get("title") or url or "未命名",
        body=inbox_body(markdown), added_at=saved_at, source=source, category=category,
        board=board_of(source, category) if category else "",
        # Without a page URL the saved message's jump link is the best citation.
        url=url or (origin if origin.startswith(("https://", "http://")) else ""),
        canonical_url=url, published_at=None, state=item.get("state"),
        state_at=_timestamp(state_time(item), saved_at))


def _inbox_fingerprint(inbox_store, items: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256(INBOX_SYNC_VERSION.encode())
    digest.update(json.dumps(items, sort_keys=True, ensure_ascii=False, default=str).encode())
    for item in items:
        try:
            stat = inbox_store.path(item).stat()
            digest.update(f"{item.get('file')}:{stat.st_size}:{stat.st_mtime_ns};".encode())
        except (KeyError, OSError):
            digest.update(f"{item.get('file')}:missing;".encode())
    return digest.hexdigest()


def sync_inbox(knowledge, inbox_store, *, now: float | None = None) -> dict[str, Any]:
    """Write every inbox entry with its state; returns ``{items, changed, skipped, missing, unchanged}``.

    Skipped entirely while the index and item files are unchanged. Dropped items whose
    ``state_at`` is over 30 days old are not written again (the store's cleanup removes
    them from the index; the Markdown files stay). A missing file is skipped with a
    warning and leaves any existing document as it was.
    """
    now = time.time() if now is None else now
    items = inbox_store.items()
    fingerprint = _inbox_fingerprint(inbox_store, items)
    result = {"items": len(items), "changed": 0, "skipped": 0, "missing": 0, "unchanged": False}
    if knowledge.get_meta(INBOX_FINGERPRINT) == fingerprint:
        result["unchanged"] = True
        return result
    docs = []
    for item in items:
        if item.get("state") == DROPPED:
            saved_at = _timestamp(item.get("saved_at"), now)
            if _timestamp(state_time(item), saved_at) < now - DROPPED_INBOX_TTL:
                result["skipped"] += 1
                continue
        try:
            markdown = inbox_store.path(item).read_text(encoding="utf-8")
        except (KeyError, OSError) as error:
            logger.warning("知识库同步：收件箱文件缺失 [%s]: %s", item.get("id"), error)
            result["missing"] += 1
            continue
        docs.append(inbox_doc(item, markdown, now=now))
    result["changed"] = knowledge.upsert_docs(docs)
    knowledge.set_meta(INBOX_FINGERPRINT, fingerprint)
    return result


# -- pins ------------------------------------------------------------------------


def pin_urls(feedback_store, inbox_store) -> set[str]:
    """Canonical URLs to pin: shown, judged or 📥-saved items, plus inbox entries.

    Inbox entries count as saved unless dropped.
    """
    urls, saved_inbox_ids = feedback_store.engaged()
    urls = set(urls)
    for item in inbox_store.items():
        if item.get("url") and (item.get("state") != DROPPED or item.get("id") in saved_inbox_ids):
            urls.add(item["url"])
    return {url for url in urls if url.startswith(("https://", "http://"))}


def sync_pins(knowledge, feedback_store, inbox_store) -> int:
    """Make the pinned set exactly ``pin_urls()``; returns rows whose pin flag changed."""
    return knowledge.pin_keys(pin_urls(feedback_store, inbox_store), exclusive=True)


# -- all -------------------------------------------------------------------------


def sync_all(knowledge, *, pool_reader=None, inbox_store=None, feedback_store=None,
             now: float | None = None) -> dict[str, Any]:
    """Run news, inbox and pin sync; one failing step never stops the others.

    Returns ``{'news': dict|None, 'inbox': dict|None, 'pins': int|None, 'errors': {step: str}}``;
    a step is ``None`` when its source was not given or it failed (then listed in
    ``errors``). Pins need both the feedback and the inbox store, since the pin set is
    exclusive and a partial set would unpin the rest.
    """
    now = time.time() if now is None else now
    result: dict[str, Any] = {"news": None, "inbox": None, "pins": None, "errors": {}}
    steps = []
    if pool_reader is not None:
        steps.append(("news", lambda: sync_news(knowledge, pool_reader, now=now)))
    if inbox_store is not None:
        steps.append(("inbox", lambda: sync_inbox(knowledge, inbox_store, now=now)))
    if feedback_store is not None and inbox_store is not None:
        steps.append(("pins", lambda: sync_pins(knowledge, feedback_store, inbox_store)))
    for name, step in steps:
        try:
            result[name] = step()
        except Exception as error:  # noqa: BLE001 - isolate each step; the loop retries next round
            logger.exception("知识库同步失败 [%s]", name)
            result["errors"][name] = f"{type(error).__name__}: {error}"[:300]
    return result
