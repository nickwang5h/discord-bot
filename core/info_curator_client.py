from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import aiohttp

import config


BILIBILI_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}
CANONICAL_BILIBILI_HOSTS = {"bilibili.com", "www.bilibili.com"}
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
CANONICAL_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com"}
ALL_VIDEO_HOSTS = BILIBILI_HOSTS | YOUTUBE_HOSTS
SERVICE_HOSTS = {"video-summary", "127.0.0.1", "localhost", "::1"}
BVID_RE = re.compile(r"^BV[A-Za-z0-9]{10,20}$")
YOUTUBE_VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
MAX_URL_CHARS = 2048
MAX_WORKER_RESPONSE_BYTES = 128 * 1024
MAX_MARKDOWN_CHARS = 32_000


class InfoCuratorError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CuratedVideoSummary:
    markdown: str
    provider: str
    model: str
    profile: str
    transcript_source: str
    language: str
    reused: bool
    media_reused: bool


def _safe_url_parts(url: str):
    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= MAX_URL_CHARS
        or any(ord(character) < 32 for character in url)
    ):
        raise InfoCuratorError("invalid_media_url", "视频链接格式无效。")
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as error:
        raise InfoCuratorError("invalid_media_url", "视频链接格式无效。") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise InfoCuratorError("unsupported_media_url", "只支持完整的 HTTPS B站或 YouTube 链接。")
    return parsed


def is_bilibili_url(url: str) -> bool:
    try:
        parsed = _safe_url_parts(url)
    except InfoCuratorError:
        return False
    return parsed.hostname.lower() in BILIBILI_HOSTS


def is_youtube_url(url: str) -> bool:
    try:
        parsed = _safe_url_parts(url)
    except InfoCuratorError:
        return False
    return parsed.hostname.lower() in YOUTUBE_HOSTS


def is_supported_video_url(url: str) -> bool:
    try:
        parsed = _safe_url_parts(url)
    except InfoCuratorError:
        return False
    return parsed.hostname.lower() in ALL_VIDEO_HOSTS


def canonicalize_video_url(url: str) -> str:
    parsed = _safe_url_parts(url)
    hostname = parsed.hostname.lower()
    if hostname in CANONICAL_BILIBILI_HOSTS:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0] != "video" or not BVID_RE.fullmatch(parts[1]):
            raise InfoCuratorError(
                "unsupported_media_url",
                "请使用完整的 www.bilibili.com/video/BV... 链接。",
            )
        page_values = parse_qs(parsed.query, keep_blank_values=True).get("p", [])
        if any(value != "1" for value in page_values):
            raise InfoCuratorError(
                "unsupported_media_url",
                "带分P的 B站链接暂不支持，请提交视频第一P的完整 BV 链接。",
            )
        return f"https://www.bilibili.com/video/{parts[1]}"

    if hostname in YOUTUBE_HOSTS:
        video_id: str | None = None
        if hostname == "youtu.be":
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 1 and YOUTUBE_VIDEO_ID_RE.fullmatch(parts[0]):
                video_id = parts[0]
        elif hostname in {"www.youtube.com", "youtube.com", "m.youtube.com"}:
            if parsed.path == "/watch":
                qs = parse_qs(parsed.query, keep_blank_values=True)
                v_list = qs.get("v", [])
                if len(v_list) == 1 and YOUTUBE_VIDEO_ID_RE.fullmatch(v_list[0]):
                    video_id = v_list[0]
            elif parsed.path.startswith(("/embed/", "/shorts/", "/v/", "/live/")):
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) >= 2 and YOUTUBE_VIDEO_ID_RE.fullmatch(parts[1]):
                    video_id = parts[1]
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        raise InfoCuratorError(
            "unsupported_media_url",
            "请使用完整的 YouTube 视频链接（如 youtube.com/watch?v=... 或 youtu.be/...）。",
        )

    raise InfoCuratorError(
        "unsupported_media_url",
        "只支持完整的 B站 BV 链接或 YouTube 视频链接。",
    )


def _service_endpoint(value: str) -> str:
    if not value:
        raise InfoCuratorError(
            "worker_unavailable", "视频总结服务尚未在此部署环境中配置。"
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise InfoCuratorError("worker_config_invalid", "视频总结服务配置无效。") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in SERVICE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/v1/video-summary"
        or parsed.query
        or parsed.fragment
        or port is None
        or not 1 <= port <= 65535
    ):
        raise InfoCuratorError("worker_config_invalid", "视频总结服务配置无效。")
    return value


