import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_store
from app import database as db

SECRET = "web-store-session-secret-with-at-least-32-characters"


class WebStoreAppTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.sqlite3")
        self.original_db_name = db.DB_NAME
        db.DB_NAME = self.db_path
        db.init_db()

        web_store.CDK_UNIT_PRICE = 50000
        web_store.CDK_UNIT_PRICE_1Y = 200000
        web_store.CDK_SECRET = SECRET
        web_store.BANK_BIN = "970418"
        web_store.BANK_ACCOUNT = "1234567890"
        web_store.BANK_NAME = "TPBank"
        web_store.BANK_OWNER = "NGUYEN VAN A"
        web_store.SEPAY_API_TOKEN = "test-token"
        web_store.WEB_ADMIN_USER = "admin"
        web_store.WEB_ADMIN_PASSWORD = "s3cret-pass"
        web_store.WEB_ADMIN_PASSWORD_HASH = ""
        web_store.WEB_SESSION_SECRET = SECRET
        web_store.payment_config_errors = lambda: []

        async def _noop_complete(order_id):
            return None

        web_store._complete_web_order = _noop_complete

        self.runner = web.AppRunner(web_store.build_app())
        self.loop = asyncio.get_event_loop()

    async def asyncSetUp(self):
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"

    async def asyncTearDown(self):
        await self.runner.cleanup()
        db.DB_NAME = self.original_db_name
        self.tmp.cleanup()

    async def test_landing_page_renders(self):
        from aiohttp import ClientSession
        async with ClientSession() as session:
            async with session.get(f"{self.base}/") as resp:
                body = await resp.text()
        self.assertEqual(200, resp.status)
        self.assertIn("Locket Gold", body)
        self.assertIn("Kích hoạt Gold", body)
        self.assertNotIn("dns", body.lower())

    async def test_verify_page_and_api(self):
        from aiohttp import ClientSession
        codes = db.gen_cdk(1, admin_id=0, cdk_secret=SECRET, plan="1y", spins=3)
        async with ClientSession() as session:
            async with session.post(f"{self.base}/api/verify", json={"code": codes[0]}) as resp:
                data = await resp.json()
        self.assertEqual("valid", data["status"])
        self.assertEqual("1y", data["plan"])
        self.assertEqual(3, data["spins_left"])

        db.consume_key(codes[0], user_id=99, secret=SECRET)
        async with ClientSession() as session:
            async with session.post(f"{self.base}/api/verify", json={"code": codes[0]}) as resp:
                data = await resp.json()
        self.assertEqual("valid", data["status"])
        self.assertEqual(2, data["spins_left"])

        for _ in range(2):
            db.consume_key(codes[0], user_id=99, secret=SECRET)
        async with ClientSession() as session:
            async with session.post(f"{self.base}/api/verify", json={"code": codes[0]}) as resp:
                data = await resp.json()
        self.assertEqual("used", data["status"])
        self.assertEqual(99, data["used_by"])

    async def _admin_login(self, session, password="s3cret-pass"):
        """GET /admin/login to obtain the CSRF cookie + token, then POST credentials."""
        async with session.get(f"{self.base}/admin/login") as resp:
            body = await resp.text()
        import re
        m = re.search(r'name="csrf" value="([0-9a-f]+)"', body)
        self.assertIsNotNone(m, "login page must embed a CSRF token")
        async with session.post(
            f"{self.base}/admin/login",
            data={"username": "admin", "password": password, "csrf": m.group(1)},
            allow_redirects=False,
        ) as resp:
            self.assertEqual(302, resp.status)
        return m.group(1)

    async def test_admin_requires_login_and_grants_session(self):
        from aiohttp import ClientSession, CookieJar
        jar = CookieJar(unsafe=True)
        async with ClientSession(cookie_jar=jar) as session:
            async with session.get(f"{self.base}/admin") as resp:
                self.assertEqual(200, resp.status)
                self.assertIn("/admin/login", str(resp.url))

            async with session.get(f"{self.base}/admin/login") as resp:
                body = await resp.text()
            import re
            m = re.search(r'name="csrf" value="([0-9a-f]+)"', body)
            self.assertIsNotNone(m)

            async with session.post(
                f"{self.base}/admin/login",
                data={"username": "admin", "password": "wrong", "csrf": m.group(1)},
            ) as resp:
                body = await resp.text()
                self.assertIn("Sai tên đăng nhập", body)

            async with session.post(
                f"{self.base}/admin/login",
                data={"username": "admin", "password": "s3cret-pass"},
                allow_redirects=False,
            ) as resp:
                body = await resp.text()
                self.assertIn("Phiên đăng nhập không hợp lệ", body)

            await self._admin_login(session)

            async with session.get(f"{self.base}/admin") as resp:
                body = await resp.text()
            self.assertEqual(200, resp.status)
            self.assertIn("Tổng quan", body)

            async with session.get(f"{self.base}/admin/logout", allow_redirects=False) as resp:
                self.assertEqual(302, resp.status)

            async with session.get(f"{self.base}/admin", allow_redirects=False) as resp:
                self.assertEqual(302, resp.status)
                self.assertIn("/admin/login", resp.headers.get("Location", ""))

    async def test_admin_generate_keys_requires_csrf(self):
        from aiohttp import ClientSession, CookieJar
        jar = CookieJar(unsafe=True)
        async with ClientSession(cookie_jar=jar) as session:
            token = await self._admin_login(session)

            async with session.post(f"{self.base}/admin/keys/generate", json={"count": 5}) as resp:
                self.assertEqual(403, resp.status)

            async with session.post(
                f"{self.base}/admin/keys/generate",
                json={"count": 5, "spins": 2, "plan": "1y"},
                headers={"X-CSRF-Token": token},
            ) as resp:
                data = await resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(5, len(data["codes"]))
            for code in data["codes"]:
                self.assertTrue(db.validate_cdk(code, secret=SECRET))
            detail = db.get_cdk_detail(data["codes"][0], secret=SECRET)
            self.assertEqual("1y", detail["plan"])
            self.assertEqual(2, detail["spins_left"])

    async def test_admin_sources_add_remove_and_cleanup(self):
        from aiohttp import ClientSession, CookieJar
        jar = CookieJar(unsafe=True)
        async with ClientSession(cookie_jar=jar) as session:
            token = await self._admin_login(session)

            async def fake_check_source(source, probe=False, proxy_url=None):
                return {
                    "username": source["username"], "status": "usable",
                    "uid": "U" * 28, "days_left": 200,
                    "expires": "2030-01-01 00:00:00",
                }

            with patch.object(web_store.activation, "check_source", new=AsyncMock(side_effect=fake_check_source)):
                async with session.post(
                    f"{self.base}/admin/sources/add",
                    json={"username": "https://locket.cam/goldsrc"},
                    headers={"X-CSRF-Token": token},
                ) as resp:
                    data = await resp.json()
            self.assertTrue(data["ok"], data)
            self.assertEqual("goldsrc", data["username"])
            self.assertIn("goldsrc", [s["username"] for s in db.list_gold_sources()])

            # CSRF is required.
            async with session.post(
                f"{self.base}/admin/sources/remove", json={"username": "goldsrc"}
            ) as resp:
                self.assertEqual(403, resp.status)

            async with session.post(
                f"{self.base}/admin/sources/remove",
                json={"username": "goldsrc"},
                headers={"X-CSRF-Token": token},
            ) as resp:
                data = await resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual([], db.list_gold_sources())

    async def test_admin_password_hash_verification(self):
        import web_store
        import base64
        import hashlib

        salt = b"fixed-salt-123456"
        digest = hashlib.pbkdf2_hmac("sha256", b"hunter2", salt, 1000)
        web_store.WEB_ADMIN_PASSWORD = ""
        web_store.WEB_ADMIN_PASSWORD_HASH = (
            "pbkdf2$1000$"
            + base64.urlsafe_b64encode(salt).decode().rstrip("=")
            + "$"
            + base64.urlsafe_b64encode(digest).decode().rstrip("=")
        )
        self.assertTrue(web_store._verify_admin_password("hunter2"))
        self.assertFalse(web_store._verify_admin_password("wrong"))
        self.assertFalse(web_store._verify_admin_password(""))
        web_store.WEB_ADMIN_PASSWORD_HASH = "pbkdf2$not-an-int$xx$yy"
        self.assertFalse(web_store._verify_admin_password("hunter2"))
        web_store.WEB_ADMIN_PASSWORD_HASH = ""
        web_store.WEB_ADMIN_PASSWORD = "legacy-pass"
        self.assertTrue(web_store._verify_admin_password("legacy-pass"))
        self.assertFalse(web_store._verify_admin_password("nope"))

    async def test_admin_link_hidden_from_public_nav(self):
        from aiohttp import ClientSession
        async with ClientSession() as session:
            async with session.get(f"{self.base}/") as resp:
                body = await resp.text()
            async with session.get(f"{self.base}/verify") as resp:
                verify_body = await resp.text()
        self.assertNotIn("/admin/login", body)
        self.assertNotIn("/admin/login", verify_body)
        self.assertNotIn("🔐 Admin", body)

    async def test_api_check_resolves_profile_and_status(self):
        from aiohttp import ClientSession
        import web_store

        async def fake_resolve(username, proxy_url=None):
            return {"uid": "UID-XYZ", "avatar": "//cdn.example/av.jpg"}

        async def fake_status(uid, proxy_url=None):
            return {"active": False}

        with (
            patch.object(web_store.locket, "resolve_profile", new=AsyncMock(side_effect=fake_resolve)),
            patch.object(web_store.locket, "check_status", new=AsyncMock(side_effect=fake_status)),
        ):
            async with ClientSession() as session:
                async with session.post(f"{self.base}/api/check", json={"username": "alice"}) as resp:
                    data = await resp.json()
                self.assertTrue(data["ok"])
                self.assertEqual("UID-XYZ", data["uid"])
                self.assertEqual("alice", data["username"])
                self.assertEqual("https://cdn.example/av.jpg", data["avatar"])
                self.assertFalse(data["gold_active"])

                async with session.post(
                    f"{self.base}/api/check", json={"username": "https://locket.cam/bob"}
                ) as resp:
                    data = await resp.json()
                self.assertEqual("bob", data["username"])

    async def test_api_check_not_found(self):
        from aiohttp import ClientSession
        import web_store

        with patch.object(web_store.locket, "resolve_profile", new=AsyncMock(return_value=None)):
            async with ClientSession() as session:
                async with session.post(f"{self.base}/api/check", json={"username": "ghost"}) as resp:
                    data = await resp.json()
        self.assertEqual(404, resp.status)
        self.assertFalse(data["ok"])

    async def test_api_create_order_uses_plan_price(self):
        from aiohttp import ClientSession
        async with ClientSession() as session:
            async with session.post(f"{self.base}/api/order", json={"plan": "1y", "quantity": 2}) as resp:
                data = await resp.json()
        self.assertTrue(data["ok"], data)
        order = db.get_cdk_order(id=data["order_id"])
        self.assertEqual("1y", order["plan"])
        self.assertEqual(2 * web_store.CDK_UNIT_PRICE_1Y, order["total_price"])

        async with ClientSession() as session:
            async with session.post(f"{self.base}/api/order", json={"plan": "bad"}) as resp:
                self.assertEqual(400, resp.status)
            async with session.post(f"{self.base}/api/order", json={"plan": "1m", "quantity": 9}) as resp:
                self.assertEqual(400, resp.status)

    async def test_landing_shows_single_permanent_product(self):
        from aiohttp import ClientSession
        async with ClientSession() as session:
            async with session.get(f"{self.base}/") as resp:
                body = await resp.text()
        self.assertIn("Gói Vĩnh Viễn", body)
        self.assertIn("Kích hoạt lại miễn phí", body)
        self.assertIn("rớt", body.lower())
        self.assertNotIn("Gói 1 Năm", body)

    async def test_api_check_reports_reactivation_eligibility(self):
        from aiohttp import ClientSession
        import web_store

        uid = "R" * 28

        async def fake_resolve(username, proxy_url=None):
            return {"uid": uid, "avatar": None}

        async def fake_status(check_uid, proxy_url=None):
            return {"active": False, "expires": "Unknown"}

        with (
            patch.object(web_store.locket, "resolve_profile", new=AsyncMock(side_effect=fake_resolve)),
            patch.object(web_store.locket, "check_status", new=AsyncMock(side_effect=fake_status)),
        ):
            async with ClientSession() as session:
                async with session.post(f"{self.base}/api/check", json={"username": "veteran"}) as resp:
                    data = await resp.json()
                self.assertFalse(data["can_reactivate"])

                db.mark_uid_activated(uid, when=0)
                async with session.post(f"{self.base}/api/check", json={"username": "veteran"}) as resp:
                    data = await resp.json()
                self.assertTrue(data["can_reactivate"])

    async def test_api_reactivate_requires_previous_activation(self):
        from aiohttp import ClientSession
        import web_store
        with patch.object(web_store.locket, "resolve_uid", new=AsyncMock(return_value="X" * 28)):
            async with ClientSession() as session:
                async with session.post(f"{self.base}/api/reactivate", json={"username": "nobody"}) as resp:
                    data = await resp.json()
        self.assertEqual(403, resp.status)
        self.assertFalse(data["ok"])

    async def test_api_reactivate_success_then_cooldown(self):
        from aiohttp import ClientSession
        import web_store

        uid = "Y" * 28
        db.mark_uid_activated(uid, when=0)
        result = {
            "ok": True, "code": "ok", "message": "done",
            "uid": uid, "expires": "2027-01-01 00:00:00", "days_left": 100,
            "source": "sourceuser", "source_used": 1,
        }
        with (
            patch.object(web_store.locket, "resolve_uid", new=AsyncMock(return_value=uid)),
            patch.object(web_store.activation, "activate", new=AsyncMock(return_value=result)) as activate,
        ):
            async with ClientSession() as session:
                async with session.post(f"{self.base}/api/reactivate", json={"username": "veteran"}) as resp:
                    data = await resp.json()
                self.assertTrue(data["ok"], data)
                self.assertTrue(data["free"])
                activate.assert_awaited_once()

                # Immediately after a successful re-activation the cooldown applies.
                async with session.post(f"{self.base}/api/reactivate", json={"username": "veteran"}) as resp:
                    data2 = await resp.json()
                self.assertEqual(429, resp.status)
                self.assertFalse(data2["ok"])

                # Cooldown is 0 in tests -> allow again if configured that way.
                with patch.object(web_store, "FREE_REACTIVATE_COOLDOWN_MINUTES", 0):
                    async with session.post(f"{self.base}/api/reactivate", json={"username": "veteran"}) as resp:
                        data3 = await resp.json()
                self.assertTrue(data3["ok"], data3)

    async def test_api_redeem_marks_uid_for_free_reactivation(self):
        from aiohttp import ClientSession
        import web_store
        codes = db.gen_cdk(1, admin_id=1, cdk_secret=SECRET, plan="1m", spins=1)
        uid = "Z" * 28
        result = {
            "ok": True, "code": "ok", "message": "done",
            "uid": uid, "expires": "2027-01-01 00:00:00", "days_left": 100,
            "source": "sourceuser", "source_used": 1,
        }
        with patch.object(web_store.activation, "activate", new=AsyncMock(return_value=result)):
            async with ClientSession() as session:
                async with session.post(
                    f"{self.base}/api/redeem", json={"code": codes[0], "username": "alice"}
                ) as resp:
                    data = await resp.json()
        self.assertTrue(data["ok"], data)
        self.assertIsNotNone(db.get_uid_activation(uid))

    async def test_api_redeem_rejects_unknown_key(self):
        from aiohttp import ClientSession
        with patch.object(web_store.activation, "activate", new=AsyncMock()) as activate:
            async with ClientSession() as session:
                async with session.post(
                    f"{self.base}/api/redeem", json={"code": "LK-NOPE", "username": "alice"}
                ) as resp:
                    data = await resp.json()
        self.assertEqual(400, resp.status)
        self.assertFalse(data["ok"])
        activate.assert_not_awaited()

    async def test_api_redeem_refunds_spin_when_activation_fails(self):
        from aiohttp import ClientSession
        codes = db.gen_cdk(1, admin_id=1, cdk_secret=SECRET, plan="1m", spins=1)
        with patch.object(
            web_store.activation, "activate",
            new=AsyncMock(return_value={"ok": False, "code": "no_source", "message": "empty"}),
        ):
            async with ClientSession() as session:
                async with session.post(
                    f"{self.base}/api/redeem", json={"code": codes[0], "username": "alice"}
                ) as resp:
                    data = await resp.json()
        self.assertEqual(400, resp.status)
        self.assertTrue(data["refunded"])
        detail = db.get_cdk_detail(codes[0], secret=SECRET)
        self.assertEqual(1, detail["spins_left"])
        self.assertEqual("valid", detail["status"])

    async def test_api_redeem_success_logs_history(self):
        from aiohttp import ClientSession
        codes = db.gen_cdk(1, admin_id=1, cdk_secret=SECRET, plan="1m", spins=1)
        result = {
            "ok": True, "code": "ok", "message": "done",
            "uid": "U" * 28, "expires": "2027-01-01 00:00:00", "days_left": 100,
            "source": "sourceuser", "source_used": 1,
        }
        with patch.object(web_store.activation, "activate", new=AsyncMock(return_value=result)) as activate:
            async with ClientSession() as session:
                async with session.post(
                    f"{self.base}/api/redeem", json={"code": codes[0], "username": "alice"}
                ) as resp:
                    data = await resp.json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(0, data["spins_left"])
        activate.assert_awaited_once()
        detail = db.get_cdk_detail(codes[0], secret=SECRET)
        self.assertEqual("used", detail["status"])
        history = db.list_key_redemptions(limit=5)
        self.assertTrue(any(row["target"] == "alice" for row in history))


if __name__ == "__main__":
    unittest.main()
