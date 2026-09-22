import unittest
from unittest.mock import AsyncMock, patch

import scan_locket
import tgbot
from app.services import locket


class DynamicLinkSupportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        locket._uid_cache.clear()
        locket._uid_pending.clear()

    def test_extract_username_various_formats(self):
        # 1. Dynamic links
        self.assertEqual(
            tgbot.extract_username("https://locket.camera/links/oTuuThx5GxDxunHRA"),
            "https://locket.camera/links/oTuuThx5GxDxunHRA",
        )
        self.assertEqual(
            tgbot.extract_username("locket.camera/links/oTuuThx5GxDxunHRA?utm_source=threads"),
            "https://locket.camera/links/oTuuThx5GxDxunHRA",
        )
        self.assertEqual(
            tgbot.extract_username("https://locket.cam/links/oTuuThx5GxDxunHRA"),
            "https://locket.camera/links/oTuuThx5GxDxunHRA",
        )

        # 2. Standard username and invite links
        self.assertEqual(tgbot.extract_username("https://locket.cam/toiii"), "toiii")
        self.assertEqual(tgbot.extract_username("https://locket.camera/invites/pdlinhh"), "pdlinhh")
        self.assertEqual(tgbot.extract_username("https://locket.cam/invites/pdlinhh"), "pdlinhh")
        self.assertEqual(tgbot.extract_username("@toiii"), "toiii")
        self.assertEqual(tgbot.extract_username("toiii"), "toiii")

    def test_scan_locket_extracts_both_user_and_dynamic_links(self):
        text = (
            "bài viết trên Threads: hãy tham gia https://locket.camera/links/oTuuThx5GxDxunHRA "
            "hoặc kết bạn nick https://locket.cam/toiii và https://locket.camera/invites/pdlinhh"
        )
        links = set()
        count = scan_locket.extract_locket_links_from_text(text, "Threads Test", links)
        self.assertEqual(count, 3)
        self.assertIn("https://locket.camera/links/oTuuThx5GxDxunHRA", links)
        self.assertIn("https://locket.cam/toiii", links)
        self.assertIn("https://locket.cam/pdlinhh", links)

    def test_scan_locket_normalizes_dynamic_links(self):
        self.assertEqual(
            scan_locket.normalize_dynamic_link("oTuuThx5GxDxunHRA"),
            "https://locket.camera/links/oTuuThx5GxDxunHRA",
        )
        # Invalid / short / ignored
        self.assertIsNone(scan_locket.normalize_dynamic_link("abc"))
        self.assertIsNone(scan_locket.normalize_dynamic_link("comment"))

    async def test_resolve_uid_dynamic_link(self):
        fake_uid = "U" * 28
        with patch.object(locket, "_resolve_uid", new=AsyncMock(return_value=fake_uid)) as mock_resolve:
            res = await locket.resolve_uid("https://locket.camera/links/oTuuThx5GxDxunHRA")
            self.assertEqual(res, fake_uid)
            mock_resolve.assert_awaited_once_with("links/oTuuThx5GxDxunHRA", None)

            # Check cache hit
            res2 = await locket.resolve_uid("https://locket.camera/links/oTuuThx5GxDxunHRA")
            self.assertEqual(res2, fake_uid)
            self.assertEqual(mock_resolve.await_count, 1)

    async def test_release_source_exhausted_marks_five(self):
        sources = [{"username": "testsource", "count": 1, "expires": "2030-01-01 00:00:00", "stt": 1}]
        with patch.object(tgbot, "load_sources", return_value=sources), patch.object(tgbot, "save_sources") as mock_save:
            count = await tgbot.release_source("testsource", success=False, exhausted=True)
            self.assertEqual(count, 5)
            self.assertEqual(sources[0]["count"], 5)
            mock_save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
