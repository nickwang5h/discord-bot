"""Weekly review: deterministic statistics of one ISO week, rendered for one embed.

Pure apart from read-only store queries: the inputs are the store objects
(``FeedbackStore``, ``InboxStore``, ``KnowledgeStore``, optionally a watch store) and
only their read methods are called, so tests can seed temporary databases. No model
is called (design §9 Q7). Weeks run Monday 00:00 to Monday 00:00 in ``BOT_TIMEZONE``,
so a week that crosses a DST change is 167 or 169 hours long.

Sections (design §5.6, 1–7): exposures (personal / shared); 🆕/👌/🚫 with exact and
coarse verdicts reported apart; sources with the most 🆕, sources without any 🆕 and
cut candidates (≥ ``CUT_MIN_EXPOSURES`` exposures this week and no 🆕), one board
line; inbox saved / done / dropped / backlog; watch hits; /recall uses; the current
reader profile. A section without data shows "暂无".
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from config import TZ
from core.feedback import profile as reader_profile
from core.feedback.boards import BOARDS, LABELS
from core.inbox import DONE, DROPPED, PENDING, state_time

logger = logging.getLogger(__name__)

DAY = 86400
MAX_CHARS = 4000
TOP_SOURCES = 5
CUT_MIN_EXPOSURES = 3
TREND_WEEKS = 4
MIN_RATED_FOR_RATE = 3
LIST_LIMIT = 15
INBOX_TITLES = 5
NAME_CHARS = 40
TITLE_CHARS = 80
EMPTY = "暂无"
RECALL_USAGE = "recall"

_WEEK_RE = re.compile(r"^(\d{4})-?W(\d{1,2})$", re.IGNORECASE)
_MARKDOWN = re.compile(r"[`*_~|\[\]()<>#@\\]")


# -- weeks ---------------------------------------------------------------------


@dataclass(frozen=True)
class Week:
    key: str  # ISO week, 'YYYY-Www'
    start: datetime  # Monday 00:00 local (aware)
    end: datetime  # next Monday 00:00 local (aware)

    @property
    def start_ts(self) -> float:
        return self.start.timestamp()

    @property
    def end_ts(self) -> float:
        return self.end.timestamp()

    @property
    def label(self) -> str:
        last = self.end.date() - timedelta(days=1)
        return f"{self.start:%m/%d}–{last:%m/%d}"

    def previous(self) -> "Week":
        return week_of_date(self.start.date() - timedelta(days=7), self.start.tzinfo)


def week_of_date(day: date, tz: tzinfo | None = None) -> Week:
    tz = tz or TZ
    year, number, _ = day.isocalendar()
    monday = date.fromisocalendar(year, number, 1)
    start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    following = monday + timedelta(days=7)
    end = datetime(following.year, following.month, following.day, tzinfo=tz)
    return Week(f"{year}-W{number:02d}", start, end)


def parse_week(text: str, tz: tzinfo | None = None) -> Week:
    """'2026-W41' (or '2026W41') to a Week; ValueError for anything else."""
    match = _WEEK_RE.match((text or "").strip())
    if not match:
        raise ValueError("周的格式应为 YYYY-Www，例如 2026-W41")
    try:
        monday = date.fromisocalendar(int(match[1]), int(match[2]), 1)
    except ValueError as error:
        raise ValueError(f"没有这一周: {text}") from error
    return week_of_date(monday, tz)


def previous_week(now: float | datetime | None = None, tz: tzinfo | None = None) -> Week:
    """The full week before the one containing ``now``."""
    tz = tz or TZ
    if now is None:
        now = time.time()
    moment = now.astimezone(tz) if isinstance(now, datetime) else datetime.fromtimestamp(now, tz)
    return week_of_date(moment.date() - timedelta(days=7), tz)


# -- result --------------------------------------------------------------------


@dataclass(frozen=True)
class SourceLine:
    name: str
    exposures: int
    rated: int
    new: float
    known: float
    skip: float
    new_rate: float | None
    personal: bool | None = None  # None when the source is not in the configured catalog


@dataclass(frozen=True)
class WeeklyReview:
    week: Week
    exposures: dict[str, int] | None = None  # personal, shared, total
    verdicts: dict[str, Any] | None = None  # exact{}, coarse{}, total{}, rated, new_rate
    trend: tuple[tuple[str, float | None], ...] = ()
    top_sources: tuple[SourceLine, ...] = ()
    zero_new_sources: tuple[SourceLine, ...] = ()
    cut_candidates: tuple[SourceLine, ...] = ()
    unexposed_personal: tuple[str, ...] = ()
    silent_sources: tuple[str, ...] = ()  # configured sources with no material this week
    boards: tuple[dict[str, Any], ...] = ()
    inbox: dict[str, Any] | None = None
    watch: tuple[dict[str, Any], ...] | None = None
    recall_uses: int | None = None
    profile_lines: tuple[str, ...] = ()
    knowledge: dict[str, Any] | None = None
    errors: tuple[str, ...] = field(default=())

    @property
    def title(self) -> str:
        return f"📊 每周回看 · {self.week.key}（{self.week.label}）"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["week"] = {"key": self.week.key, "start": self.week.start.isoformat(),
                        "end": self.week.end.isoformat()}
        return data

    def render(self, *, max_chars: int = MAX_CHARS) -> str:
        sections = [
            ("① 推送曝光", self._exposure_lines()),
            ("② 新知 / 已知 / 不关心", self._verdict_lines()),
            ("③ 来源与板块", self._source_lines()),
            ("④ 收件箱", self._inbox_lines()),
            ("⑤ 追踪", self._watch_lines()),
            ("⑥ /recall", [f"本周使用 {self.recall_uses} 次"] if self.recall_uses is not None else []),
            ("⑦ 读者画像", list(self.profile_lines)),
        ]
        parts = []
        for heading, lines in sections:
            parts.append(f"**{heading}**")
            parts.extend(lines or [EMPTY])
        if self.knowledge:
            k = self.knowledge
            parts.append(f"-# 知识库 {k['docs']:,} 篇（新闻 {k['news']:,} · 收藏 {k['inbox']:,}）"
                         f" · 全文检索{'可用' if k['fts5'] else '不可用'}")
        if self.errors:
            parts.append(f"-# 部分统计读取失败：{'、'.join(self.errors)}")
        return _fit("\n".join(parts), max_chars)

    # -- section renderers --------------------------------------------------

    def _exposure_lines(self) -> list[str]:
        e = self.exposures
        if not e or not (e["personal"] or e["shared"]):
            return []
        return [f"个人 {e['personal']} 条 · 共享 {e['shared']} 条"]

    def _verdict_lines(self) -> list[str]:
        v = self.verdicts
        if not v or not v["rated"]:
            return []
        exact, coarse, total = v["exact"], v["coarse"], v["total"]
        lines = [
            f"精确：🆕 {exact['new']} · 👌 {exact['known']} · 🚫 {exact['skip']}",
            f"粗（反应按 1/n 加权）：🆕 {_fmt(coarse['new'])} · 👌 {_fmt(coarse['known'])} · 🚫 {_fmt(coarse['skip'])}",
            f"合计：🆕 {_fmt(total['new'])} · 👌 {_fmt(total['known'])} · 🚫 {_fmt(total['skip'])}"
            f" · 新知率 {_rate(v['new_rate'])}",
        ]
        if any(rate is not None for _, rate in self.trend):
            lines.append("近 4 周新知率：" + " → ".join(f"{key[-3:]} {_rate(rate)}" for key, rate in self.trend))
        return lines

    def _source_lines(self) -> list[str]:
        lines: list[str] = []
        if self.top_sources:
            lines.append("🆕 最多的来源：")
            for rank, s in enumerate(self.top_sources, 1):
                rate = f" · 新知率 {_rate(s.new_rate)}" if s.new_rate is not None else ""
                lines.append(f"{rank}. {_name(s.name)} 🆕 {_fmt(s.new)} · 曝光 {s.exposures}{rate}")
        if self.zero_new_sources:
            lines.append("零新知（本周有曝光）：" + _names(
                [f"{_name(s.name)}({s.exposures})" for s in self.zero_new_sources]))
        if self.cut_candidates:
            lines.append(f"✂️ 砍源候选（曝光 ≥ {CUT_MIN_EXPOSURES} 且零新知）：")
            for s in self.cut_candidates[:LIST_LIMIT]:
                tag = "" if s.personal is not False else "（共享源）"
                lines.append(f"- {_name(s.name)}{tag} 曝光 {s.exposures} · 已评 {s.rated}"
                             f" · 👌 {_fmt(s.known)} · 🚫 {_fmt(s.skip)}")
        if self.unexposed_personal:
            lines.append("本周没被推送的个人源：" + _names([_name(n) for n in self.unexposed_personal]))
        if self.silent_sources:
            lines.append("本周没有新素材的信源（疑似失效）：" + _names([_name(n) for n in self.silent_sources]))
        boards = [b for b in self.boards if b["exposures"] or b["rated"]]
        if boards:
            lines.append("板块：" + " · ".join(
                f"{b['label']} {b['exposures']}/🆕{_fmt(b['new'])}/👌{_fmt(b['known'])}/🚫{_fmt(b['skip'])}"
                for b in boards))
        return lines

    def _inbox_lines(self) -> list[str]:
        i = self.inbox
        if not i:
            return []
        if not (i["saved"] or i["done"] or i["dropped"] or i["backlog"]):
            return []
        oldest = f"（最老 {i['oldest_days']} 天）" if i.get("oldest_days") is not None else ""
        lines = [f"存入 {i['saved']} · 读完 {i['done']} · 丢弃 {i['dropped']} · 积压 {i['backlog']}{oldest}"]
        lines.extend(f"- {_plain(title, TITLE_CHARS)}" for title in i.get("titles", ())[:INBOX_TITLES])
        return lines

    def _watch_lines(self) -> list[str]:
        if not self.watch:
            return []
        return [f"- {_name(w.get('name'))}：命中 {w.get('hits', 0)} · 已推送 {w.get('delivered', 0)}"
                f" · 否决 {w.get('rejected', 0)} · 不确定 {w.get('uncertain', 0)}" for w in self.watch[:LIST_LIMIT]]


# -- formatting ----------------------------------------------------------------


def _fmt(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".") if value else "0"


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _plain(text: Any, limit: int) -> str:
    value = _MARKDOWN.sub("", " ".join(str(text or "").split()))
    return value if len(value) <= limit else f"{value[:limit - 1]}…"


def _name(text: Any) -> str:
    return f"`{_plain(text, NAME_CHARS) or '未知'}`"


def _names(items: list[str]) -> str:
    shown = "、".join(items[:LIST_LIMIT])
    return shown + (f" 等 {len(items)} 个" if len(items) > LIST_LIMIT else "")


def _fit(text: str, limit: int) -> str:
    """Cut at a line boundary so the embed never exceeds ``limit`` characters."""
    if len(text) <= limit:
        return text
    marker = "\n…（超出长度，已截断）"
    head = text[:max(0, limit - len(marker))]
    if "\n" in head:
        head = head[:head.rindex("\n")]
    return (head + marker)[:limit]


# -- building ------------------------------------------------------------------


def _timestamp(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _source_line(row: dict[str, Any], catalog: dict[str, bool]) -> SourceLine:
    decided = row["new"] + row["known"]
    rate = row["new"] / decided if decided >= MIN_RATED_FOR_RATE else None
    return SourceLine(name=str(row["name"]), exposures=row["exposures"], rated=row["rated"], new=row["new"],
                      known=row["known"], skip=row["skip"], new_rate=rate, personal=catalog.get(str(row["name"])))


def _feedback_sections(feedback, week: Week, catalog: dict[str, bool]) -> dict[str, Any]:
    start, end = week.start_ts, week.end_ts
    totals = feedback.stats(start, until=end, by=None)["totals"]
    personal = feedback.stats(start, until=end, by=None, personal=True)["totals"]
    shared = feedback.stats(start, until=end, by=None, personal=False)["totals"]
    trend = []
    span = week
    for _ in range(TREND_WEEKS):
        trend.append((span.key, feedback.stats(span.start_ts, until=span.end_ts, by=None)["totals"]["new_rate"]))
        span = span.previous()
    rows = feedback.stats(start, until=end, by="source")["rows"]
    sources = [_source_line(row, catalog) for row in rows]
    top = sorted((s for s in sources if s.new > 0), key=lambda s: (-s.new, -s.exposures, s.name))[:TOP_SOURCES]
    zero = sorted((s for s in sources if s.exposures > 0 and s.new == 0), key=lambda s: (-s.exposures, s.name))
    exposed = {s.name for s in sources if s.exposures > 0}
    board_rows = {row["name"]: row for row in feedback.stats(start, until=end, by="board")["rows"]}
    boards = []
    for board in (*BOARDS, *sorted(set(board_rows) - set(BOARDS))):
        row = board_rows.get(board)
        if row is not None:
            boards.append({"board": board, "label": LABELS.get(board, str(board)), "exposures": row["exposures"],
                           "rated": row["rated"], "new": row["new"], "known": row["known"], "skip": row["skip"]})
    return {
        "exposures": {"personal": personal["exposures"], "shared": shared["exposures"],
                      "total": totals["exposures"]},
        "verdicts": {"exact": dict(totals["exact_verdicts"]), "coarse": dict(totals["coarse_verdicts"]),
                     "total": {key: totals[key] for key in ("new", "known", "skip")},
                     "rated": totals["rated"], "new_rate": totals["new_rate"]},
        "trend": tuple(reversed(trend)),
        "top_sources": tuple(top),
        "zero_new_sources": tuple(zero),
        "cut_candidates": tuple(s for s in zero if s.exposures >= CUT_MIN_EXPOSURES),
        "unexposed_personal": tuple(sorted(name for name, is_personal in catalog.items()
                                           if is_personal and name not in exposed)),
        "boards": tuple(boards),
    }


def _profile_lines(feedback, now: float) -> tuple[str, ...]:
    rows = feedback.feedback_since(now - reader_profile.WINDOW_DAYS * DAY)
    built = reader_profile.build(rows, now)
    if built is None:
        horizon = now - reader_profile.WINDOW_DAYS * DAY
        exact = sum(1 for row in rows if not row.get("coarse") and row.get("verdict") in reader_profile.VERDICTS
                    and float(row.get("updated_at") or 0) >= horizon)
        missing = max(0, reader_profile.MIN_EXACT - exact)
        return (f"冷启动：近 {reader_profile.WINDOW_DAYS} 天精确反馈 {exact} 条，"
                f"还差 {missing} 条才开始按画像选编。",)
    lines = built.explain().split("\n")[:2]
    if built.fresh_topics:
        lines.append("常出新知：" + "、".join(_plain(t, 24) for t in built.fresh_topics[:6]))
    if built.known_topics:
        lines.append("多为已知：" + "、".join(_plain(t, 24) for t in built.known_topics[:6]))
    if built.not_interested:
        lines.append("不关心：" + "、".join(_plain(t, 24) for t in built.not_interested[:6]))
    return tuple(lines)


def _inbox_section(inbox, week: Week, now: float) -> dict[str, Any]:
    start, end = week.start_ts, week.end_ts
    saved, done, dropped, titles = 0, 0, 0, []
    backlog, oldest = 0, None
    for item in inbox.items():  # newest saved first
        saved_at = _timestamp(item.get("saved_at"))
        if saved_at is not None and start <= saved_at < end:
            saved += 1
            titles.append(item.get("title") or item.get("url") or "未命名")
        state = item.get("state")
        changed = _timestamp(state_time(item)) if item.get("saved_at") else None
        in_week = changed is not None and start <= changed < end
        if state == DONE and in_week:
            done += 1
        elif state == DROPPED and in_week:
            dropped += 1
        elif state == PENDING:
            backlog += 1
            if saved_at is not None:
                oldest = saved_at if oldest is None else min(oldest, saved_at)
    oldest_days = int(max(0.0, now - oldest) // DAY) if oldest is not None else None
    return {"saved": saved, "done": done, "dropped": dropped, "backlog": backlog,
            "oldest_days": oldest_days, "titles": tuple(titles[:INBOX_TITLES])}


def _silent_sources(knowledge, week: Week, catalog: dict[str, bool]) -> tuple[str, ...]:
    # Only meaningful when the index received any news this week (sync is running).
    if not knowledge.recent(kind="news", since=week.start_ts, limit=1):
        return ()
    return tuple(sorted(name for name in catalog
                        if not knowledge.recent(kind="news", source=name, since=week.start_ts, limit=1)))


def build_review(week: Week, *, feedback=None, inbox=None, knowledge=None, watch=None,
                 catalog: list[dict[str, Any]] | None = None, now: float | None = None) -> WeeklyReview:
    """Statistics for ``week`` from whichever stores are given (None skips a section).

    ``catalog`` is ``core.feedback.boards.source_catalog()`` (``{name, personal}``
    entries) and enables the "not pushed" and "no new material" lists. ``watch`` is an
    optional object with ``weekly_summary(start, end) -> [{name, hits, delivered,
    rejected, uncertain}]``. ``now`` (default: the current time) dates the profile and
    the inbox backlog. A section whose store raises is left empty and listed in
    ``errors``; nothing is written to any store.
    """
    now = time.time() if now is None else float(now)
    names = {str(entry["name"]): bool(entry.get("personal")) for entry in catalog or ()}
    values: dict[str, Any] = {}
    errors: list[str] = []

    def section(label: str, compute) -> None:
        try:
            result = compute()
        except Exception:  # noqa: BLE001 - one broken store must not cost the whole report
            logger.exception("周报统计失败 [%s]", label)
            errors.append(label)
            return
        if isinstance(result, dict) and label == "反馈":
            values.update(result)
        else:
            values[label] = result

    if feedback is not None:
        section("反馈", lambda: _feedback_sections(feedback, week, names))
        section("画像", lambda: _profile_lines(feedback, now))
    if inbox is not None:
        section("收件箱", lambda: _inbox_section(inbox, week, now))
    if watch is not None:
        section("追踪", lambda: tuple(watch.weekly_summary(week.start_ts, week.end_ts)))
    if knowledge is not None:
        section("recall", lambda: knowledge.usage_count(RECALL_USAGE, week.start.date().isoformat(),
                                                        week.end.date().isoformat()))
        section("知识库", knowledge.stats)
        if names:
            section("信源", lambda: _silent_sources(knowledge, week, names))

    return WeeklyReview(
        week=week,
        exposures=values.get("exposures"),
        verdicts=values.get("verdicts"),
        trend=values.get("trend", ()),
        top_sources=values.get("top_sources", ()),
        zero_new_sources=values.get("zero_new_sources", ()),
        cut_candidates=values.get("cut_candidates", ()),
        unexposed_personal=values.get("unexposed_personal", ()),
        silent_sources=values.get("信源", ()),
        boards=values.get("boards", ()),
        inbox=values.get("收件箱"),
        watch=values.get("追踪"),
        recall_uses=values.get("recall"),
        profile_lines=values.get("画像", ()),
        knowledge=values.get("知识库"),
        errors=tuple(errors),
    )
