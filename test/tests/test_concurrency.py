import asyncio
import unittest
from types import SimpleNamespace
from urllib.error import URLError
from unittest.mock import AsyncMock, patch

import tgbot
from app.services import locket


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def test_updating_source_preserves_count(self):
        sources = [{"username": "ExampleUser", "count": 4, "expires": "", "stt": 1}]
        with patch.object(tgbot, "load_sources", return_value=sources), patch.object(tgbot, "save_sources") as save:
            count = tgbot.set_current_source("@exampleuser", "2030-01-01 00:00:00")
        self.assertEqual(count, 4)
        self.assertEqual(len(save.call_args.args[0]), 1)
        self.assertEqual(save.call_args.args[0][0]["count"], 4)

    async def test_setsource_checks_status_without_alias(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=message)
        context = SimpleNamespace(args=["exampleuser"])
        for status, expected_save in [
            ({"active": True, "expires": "2099-01-01 00:00:00"}, True),
            ({"active": False, "expires": "Unknown", "error": "HTTP_403"}, False),
        ]:
            with self.subTest(status=status), patch.object(tgbot, "is_admin", return_value=True), patch.object(
                locket, "resolve_uid", new=AsyncMock(return_value="A" * 28)
            ), patch.object(locket, "check_status", new=AsyncMock(return_value=status)), patch.object(
                locket, "aliasSubscriber", new=AsyncMock()
            ) as alias, patch.object(tgbot, "set_current_source", return_value=4) as save:
                await tgbot.cmd_setsource(update, context)
                alias.assert_not_awaited()
                self.assertEqual(save.called, expected_save)

    def test_sources_are_deduplicated_and_sorted_by_expiry(self):
        sources = [
            {
                "stt": 1,
                "username": "ExampleUser",
                "count": 1,
                "expires": "expires: 2028-01-01 00:00:00 (còn 400 ngày)",
            },
            {
                "stt": 2,
                "username": "exampleuser",
                "count": 3,
                "expires": "expires: 2029-01-01 00:00:00 (còn 700 ngày)",
            },
            {
                "stt": 3,
                "username": "LaterUser",
                "count": 0,
                "expires": "expires: 2030-01-01 00:00:00 (còn 1000 ngày)",
            },
        ]

        normalized = tgbot._normalize_sources(sources)

        self.assertEqual([item["username"] for item in normalized], ["LaterUser", "ExampleUser"])
        self.assertEqual(normalized[1]["count"], 3)
        self.assertIn("2029-01-01", normalized[1]["expires"])
        self.assertEqual([item["stt"] for item in normalized], [1, 2])

    async def test_single_check_requests_use_four_slots(self):
        active = 0
        peak = 0

        async def fake_check(update, context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

        with patch.object(tgbot, "cmd_check", new=fake_check):
            await asyncio.gather(
                *(tgbot.limited_cmd_check(None, None) for _ in range(8))
            )

        self.assertEqual(peak, tgbot.MAX_CONCURRENT_CHECKS)

    async def test_bulk_check_requests_are_serialized(self):
        active = 0
        peak = 0

        async def fake_check_file(update, context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

        with patch.object(tgbot, "check_file", new=fake_check_file):
            await asyncio.gather(
                *(tgbot.serialized_check_file(None, None) for _ in range(4))
            )

        self.assertEqual(peak, 1)

    async def test_scan_requests_use_two_slots(self):
        active = 0
        peak = 0

        async def fake_scan(update, context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

        class Message:
            async def reply_text(self, *args, **kwargs):
                return None

        update = SimpleNamespace(message=Message())
        context = SimpleNamespace(args=["https://example.test/video"])

        with patch.object(tgbot, "cmd_scan", new=fake_scan):
            await asyncio.gather(
                *(tgbot.limited_cmd_scan(update, context) for _ in range(5))
            )

        self.assertEqual(peak, tgbot.MAX_CONCURRENT_SCANS)

    async def test_locket_requests_are_bounded(self):
        active = 0
        peak = 0

        async def fake_to_thread(func, *args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return "A" * 28

        with patch.object(locket, "_request_slots", asyncio.Semaphore(2)), patch.object(
            locket.asyncio, "to_thread", new=fake_to_thread
        ):
            results = await asyncio.gather(
                *(locket.resolve_uid(f"user-{index}") for index in range(5))
            )

        self.assertEqual(results, ["A" * 28] * 5)
        self.assertEqual(peak, locket.MAX_CONCURRENT_REQUESTS)

    async def test_proxy_failure_does_not_fall_back_to_direct_connection(self):
        class FailingProxyOpener:
            def open(self, req, timeout):
                raise URLError("proxy unavailable")

        with patch.object(locket, "_request_slots", asyncio.Semaphore(2)), patch.object(
            locket.urllib.request,
            "build_opener",
            return_value=FailingProxyOpener(),
        ), patch.object(
            locket.urllib.request,
            "urlopen",
            side_effect=AssertionError("direct connection must not be used"),
        ):
            result = await locket.resolve_uid(
                "proxy-test-user",
                proxy_url="http://127.0.0.1:9999",
            )

        self.assertEqual(result, "PROXY_ERROR")


if __name__ == "__main__":
    unittest.main()
