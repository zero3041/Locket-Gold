import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError
from urllib.request import Request

from app.services import locket


class RequestControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        locket._uid_cache.clear()
        locket._uid_pending.clear()

    async def test_duplicate_requests_and_later_cache_hit(self):
        async def lookup(*args):
            await asyncio.sleep(0.01)
            return "A" * 28
        with patch.object(locket, "_resolve_uid", new=AsyncMock(side_effect=lookup)) as fetch:
            results = await asyncio.gather(*(locket.resolve_uid("same") for _ in range(12)))
            self.assertEqual(results, ["A" * 28] * 12)
            self.assertEqual(await locket.resolve_uid("same"), "A" * 28)
            self.assertEqual(fetch.await_count, 1)

    async def test_failures_and_expired_entries_are_not_reused(self):
        with patch.object(locket, "_resolve_uid", new=AsyncMock(side_effect=[None, "A" * 28, "B" * 28])) as fetch:
            self.assertIsNone(await locket.resolve_uid("retry"))
            self.assertEqual(await locket.resolve_uid("retry"), "A" * 28)
            locket._uid_cache[("retry", None)] = ("A" * 28, 0)
            self.assertEqual(await locket.resolve_uid("retry"), "B" * 28)
            self.assertEqual(fetch.await_count, 3)

    def test_refusal_blocks_queued_requests_and_respects_retry_after(self):
        for code in (403, 429):
            req = Request("https://example.test/")
            error = HTTPError(req.full_url, code, "refused", {"Retry-After": "120"}, None)
            with patch.object(locket, "_next_request_at", 0), patch.object(locket, "_blocked_until", 0), patch.object(
                locket.time, "monotonic", return_value=100
            ), patch.object(locket, "_transport_open", side_effect=error) as transport:
                with self.assertRaises(HTTPError):
                    locket._open_request(req, 1)
                self.assertEqual(locket._blocked_until, 220)
                with self.assertRaises(HTTPError):
                    locket._open_request(req, 1)
                self.assertEqual(transport.call_count, 1)

    def test_requests_wait_for_interval(self):
        req = Request("https://example.test/")
        with patch.object(locket, "_next_request_at", 102), patch.object(locket, "_blocked_until", 0), patch.object(
            locket.time, "monotonic", return_value=100
        ), patch.object(locket.time, "sleep") as sleep, patch.object(locket, "_transport_open"):
            locket._open_request(req, 1)
            sleep.assert_called_once_with(2)
