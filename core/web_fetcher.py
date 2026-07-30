import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import aiohttp

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
HEADERS = {"User-Agent": "DiscordDigestBot/1.0 (+content summarizer)"}


class UnsafeUrlError(ValueError):
    pass


async def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrlError("只支持公开的 HTTP/HTTPS URL")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URL 不能包含登录凭证")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".local"):
        raise UnsafeUrlError("不能访问本机或局域网地址")

    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if not literal_ip.is_global:
            raise UnsafeUrlError("不能访问本机、私网或保留地址")
        return

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise UnsafeUrlError("域名无法解析") from error

    if not addresses:
        raise UnsafeUrlError("域名没有可用地址")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise UnsafeUrlError("不能访问本机、私网或保留地址")


async def fetch_public_html(url: str) -> str:
    """Fetch bounded public HTML while validating every redirect target."""
    timeout = aiohttp.ClientTimeout(total=20)
    current_url = url
    async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
        for redirect_count in range(MAX_REDIRECTS + 1):
            await _validate_public_url(current_url)
            async with session.get(current_url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    if redirect_count >= MAX_REDIRECTS:
                        raise RuntimeError("网页重定向次数过多")
                    location = response.headers.get("Location")
                    if not location:
                        raise RuntimeError("网页返回了无目标重定向")
                    current_url = urljoin(current_url, location)
                    continue

                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").lower()
                if not any(kind in content_type for kind in ("text/html", "application/xhtml+xml", "text/plain")):
                    raise RuntimeError("该 URL 不是可总结的文本网页")

                content = await response.content.read(MAX_HTML_BYTES + 1)
                if len(content) > MAX_HTML_BYTES:
                    raise RuntimeError("网页内容超过 2 MB 限制")
                encoding = response.charset or "utf-8"
                return content.decode(encoding, errors="replace")

    raise RuntimeError("网页抓取失败")
