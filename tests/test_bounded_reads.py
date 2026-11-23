import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

import config
from core import bilibili_transcript, feeds, info_curator_client, web_fetcher, web_search
from core.news import personal

RSS = ("<?xml version='1.0'?><rss version='2.0'><channel><title>T</title>"
       + "".join(f"<item><title>Item {i} {'x' * 200}</title><link>https://example.com/{i}</link></item>"
                 for i in range(6))
       + "</channel></rss>").encode()
HTML = ("<html><body>" + "p" * 3000 + "<p>the end</p></body></html>").encode()
JSON_BODY = json.dumps({"code": 0, "data": {"items": ["y" * 100 for _ in range(30)]}}).encode()


class TwoBatchServer(unittest.IsolatedAsyncioTestCase):
    """A local server that writes each body in two batches, as a slow network does."""

    async def asyncSetUp(self):
        async def handler(request):
            body, content_type, status = self.bodies[request.path]
            response = web.StreamResponse(status=status, headers={"Content-Type": content_type})
            await response.prepare(request)
            half = len(body) // 2
            await response.write(body[:half])
            await asyncio.sleep(0.05)
            await response.write(body[half:])
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        self.bodies = {}
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()

    async def asyncTearDown(self):
        await self.server.close()

    def serve(self, path, body, content_type="application/octet-stream", status=200):
        self.bodies[path] = (body, content_type, status)
        return str(self.server.make_url(path))


class ReadCappedTests(TwoBatchServer):
    async def test_reads_to_the_end_across_batches(self):
        url = self.serve("/b", b"a" * 1000 + b"b" * 1000)
        async with aiohttp.ClientSession() as session, session.get(url) as response:
            self.assertEqual(await web_fetcher.read_capped(response, 5000), b"a" * 1000 + b"b" * 1000)

    async def test_stops_one_byte_past_the_limit(self):
        url = self.serve("/b", b"a" * 1000 + b"b" * 1000)
        async with aiohttp.ClientSession() as session, session.get(url) as response:
            self.assertEqual(len(await web_fetcher.read_capped(response, 1500)), 1501)


class CallSiteTests(TwoBatchServer):
    async def test_feed_reads_every_item(self):
        source = feeds.FeedSource("General", self.serve("/rss", RSS, "application/rss+xml"), "T")
        items = await feeds.fetch_feed(source, max_age_seconds=None, max_items=10)
        self.assertEqual(len(items), 6)

    async def test_feed_over_the_limit_still_fails(self):
        source = feeds.FeedSource("General", self.serve("/rss", RSS, "application/rss+xml"), "T")
        with patch.object(feeds, "MAX_FEED_BYTES", len(RSS) - 10), self.assertRaisesRegex(RuntimeError, "超过"):
            await feeds.fetch_feed(source, max_age_seconds=None, max_items=10)

    async def test_public_html_reads_the_whole_page(self):
        url = self.serve("/page", HTML, "text/html; charset=utf-8")
        with patch.object(web_fetcher, "_validate_public_url", AsyncMock()):
            self.assertTrue((await web_fetcher.fetch_public_html(url)).endswith("the end</p></body></html>"))

    async def test_search_response_reads_the_whole_body(self):
        url = self.serve("/search", JSON_BODY, "application/json")
        async with aiohttp.ClientSession() as session, session.get(url) as response:
            self.assertEqual(await web_search._read_limited(response), JSON_BODY)

    async def test_personal_probe_counts_every_item(self):
        entry = {"name": "T", "category": "General", "url": self.serve("/rss", RSS, "application/rss+xml")}
        with patch.object(personal, "_validate_public_url", AsyncMock()):
            self.assertEqual(await personal.probe(entry), 6)

    async def test_bilibili_json_reads_the_whole_body(self):
        url = self.serve("/x", JSON_BODY, "application/json")
        async with aiohttp.ClientSession() as session:
            value = await bilibili_transcript._fetch_json(session, url, headers={}, max_bytes=100_000,
                                                          error_code="subtitle_unavailable")
        self.assertEqual(value, json.loads(JSON_BODY))

    async def test_worker_response_reads_the_whole_body(self):
        error = json.dumps({"ok": False, "error": {"code": "x", "message": "y" * 2000}}).encode()
        endpoint = self.serve("/v1/video-summary", error, "application/json", status=500)
        seen = []
        decode = info_curator_client._decode_json
        with patch.object(config, "INFO_CURATOR_SERVICE_URL", endpoint), \
                patch.object(info_curator_client, "_decode_json", lambda raw: seen.append(raw) or decode(raw)):
            with self.assertRaises(info_curator_client.InfoCuratorError):
                await info_curator_client.fetch_curated_video_summary(
                    "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(seen, [error])


if __name__ == "__main__":
    unittest.main()
