import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import bot
from app import database as db
from app.services import activation


def _future(days):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


class SourcePoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.patch = patch.object(db, "DB_NAME", self.db_path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        db.init_db()

    def test_add_and_normalize_sources(self):
        self.assertTrue(db.add_gold_source("https://locket.cam/@Alice", count=2, expires=_future(30)))
        self.assertTrue(db.add_gold_source("alice", count=1, expires=_future(40)))
        sources = db.list_gold_sources()
        self.assertEqual(1, len(sources))
        self.assertEqual(2, sources[0]["count"])

    def test_short_expiry_is_rejected(self):
        self.assertFalse(db.add_gold_source("bob", expires=_future(3)))
        self.assertEqual([], db.list_gold_sources())

    def test_month_plan_prefers_25_to_30_days(self):
        db.add_gold_source("long", expires=_future(300))
        db.add_gold_source("mid", expires=_future(28))
        reserved = db.reserve_gold_source("1m")
        self.assertEqual("mid", reserved["username"])

    def test_year_plan_prefers_200_to_360_days(self):
        db.add_gold_source("short", expires=_future(28))
        db.add_gold_source("long", expires=_future(300))
        reserved = db.reserve_gold_source("1y")
        self.assertEqual("long", reserved["username"])

    def test_release_success_increments_and_exhausted_removes(self):
        db.add_gold_source("alice", expires=_future(30))
        reserved = db.reserve_gold_source("1m")
        db.release_gold_source(reserved["id"], success=True)
        source = db.list_gold_sources()[0]
        self.assertEqual(1, source["count"])
        self.assertEqual(0, source["in_flight"])

        db.release_gold_source(reserved["id"], exhausted=True)
        self.assertEqual([], db.list_gold_sources())

    def test_full_source_is_not_reserved(self):
        db.add_gold_source("full", count=5, expires=_future(30))
        self.assertIsNone(db.reserve_gold_source("1m"))

    def test_cleanup_drops_expired_sources(self):
        db.add_gold_source("usable", expires=_future(30))
        # Insert directly to bypass the min-days guard.
        conn = db._connect()
        conn.execute(
            "INSERT INTO gold_sources (username, count, in_flight, expires_at, expires_ts, created_at, updated_at) VALUES (?, 0, 0, ?, ?, 0, 0)",
            ("stale", _future(-1), int((datetime.now() - timedelta(days=1)).timestamp())),
        )
        conn.commit()
        conn.close()
        self.assertEqual(1, db.cleanup_gold_sources(min_days=10))
        self.assertEqual(["usable"], [s["username"] for s in db.list_gold_sources()])


class ActivationEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.patch = patch.object(db, "DB_NAME", self.db_path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        db.init_db()
        db.add_gold_source("source1", expires=_future(28))
        db.add_gold_source("source2", expires=_future(29))

    def _locket_patches(self, dest_first=None, dest_final=None, source_status=None, alias_results=None):
        dest_first = dest_first or {"active": False, "expires": "Unknown"}
        dest_final = dest_final or dest_first
        source_status = source_status or {"active": True, "expires": _future(28)}
        alias_results = list(alias_results or [(True, "SUCCESS")])
        dest_uid = "D" * 28
        source_uid = "S" * 28
        counter = {"dest": 0}

        async def fake_resolve(raw, proxy_url=None):
            if raw == "nobody":
                return None
            if raw == "alice":
                return dest_uid
            return source_uid

        async def fake_status(uid, proxy_url=None):
            if uid == dest_uid:
                counter["dest"] += 1
                return dest_first if counter["dest"] == 1 else dest_final
            return source_status

        async def fake_alias(source, dest, proxy_url=None):
            return alias_results.pop(0) if alias_results else (False, "unexpected")

        return (
            patch.object(activation.locket, "resolve_uid", new=AsyncMock(side_effect=fake_resolve)),
            patch.object(activation.locket, "check_status", new=AsyncMock(side_effect=fake_status)),
            patch.object(activation.locket, "alias_subscriber", new=AsyncMock(side_effect=fake_alias)),
        )

    async def test_successful_activation_bumps_source_count(self):
        p1, p2, p3 = self._locket_patches(
            dest_final={"active": True, "expires": _future(30)},
        )
        with p1, p2, p3:
            result = await activation.activate("alice", plan="1m")
        self.assertTrue(result["ok"], result)
        self.assertEqual("source1", result["source"])
        self.assertEqual(1, result["source_used"])
        source = next(s for s in db.list_gold_sources() if s["username"] == "source1")
        self.assertEqual(1, source["count"])
        self.assertEqual(0, source["in_flight"])

    async def test_alias_limit_fails_over_to_the_next_source(self):
        p1, p2, p3 = self._locket_patches(alias_results=[
            (False, "Alias limit reached"),
            (True, "SUCCESS"),
        ])
        with p1, p2, p3:
            result = await activation.activate("alice", plan="1m")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["source"])
        # The limited source is retired, the healthy one stays.
        self.assertEqual(1, len(db.list_gold_sources()))

    async def test_a_source_that_hits_alias_limit_is_retired(self):
        db.add_gold_source("source1", count=4, expires=_future(28))
        p1, p2, p3 = self._locket_patches(alias_results=[(False, "Alias limit reached")])
        with p1, p2, p3:
            result = await activation.activate("alice", plan="1m")
        self.assertFalse(result["ok"])
        names = {s["username"] for s in db.list_gold_sources()}
        self.assertNotIn("source1", names)

    async def test_no_source_returns_no_source_code(self):
        db.cleanup_gold_sources(min_days=0)
        conn = db._connect()
        conn.execute("DELETE FROM gold_sources")
        conn.commit()
        conn.close()
        p1, p2, p3 = self._locket_patches()
        with p1, p2, p3:
            result = await activation.activate("alice", plan="1m")
        self.assertFalse(result["ok"])
        self.assertEqual("no_source", result["code"])

    async def test_existing_long_gold_is_rejected(self):
        p1, p2, p3 = self._locket_patches(
            dest_first={"active": True, "expires": _future(300)},
        )
        with p1, p2, p3:
            result = await activation.activate("alice", plan="1m")
        self.assertFalse(result["ok"])
        self.assertEqual("already_gold", result["code"])
        source = next(s for s in db.list_gold_sources() if s["username"] == "source1")
        self.assertEqual(0, source["count"])

    async def test_year_plan_can_overwrite_short_remaining_gold(self):
        p1, p2, p3 = self._locket_patches(
            dest_first={"active": True, "expires": _future(100)},
        )
        with p1, p2, p3:
            result = await activation.activate("alice", plan="1y")
        self.assertTrue(result["ok"], result)

    async def test_destination_not_found(self):
        with (
            patch.object(activation.locket, "resolve_uid", new=AsyncMock(return_value=None)),
            patch.object(activation.locket, "check_status", new=AsyncMock()) as check,
        ):
            result = await activation.activate("nobody", plan="1m")
        self.assertFalse(result["ok"])
        self.assertEqual("not_found", result["code"])
        check.assert_not_awaited()

    async def test_check_source_reports_states(self):
        source = db.list_gold_sources()[0]
        with (
            patch.object(activation.locket, "resolve_uid", new=AsyncMock(return_value="U" * 28)),
            patch.object(activation.locket, "check_status", new=AsyncMock(return_value={
                "active": True, "expires": _future(28),
            })),
            patch.object(activation.locket, "alias_subscriber", new=AsyncMock(return_value=(True, "SUCCESS"))) as alias,
        ):
            outcome = await activation.check_source(source, probe=True)
        self.assertEqual("usable", outcome["status"])
        self.assertTrue(outcome["expires"])
        alias.assert_awaited_once()


class CheckHarvestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.patch = patch.object(db, "DB_NAME", self.db_path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        db.init_db()

    def _update(self):
        status_msg = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        message = SimpleNamespace(reply_text=AsyncMock(return_value=status_msg))
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(id=70),
            message=message,
        )
        return update, status_msg

    async def test_single_check_harvests_long_lived_gold(self):
        update, status_msg = self._update()
        context = SimpleNamespace(user_data={}, args=["golduser"])
        with (
            patch.object(bot, "GOLD_MIN_SOURCE_DAYS", 10),
            patch.object(bot, "_check_account", new=AsyncMock(return_value=(
                "U" * 28, {"active": True, "expires": _future(120)}, None,
            ))),
        ):
            await bot._do_check(update, context, "golduser", "VI")
        self.assertTrue(db.gold_source_exists("golduser"))
        text = status_msg.edit_text.await_args.args[0]
        self.assertIn("kho nguồn", text)

    async def test_single_check_ignores_short_lived_gold(self):
        update, status_msg = self._update()
        context = SimpleNamespace(user_data={}, args=["shorty"])
        with (
            patch.object(bot, "GOLD_MIN_SOURCE_DAYS", 10),
            patch.object(bot, "_check_account", new=AsyncMock(return_value=(
                "S" * 28, {"active": True, "expires": _future(3)}, None,
            ))),
        ):
            await bot._do_check(update, context, "shorty", "VI")
        self.assertFalse(db.gold_source_exists("shorty"))

    async def test_checksources_probes_by_default_and_quick_skips_probe(self):
        db.add_gold_source("srcprobe", expires=_future(300))
        seen = []

        async def fake_check_source(source, probe=False, proxy_url=None):
            seen.append(probe)
            return {
                "username": source["username"], "status": "usable",
                "uid": "U" * 28, "days_left": 300, "expires": _future(300),
            }

        def update_for(args):
            status_msg = SimpleNamespace(edit_text=AsyncMock())
            return SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                message=SimpleNamespace(reply_text=AsyncMock(return_value=status_msg)),
            )

        with (
            patch.object(bot, "ADMIN_ID", 1),
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot.activation, "check_source", new=AsyncMock(side_effect=fake_check_source)),
        ):
            await bot.cmd_checksources(update_for([]), SimpleNamespace(args=[]))
            await bot.cmd_checksources(update_for(["quick"]), SimpleNamespace(args=["quick"]))

        self.assertEqual([True, False], seen)


if __name__ == "__main__":
    unittest.main()
