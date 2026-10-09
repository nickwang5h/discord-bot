"""Saved-for-later items: one Markdown file per source and a small JSON index.

No model is involved. An item is the text itself (extracted article or the saved
message), the reason it was saved, and whether it has been read.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import trafilatura

from core.storage import JsonStore

PENDING, DONE, DROPPED = "pending", "done", "dropped"
STATES = {PENDING, DONE, DROPPED}
MAX_BODY_CHARS = 200_000
_TRACKING = re.compile(r"^(utm_|spm$|from$|share_|vd_source$|fbclid$|gclid$|ref$)")
_SLUG_UNSAFE = re.compile(r"[^\w一-鿿]+")


@dataclass(frozen=True)
class Article:
    title: str
    text: str


def canonical_url(url: str) -> str:
    """Same page, same item: drop fragments and tracking parameters."""
    parts = urlsplit(url.strip())
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
         if not _TRACKING.match(key.lower())]
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def item_id(url: str | None, text: str) -> str:
    key = canonical_url(url) if url else " ".join(text.split())
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def extract_article(html: str) -> Article | None:
    """Readable Markdown body and title from a fetched page; None when nothing usable."""
    text = trafilatura.extract(html, output_format="markdown", include_links=False)
    if not text or len(text.strip()) < 200:
        return None
    metadata = trafilatura.extract_metadata(html)
    title = (metadata.title if metadata and metadata.title else "").strip()
    return Article(title=title, text=text.strip()[:MAX_BODY_CHARS])


def state_time(item: dict[str, Any]) -> str:
    """When the item reached its current state; entries older than `state_at` use `saved_at`."""
    return item.get("state_at") or item["saved_at"]


def _slug(title: str) -> str:
    return _SLUG_UNSAFE.sub("-", title).strip("-")[:40] or "item"


def _one_line(text: str) -> str:
    return " ".join(text.split())


class InboxStore:
    def __init__(self, root: Path):
        self.root = root
        self.items_dir = root / "items"
        self._index = JsonStore(root / "index.json", dict)

    def get(self, identifier: str) -> dict[str, Any] | None:
        return self._index.read().get(identifier)

    def by_card(self, message_id: int) -> dict[str, Any] | None:
        for item in self._index.read().values():
            if item.get("card_message_id") == message_id:
                return item
        return None

    def pending(self) -> list[dict[str, Any]]:
        items = [item for item in self._index.read().values() if item["state"] == PENDING]
        return sorted(items, key=lambda item: item["saved_at"], reverse=True)

    def items(self) -> list[dict[str, Any]]:
        """Every item (pending, done and dropped), newest saved first."""
        return sorted(self._index.read().values(), key=lambda item: item["saved_at"], reverse=True)

    def path(self, item: dict[str, Any]) -> Path:
        return self.items_dir / item["file"]

    def save(
        self,
        *,
        url: str | None,
        title: str,
        body: str,
        note: str = "",
        summary: str = "",
        origin: str = "",
        source: str | None = None,
        via: str | None = None,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Store an item, or add the note to the one already saved for this source.

        `source` (feed source name) and `via` (e.g. `button`) are optional and only
        written when given, so older index entries without them stay valid.
        Returns the item and whether it is new.
        """
        identifier = item_id(url, body or summary or title)
        existing = self.get(identifier)
        if existing:
            if note:
                with self.path(existing).open("a", encoding="utf-8") as file:
                    file.write(f"\n> 备注：{_one_line(note)}\n")
            if existing["state"] != PENDING:
                existing = self.set_state(identifier, PENDING)
            return existing, False

        moment = now or datetime.now().astimezone()
        title = _one_line(title)[:200] or (url or "未命名")
        item = {
            "id": identifier,
            "url": canonical_url(url) if url else "",
            "title": title,
            "saved_at": moment.isoformat(timespec="seconds"),
            "state": PENDING,
            "origin": origin,
            "file": f"{moment:%Y-%m-%d}-{_slug(title)}-{identifier}.md",
            "chars": len(body),
        }
        if source:
            item["source"] = source
        if via:
            item["via"] = via
        lines = [f"# {title}", ""]
        if item["url"]:
            lines += [f"来源：{item['url']}"]
        lines += [f"保存：{item['saved_at']}", ""]
        if note:
            lines += [f"> 备注：{_one_line(note)}", ""]
        if summary:
            lines += ["## 摘要", "", summary.strip(), ""]
        if body:
            lines += ["## 正文", "", body.strip(), ""]
        self.items_dir.mkdir(parents=True, exist_ok=True)
        self.path(item).write_text("\n".join(lines), encoding="utf-8")

        def add(index: dict[str, Any]) -> dict[str, Any]:
            index[identifier] = item
            return index

        self._index.update(add)
        return item, True

    def set_card(self, identifier: str, channel_id: int, message_id: int) -> None:
        def update(index: dict[str, Any]) -> dict[str, Any]:
            index[identifier].update(card_channel_id=channel_id, card_message_id=message_id)
            return index

        self._index.update(update)

    def set_state(self, identifier: str, state: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Change the state; an actual change also records `state_at` (repeats keep it)."""
        if state not in STATES:
            raise ValueError(f"未知状态: {state}")
        moment = (now or datetime.now().astimezone()).isoformat(timespec="seconds")

        def update(index: dict[str, Any]) -> dict[str, Any]:
            item = index[identifier]
            if item["state"] != state:
                item["state"] = state
                item["state_at"] = moment
            return index

        return self._index.update(update)[identifier]
