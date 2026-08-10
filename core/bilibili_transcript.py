from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import aiohttp

CANONICAL_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
SHORT_HOSTS = {"b23.tv"}
SUBTITLE_HOSTS = {"aisubtitle.hdslb.com"}
BVID_RE = re.compile(r"^BV[A-Za-z0-9]{10,20}$")
MAX_API_BYTES = 1024 * 1024
MAX_SUBTITLE_BYTES = 5 * 1024 * 1024
MAX_DURATION_SECONDS = 90 * 60
MAX_SHORT_REDIRECTS = 3
MAX_SEGMENTS = 20_000
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
}
LANGUAGE_PRIORITY = (
    "zh-CN",
    "zh-Hans",
    "zh-Hant",
    "zh-TW",
    "zh",
    "ai-zh",
    "en",
    "en-US",
)

# Bilibili requests are intentionally serialized even though other link summaries
# may run concurrently.
_BILIBILI_REQUEST_SLOT = asyncio.Semaphore(1)


class BilibiliTranscriptError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class BilibiliTranscript:
    video_id: str
    title: str
    language: str
    source: str
    text: str
    segment_count: int


JsonFetcher = Callable[..., Awaitable[object]]


def _safe_parts(url: str):
    if not isinstance(url, str) or not 1 <= len(url) <= 2048 or any(ord(char) < 32 for char in url):
        raise BilibiliTranscriptError("invalid_url", "B站链接格式无效。")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise BilibiliTranscriptError("invalid_url", "B站链接格式无效。") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise BilibiliTranscriptError("invalid_url", "只支持公开的 HTTPS B站链接。")
    return parsed


def is_bilibili_url(url: str) -> bool:
    try:
        parsed = _safe_parts(url)
    except BilibiliTranscriptError:
        return False
    return parsed.hostname.lower() in CANONICAL_HOSTS | SHORT_HOSTS


def _extract_bvid(url: str) -> str:
    parsed = _safe_parts(url)
    if parsed.hostname.lower() not in CANONICAL_HOSTS:
        raise BilibiliTranscriptError("invalid_url", "B站短链接尚未解析为视频地址。")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != "video" or not BVID_RE.fullmatch(parts[1]):
        raise BilibiliTranscriptError("invalid_url", "链接中没有有效的 BV 视频编号。")
    return parts[1]


def _extract_page_number(url: str) -> int:
    values = parse_qs(_safe_parts(url).query).get("p", ["1"])
    try:
        page_number = int(values[0])
    except (TypeError, ValueError) as error:
        raise BilibiliTranscriptError("invalid_url", "B站视频分P编号无效。") from error
    if not 1 <= page_number <= 1000:
        raise BilibiliTranscriptError("invalid_url", "B站视频分P编号无效。")
    return page_number


def _api_data(value: object, *, error_code: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BilibiliTranscriptError(error_code, "B站 API 返回了无效数据。")
    api_code = value.get("code")
    if api_code in {-101, -400, -403}:
        raise BilibiliTranscriptError("authentication_required", "B站字幕需要登录凭据。")
    data = value.get("data")
    if api_code != 0 or not isinstance(data, dict):
        raise BilibiliTranscriptError(error_code, "B站 API 没有返回可用数据。")
    return data


def _normalize_segments(value: object) -> list[dict[str, object]]:
    if not isinstance(value, dict) or not isinstance(value.get("body"), list):
        raise BilibiliTranscriptError("subtitle_invalid", "B站字幕格式无效。")

    raw_segments: list[tuple[int, int, str]] = []
    try:
        for cue in value["body"]:
            if not isinstance(cue, dict):
                continue
            start = int(float(cue.get("from", 0)) * 1000)
            end = int(float(cue.get("to", 0)) * 1000)
            text = str(cue.get("content", ""))
            raw_segments.append((start, end, text))
    except (TypeError, ValueError) as error:
        raise BilibiliTranscriptError("subtitle_invalid", "B站字幕时间信息无效。") from error

    segments: list[dict[str, object]] = []
    previous_end = 0
    for start, end, text in sorted(raw_segments, key=lambda item: (item[0], item[1])):
        normalized_text = " ".join(text.replace("\x00", "").split()).strip()
        normalized_start = max(0, start, previous_end)
        normalized_end = max(0, end)
        if not normalized_text or normalized_end <= normalized_start:
            continue
        segments.append(
            {
                "start_ms": normalized_start,
                "end_ms": normalized_end,
                "text": normalized_text[:4000],
            }
        )
        previous_end = normalized_end
        if len(segments) >= MAX_SEGMENTS:
            break

    if not segments:
        raise BilibiliTranscriptError("subtitle_invalid", "B站字幕中没有可用内容。")
    return segments


async def _fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str],
    max_bytes: int,
    error_code: str,
) -> object:
    try:
        async with session.get(url, headers=headers, allow_redirects=False) as response:
            if response.status in {401, 403}:
                raise BilibiliTranscriptError("authentication_required", "B站字幕需要登录凭据。")
            if response.status != 200:
                raise BilibiliTranscriptError(error_code, "B站字幕请求失败。")
            raw = await response.content.read(max_bytes + 1)
    except BilibiliTranscriptError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise BilibiliTranscriptError(error_code, "连接 B站字幕服务失败。") from error

    if len(raw) > max_bytes:
        raise BilibiliTranscriptError(error_code, "B站字幕响应超过大小限制。")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BilibiliTranscriptError(error_code, "B站字幕响应不是有效 JSON。") from error