def _decode_json(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InfoCuratorError("worker_contract_mismatch", "视频总结服务返回无效数据。") from error


def _validated_result(value: object) -> CuratedVideoSummary:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "status",
        "markdown",
        "provider",
        "model",
        "profile",
        "transcript_source",
        "language",
        "reused",
        "media_reused",
    }:
        raise InfoCuratorError("worker_contract_mismatch", "视频总结服务返回无效数据。")
    markdown = value.get("markdown")
    provider = value.get("provider")
    model = value.get("model")
    profile = value.get("profile")
    transcript_source = value.get("transcript_source")
    language = value.get("language")
    if (
        value.get("schema_version") != "discord_video_summary_worker_v1"
        or value.get("status") != "complete"
        or not isinstance(markdown, str)
        or not 1 <= len(markdown) <= MAX_MARKDOWN_CHARS
        or any(ord(character) < 32 and character not in "\n\r\t" for character in markdown)
        or not isinstance(provider, str)
        or not 1 <= len(provider) <= 80
        or any(ord(character) < 32 for character in provider)
        or not isinstance(model, str)
        or not 1 <= len(model) <= 160
        or any(ord(character) < 32 for character in model)
        or not isinstance(profile, str)
        or profile not in {"summary", "brief"}
        or not isinstance(transcript_source, str)
        or transcript_source
        not in {"creator_subtitle", "automatic_subtitle", "local_asr"}
        or not isinstance(language, str)
        or not 1 <= len(language) <= 35
        or any(ord(character) < 32 for character in language)
        or not isinstance(value.get("reused"), bool)
        or not isinstance(value.get("media_reused"), bool)
    ):
        raise InfoCuratorError("worker_contract_mismatch", "视频总结服务返回无效数据。")
    return CuratedVideoSummary(
        markdown=markdown,
        provider=provider,
        model=model,
        profile=profile,
        transcript_source=transcript_source,
        language=language,
        reused=value["reused"],
        media_reused=value["media_reused"],
    )


def _worker_error(value: object) -> InfoCuratorError:
    if not isinstance(value, dict):
        return InfoCuratorError("worker_unavailable", "视频总结服务暂时不可用。")
    code = value.get("error_code")
    if (
        value.get("schema_version") != "discord_video_summary_worker_error_v1"
        or not isinstance(code, str)
        or ERROR_CODE_RE.fullmatch(code) is None
    ):
        return InfoCuratorError("worker_contract_mismatch", "视频总结服务返回无效数据。")
    messages = {
        "unsupported_media_url": "请使用完整的 B站 BV 视频链接或 YouTube 视频链接。",
        "media_not_available": "无法取得该视频的可用字幕（视频可能未提供字幕或受风控限制）。",
        "media_contract_mismatch": "字幕验证失败，未生成总结。",
        "transcript_invalid": "字幕验证失败，未生成总结。",
        "summary_attempt_exhausted": "该视频的当前总结身份此前已失败，需要管理员处理。",
        "provider_unavailable": "视频总结模型目前不可用。",
        "provider_timeout": "视频总结模型请求超时。",
        "provider_invalid_json": "视频总结模型没有返回完整的可验证结果。",
        "summary_invalid": "视频总结未通过结构验证。",
        "summary_invalid_citation": "视频总结中的时间引用未通过验证。",
        "worker_busy": "视频总结服务正在处理另一个视频，请稍后再试。",
        "worker_timeout": "视频总结处理超时。",
        "worker_config_invalid": "视频总结服务配置无效。",
    }
    return InfoCuratorError(code, messages.get(code, "视频总结服务暂时不可用。"))


async def _fetch_curated_video(
    url: str, *, profile: str
) -> CuratedVideoSummary:
    if profile not in {"summary", "brief"}:
        raise InfoCuratorError("worker_config_invalid", "视频总结服务配置无效。")
    canonical_url = canonicalize_video_url(url)
    endpoint = _service_endpoint(config.INFO_CURATOR_SERVICE_URL)
    timeout = aiohttp.ClientTimeout(total=config.INFO_CURATOR_REQUEST_TIMEOUT_SECONDS)
    payload = {"url": canonical_url}
    if profile == "brief":
        payload["profile"] = "brief"
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                endpoint,
                json=payload,
                allow_redirects=False,
            ) as response:
                raw = await response.content.read(MAX_WORKER_RESPONSE_BYTES + 1)
                status = response.status
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise InfoCuratorError("worker_unavailable", "视频总结服务暂时不可用。") from error
    if len(raw) > MAX_WORKER_RESPONSE_BYTES:
        raise InfoCuratorError("worker_contract_mismatch", "视频总结服务响应过大。")
    value = _decode_json(raw)
    if status != 200:
        raise _worker_error(value)
    result = _validated_result(value)
    if result.profile != profile:
        raise InfoCuratorError("worker_contract_mismatch", "视频总结服务返回无效数据。")
    return result


async def fetch_curated_video_summary(url: str) -> CuratedVideoSummary:
    return await _fetch_curated_video(url, profile="summary")


async def fetch_curated_video_brief(url: str) -> CuratedVideoSummary:
    return await _fetch_curated_video(url, profile="brief")
