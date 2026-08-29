import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

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
        web_store._complete_web_order = lambda order_id: None

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

    async def test_verify_page_and_api(self):
        from aiohttp import ClientSession
        codes = db.gen_cdk(1, admin_id=0, cdk_secret=SECRET)
        async with ClientSession() as session:
            async with session.post(f"{self.base}/api/verify", json={"code": codes[0]}) as resp:
                data = await resp.json()
        self.assertEqual("valid", data["status"])

        db.redeem_cdk(codes[0], user_id=99, secret=SECRET)
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

            # Wrong password fails even with a valid CSRF token.
            async with session.post(
                f"{self.base}/admin/login",
                data={"username": "admin", "password": "wrong", "csrf": m.group(1)},
            ) as resp:
                body = await resp.text()
                self.assertIn("Sai tên đăng nhập", body)

            # Missing CSRF token is rejected.
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

    async def test_admin_generate_cdk_requires_csrf(self):
        from aiohttp import ClientSession, CookieJar
        jar = CookieJar(unsafe=True)
        async with ClientSession(cookie_jar=jar) as session:
            token = await self._admin_login(session)

            # Without the CSRF header the request is rejected.
            async with session.post(f"{self.base}/admin/cdks/generate", json={"count": 5}) as resp:
                self.assertEqual(403, resp.status)

            async with session.post(
                f"{self.base}/admin/cdks/generate",
                json={"count": 5},
                headers={"X-CSRF-Token": token},
            ) as resp:
                data = await resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(5, len(data["codes"]))
            for code in data["codes"]:
                self.assertTrue(db.validate_cdk(code, secret=SECRET))

    async def test_admin_password_hash_verification(self):
        import web_store
        import base64
        import hashlib

        # Hash round-trips against _verify_admin_password.
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
        # Malformed hash never verifies.
        web_store.WEB_ADMIN_PASSWORD_HASH = "pbkdf2$not-an-int$xx$yy"
        self.assertFalse(web_store._verify_admin_password("hunter2"))
        # Legacy plaintext fallback still works.
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
        import asyncio

        async def fake_resolve(username):
            return {"uid": "UID-XYZ", "avatar": "//cdn.example/av.jpg"}

        async def fake_status(uid):
            return {"active": False}

        original_resolve = web_store.locket.resolve_profile
        original_status = web_store.locket.check_status
        web_store.locket.resolve_profile = fake_resolve
        web_store.locket.check_status = fake_status
        try:
            async with ClientSession() as session:
                async with session.post(f"{self.base}/api/check", json={"username": "alice"}) as resp:
                    data = await resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual("UID-XYZ", data["uid"])
            self.assertEqual("alice", data["username"])
            self.assertEqual("https://cdn.example/av.jpg", data["avatar"])
            self.assertFalse(data["gold_active"])
            self.assertFalse(data["paid"])
            self.assertIsNone(data["activation"])

            async with ClientSession() as session:
                async with session.post(
                    f"{self.base}/api/check",
                    json={"username": "https://locket.cam/bob"},
                ) as resp:
                    data = await resp.json()
            self.assertEqual("bob", data["username"])
        finally:
            web_store.locket.resolve_profile = original_resolve
            web_store.locket.check_status = original_status

    async def test_api_check_reports_saved_activation(self):
        from aiohttp import ClientSession
        import web_store

        async def fake_resolve(username):
            return {"uid": "UID-ACT-1", "avatar": None}

        async def fake_status(uid):
            return {"active": False}

        original_resolve = web_store.locket.resolve_profile
        original_status = web_store.locket.check_status
        web_store.locket.resolve_profile = fake_resolve
        web_store.locket.check_status = fake_status
        act_id = None
        try:
            async with ClientSession() as session:
                async with session.post(
                    f"{self.base}/api/check",
                    json={"username": "veteran"},
                ) as resp:
                    data = await resp.json()
            self.assertTrue(data["ok"])
            self.assertIsNone(data["activation"])

            act_id = web_store.db.create_web_activation(
                None, 424242, "UID-ACT-1", "veteran", status="queued",
            )
            web_store.db.update_web_activation(
                act_id,
                status="success",
                cdk_code="LOCK-OLD-CODE-1",
                dns_link="101.1.1.1",
                result="Đã kích hoạt thành công",
            )
            async with ClientSession() as session:
                async with session.post(
                    f"{self.base}/api/check",
                    json={"username": "veteran"},
                ) as resp:
                    data = await resp.json()
            self.assertEqual("success", data["activation"]["status"])
            self.assertEqual("LOCK-OLD-CODE-1", data["activation"]["cdk_code"])
            self.assertEqual("101.1.1.1", data["activation"]["dns_link"])
        finally:
            if act_id is not None:
                web_store.db.delete_web_activation(act_id)
            web_store.locket.resolve_profile = original_resolve
            web_store.locket.check_status = original_status

    async def test_api_check_not_found(self):
        from aiohttp import ClientSession
        import web_store

        original = web_store.locket.resolve_profile
        async def _missing(username):
            return None
        web_store.locket.resolve_profile = _missing
        try:
            async with ClientSession() as session:
                async with session.post(f"{self.base}/api/check", json={"username": "ghost"}) as resp:
                    data = await resp.json()
            self.assertEqual(404, resp.status)
            self.assertFalse(data["ok"])
        finally:
            web_store.locket.resolve_profile = original

    async def test_api_activate_paid_and_free_flow(self):
        from aiohttp import ClientSession
        db.mark_uid_paid("UID-PAID", order_id=999)

        # Free re-activation: no order, straight to queued.
        async with ClientSession() as session:
            async with session.post(
                f"{self.base}/api/activate",
                json={"uid": "UID-PAID", "username": "veteran"},
            ) as resp:
                data = await resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["free"])
        act = db.get_web_activation(id=data["activation_id"])
        self.assertEqual("queued", act["status"])
        self.assertIsNone(act["order_id"])

        # Paid flow: creates order + awaiting_payment activation.
        async with ClientSession() as session:
            async with session.post(
                f"{self.base}/api/activate",
                json={"uid": "UID-NEW", "username": "fresh"},
            ) as resp:
                data = await resp.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["free"])
        order = db.get_cdk_order(id=data["order_id"])
        self.assertEqual("pending", order["status"])
        act = db.get_web_activation(order_id=data["order_id"])
        self.assertEqual("awaiting_payment", act["status"])
        self.assertEqual("UID-NEW", act["uid"])

        # Payment confirmed: activation moves to 'paid', NOT queued yet.
        db.update_web_activation(act["id"], status="paid", cdk_code="LOCK-PAID-1")
        db.mark_uid_paid("UID-NEW", order_id=data["order_id"])
        act = db.get_web_activation(id=act["id"])
        self.assertEqual("paid", act["status"])

        # /start flips paid -> queued so the bot worker picks it up.
        async with ClientSession() as session:
            async with session.post(f"{self.base}/api/order/{data['order_id']}/start") as resp:
                start_data = await resp.json()
        self.assertTrue(start_data["ok"])
        self.assertEqual("queued", start_data["status"])
        act = db.get_web_activation(id=act["id"])
        self.assertEqual("queued", act["status"])

    async def test_api_activation_status_polls(self):
        from aiohttp import ClientSession
        act_id = db.create_web_activation(None, visitor_id=-5, uid="UID-POLL", username="poll", status="queued")
        async with ClientSession() as session:
            async with session.get(f"{self.base}/api/activation/{act_id}") as resp:
                data = await resp.json()
        self.assertEqual("queued", data["status"])
        self.assertEqual("poll", data["username"])

    async def test_api_activate_cdk_flow(self):
        from aiohttp import ClientSession
        import web_store
        codes = db.gen_cdk(1, admin_id=1, secret=web_store.CDK_SECRET)
        cdk_code = codes[0]
        async with ClientSession() as session:
            async with session.post(
                f"{self.base}/api/activate-cdk",
                json={"uid": "UID-CDK", "username": "cdkuser", "code": cdk_code},
            ) as resp:
                data = await resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["free"])
        self.assertIn("activation_id", data)
        act = db.get_web_activation(id=data["activation_id"])
        self.assertEqual("queued", act["status"])
        self.assertIsNone(act["order_id"])
        self.assertEqual(cdk_code, act["cdk_code"])
        self.assertTrue(db.is_uid_paid("UID-CDK"))

        # Same CDK cannot be reused while reserved.
        async with ClientSession() as session:
            async with session.post(
                f"{self.base}/api/activate-cdk",
                json={"uid": "UID-CDK2", "username": "cdkuser2", "code": cdk_code},
            ) as resp:
                data2 = await resp.json()
        self.assertFalse(data2["ok"])

        # Invalid CDK is rejected.
        async with ClientSession() as session:
            async with session.post(
                f"{self.base}/api/activate-cdk",
                json={"uid": "UID-CDK3", "username": "cdkuser3", "code": "LOCK-NOPE-1"},
            ) as resp:
                data3 = await resp.json()
        self.assertFalse(data3["ok"])
        self.assertFalse(db.is_uid_paid("UID-CDK3"))