async def _resolve_short_url(session: aiohttp.ClientSession, url: str) -> str:
    current_url = url
    for redirect_count in range(MAX_SHORT_REDIRECTS + 1):
        parsed = _safe_parts(current_url)
        host = parsed.hostname.lower()
        if host in CANONICAL_HOSTS:
            _extract_bvid(current_url)
            return current_url
        if host not in SHORT_HOSTS or redirect_count >= MAX_SHORT_REDIRECTS:
            raise BilibiliTranscriptError("redirect_not_allowed", "B站短链接跳转到了不允许的地址。")

        try:
            async with session.get(current_url, headers=HEADERS, allow_redirects=False) as response:
                if response.status not in {301, 302, 303, 307, 308}:
                    raise BilibiliTranscriptError("invalid_url", "B站短链接没有返回视频地址。")
                location = response.headers.get("Location")
        except BilibiliTranscriptError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise BilibiliTranscriptError("metadata_unavailable", "解析 B站短链接失败。") from error

        if not location:
            raise BilibiliTranscriptError("invalid_url", "B站短链接缺少跳转地址。")
        current_url = urljoin(current_url, location)

    raise BilibiliTranscriptError("redirect_not_allowed", "B站短链接跳转次数过多。")


async def _fetch_transcript(
    session: aiohttp.ClientSession,
    url: str,
    *,
    cookie: str | None,
    fetch_json: JsonFetcher = _fetch_json,
) -> BilibiliTranscript:
    parsed = _safe_parts(url)
    if parsed.hostname.lower() in SHORT_HOSTS:
        url = await _resolve_short_url(session, url)
    bvid = _extract_bvid(url)
    page_number = _extract_page_number(url)

    api_headers = {**HEADERS, "Referer": "https://www.bilibili.com/"}
    if cookie:
        if len(cookie) > 16 * 1024 or "\r" in cookie or "\n" in cookie:
            raise BilibiliTranscriptError("authentication_required", "B站登录凭据格式无效。")
        api_headers["Cookie"] = cookie

    view_url = "https://api.bilibili.com/x/web-interface/view?" + urlencode({"bvid": bvid})
    view = _api_data(
        await fetch_json(
            session,
            view_url,
            headers=api_headers,
            max_bytes=MAX_API_BYTES,
            error_code="metadata_unavailable",
        ),
        error_code="metadata_unavailable",
    )

    aid = view.get("aid")
    pages = view.get("pages")
    if isinstance(aid, bool) or not isinstance(aid, int) or not isinstance(pages, list) or not pages:
        raise BilibiliTranscriptError("metadata_invalid", "B站视频元数据无效。")
    page = next(
        (item for item in pages if isinstance(item, dict) and item.get("page") == page_number),
        None,
    )
    if not page:
        raise BilibiliTranscriptError("metadata_invalid", "指定的 B站视频分P不可用。")
    cid = page.get("cid")
    duration = page.get("duration")
    if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0:
        raise BilibiliTranscriptError("metadata_invalid", "B站视频 CID 无效。")
    if isinstance(duration, (int, float)) and duration > MAX_DURATION_SECONDS:
        raise BilibiliTranscriptError("duration_limit", "B站视频超过 90 分钟限制。")

    player_url = "https://api.bilibili.com/x/player/v2?" + urlencode({"aid": aid, "cid": cid})
    player = _api_data(
        await fetch_json(
            session,
            player_url,
            headers=api_headers,
            max_bytes=MAX_API_BYTES,
            error_code="subtitle_not_found",
        ),
        error_code="subtitle_not_found",
    )
    subtitle = player.get("subtitle")
    tracks = subtitle.get("subtitles") if isinstance(subtitle, dict) else None
    if not isinstance(tracks, list) or not tracks:
        raise BilibiliTranscriptError("subtitle_not_found", "该 B站视频没有可用字幕。")

    selected = None
    for language in LANGUAGE_PRIORITY:
        selected = next(
            (track for track in tracks if isinstance(track, dict) and track.get("lan") == language),
            None,
        )
        if selected:
            break
    if not selected:
        raise BilibiliTranscriptError("subtitle_not_found", "该 B站视频没有中文或英文字幕。")

    subtitle_url = selected.get("subtitle_url")
    if not isinstance(subtitle_url, str):
        raise BilibiliTranscriptError("subtitle_invalid", "B站字幕地址无效。")
    if subtitle_url.startswith("//"):
        subtitle_url = "https:" + subtitle_url
    subtitle_parts = _safe_parts(subtitle_url)
    if subtitle_parts.hostname.lower() not in SUBTITLE_HOSTS:
        raise BilibiliTranscriptError("redirect_not_allowed", "B站字幕地址不在允许的域名中。")

    payload = await fetch_json(
        session,
        subtitle_url,
        headers=HEADERS,
        max_bytes=MAX_SUBTITLE_BYTES,
        error_code="subtitle_fetch_failed",
    )
    segments = _normalize_segments(payload)
    language = str(selected.get("lan") or "unknown")
    source = "自动字幕" if language.startswith("ai-") else "创作者字幕"
    return BilibiliTranscript(
        video_id=bvid,
        title=str(page.get("part") or view.get("title") or "")[:500],
        language=language,
        source=source,
        text="\n".join(str(segment["text"]) for segment in segments),
        segment_count=len(segments),
    )


async def fetch_bilibili_transcript(url: str, *, cookie: str | None = None) -> BilibiliTranscript:
    timeout = aiohttp.ClientTimeout(total=20)
    async with _BILIBILI_REQUEST_SLOT:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            return await _fetch_transcript(session, url, cookie=cookie)
