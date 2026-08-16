import os
import sqlite3
import tempfile
import threading
import unittest

from app import database as db

ADMIN_SECRET = "admin-secret-value-with-at-least-32-characters"
RESERVE_SECRET = "reserve-secret-value-with-at-least-32-characters"
PURCHASE_SECRET = "purchase-secret-value-with-at-least-32-characters"
OTHER_SECRET = "different-secret-value-with-at-least-32-characters"


class CdkDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.sqlite3")
        self.original_db_name = db.DB_NAME
        self.original_secret = os.environ.get("CDK_SECRET")
        db.DB_NAME = self.db_path
        db.init_db()

    def tearDown(self):
        db.DB_NAME = self.original_db_name
        if self.original_secret is None:
            os.environ.pop("CDK_SECRET", None)
        else:
            os.environ["CDK_SECRET"] = self.original_secret
        self.tmp.cleanup()

    def _rows(self, query, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    def test_secure_admin_cdk_generation_records_metadata_and_legacy_still_redeems(self):
        with self.assertRaises(ValueError):
            db.gen_cdk(1, admin_id=7001, cdk_secret="short-secret")

        codes = db.gen_cdk(3, admin_id=7001, cdk_secret=ADMIN_SECRET)

        self.assertEqual(3, len(codes))
        for code in codes:
            self.assertRegex(code, r"^LOCK-[A-Z2-9]{8}(?:-[A-Z2-9]{8}){3}$")
            self.assertNotRegex(code, r"^LOCK-[A-Z2-9]{4}$")
            self.assertTrue(db.validate_cdk(code, secret=ADMIN_SECRET))

        rows = self._rows(
            "SELECT code, code_hash, code_nonce, source, created_by, order_id FROM cdk_codes ORDER BY created_ts, code"
        )
        self.assertEqual({row["source"] for row in rows}, {"admin"})
        self.assertEqual({row["created_by"] for row in rows}, {7001})
        self.assertEqual({row["order_id"] for row in rows}, {None})
        self.assertTrue(all(row["code_hash"] for row in rows))
        self.assertTrue(all(not row["code"].startswith("LOCK-") for row in rows))
        self.assertTrue(all(row["code_nonce"] for row in rows))
        self.assertEqual("admin", db.get_cdk_source(codes[0], secret=ADMIN_SECRET))
        self.assertIsNone(db.get_cdk_source(codes[0], secret=OTHER_SECRET))
        self.assertFalse(db.validate_cdk(codes[0], secret=OTHER_SECRET))

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("INSERT INTO cdk_codes (code) VALUES (?)", ("LOCK-ABCD",))
            conn.commit()
        finally:
            conn.close()

        self.assertTrue(db.validate_cdk(" lock-abcd "))
        self.assertTrue(db.redeem_cdk("LOCK-ABCD", user_id=42))
        self.assertFalse(db.redeem_cdk("LOCK-ABCD", user_id=43))

    def test_pre_v2_secure_plaintext_is_migrated_and_still_valid(self):
        nonce = "ABCDEFGHJKLMNPQR"
        old_code = db._code_from_nonce(nonce, secret=ADMIN_SECRET, version=1)
        old_hash = db._code_hash(old_code, secret=ADMIN_SECRET)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO cdk_codes (code, code_hash, source, created_by)
                   VALUES (?, ?, 'admin', ?)""",
                (old_code, old_hash, 7001),
            )
            conn.commit()
        finally:
            conn.close()

        db.init_db()

        row = self._rows(
            "SELECT code, code_nonce, code_version FROM cdk_codes WHERE code_hash = ?",
            (old_hash,),
        )[0]
        self.assertEqual(f"HMAC-{old_hash}", row["code"])
        self.assertEqual(nonce, row["code_nonce"])
        self.assertEqual(1, row["code_version"])
        self.assertTrue(db.validate_cdk(old_code, secret=ADMIN_SECRET))

    def test_reserve_release_and_redeem_are_atomic_for_single_use(self):
        code = db.gen_cdk(1, admin_id=1, cdk_secret=RESERVE_SECRET)[0]

        self.assertTrue(db.reserve_cdk(code, user_id=100, secret=RESERVE_SECRET))
        self.assertTrue(db.reserve_cdk(code, user_id=100, secret=RESERVE_SECRET))
        self.assertTrue(db.lock_reserved_cdk(code, user_id=100, secret=RESERVE_SECRET))
        self.assertFalse(db.validate_cdk(code))
        self.assertFalse(db.reserve_cdk(code, user_id=200, secret=RESERVE_SECRET))
        self.assertFalse(db.release_cdk(code, user_id=200, secret=RESERVE_SECRET))

        self.assertTrue(db.release_cdk(code, user_id=100, secret=RESERVE_SECRET))
        self.assertTrue(db.reserve_cdk(code, user_id=200, secret=RESERVE_SECRET))
        self.assertFalse(db.redeem_cdk(code, user_id=100))
        self.assertTrue(db.redeem_reserved_cdk(code, user_id=200, secret=RESERVE_SECRET))
        self.assertFalse(db.release_cdk(code, user_id=200, secret=RESERVE_SECRET))
        self.assertFalse(db.validate_cdk(code))

    def test_expired_reservations_can_be_reclaimed(self):
        code = db.gen_cdk(1, admin_id=1, cdk_secret=RESERVE_SECRET)[0]
        self.assertTrue(db.reserve_cdk(code, user_id=100, secret=RESERVE_SECRET, ttl_seconds=1))

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE cdk_codes SET reserved_until_ts = ? WHERE reserved_by = ?",
                (1, 100),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertTrue(db.validate_cdk(code, secret=RESERVE_SECRET))
        self.assertFalse(db.redeem_reserved_cdk(code, user_id=100, secret=RESERVE_SECRET))
        self.assertTrue(db.reserve_cdk(code, user_id=200, secret=RESERVE_SECRET))

    def test_order_completion_is_idempotent_and_generates_purchased_cdks_once(self):
        order = db.create_cdk_order(
            user_id=501,
            chat_id=6001,
            quantity=2,
            total_price=99000,
            payment_content="PAY txn-idempotent",
            expires_at=4070908800,
        )
        duplicate = db.create_cdk_order(
            user_id=501,
            chat_id=6001,
            quantity=2,
            total_price=99000,
            payment_content="PAY txn-other",
        )
        self.assertEqual(order["id"], duplicate["id"])
        self.assertEqual("PAY txn-idempotent", duplicate["payment_content"])
        self.assertEqual(1, len(db.get_pending_cdk_orders()))
        with self.assertRaises(sqlite3.IntegrityError):
            db.create_cdk_order(
                user_id=999,
                chat_id=999,
                quantity=1,
                total_price=99000,
                payment_content="PAY txn-idempotent",
            )

        completed = db.complete_cdk_order(
            order_id=order["id"],
            transaction_id="txn-idempotent",
            matched_amount=99000,
            secret=PURCHASE_SECRET,
        )
        self.assertEqual(2, len(completed))

        completed_again = db.complete_cdk_order(
            order_id=order["id"],
            transaction_id="txn-idempotent",
            matched_amount=99000,
            secret=PURCHASE_SECRET,
        )
        self.assertEqual(completed, completed_again)

        cdk_rows = self._rows(
            "SELECT code, code_hash, code_nonce, source, created_by, order_id FROM cdk_codes ORDER BY code"
        )
        self.assertEqual(2, len(cdk_rows))
        self.assertEqual({row["source"] for row in cdk_rows}, {"purchase"})
        self.assertEqual({row["created_by"] for row in cdk_rows}, {501})
        self.assertEqual({row["order_id"] for row in cdk_rows}, {order["id"]})
        self.assertTrue(all(not row["code"].startswith("LOCK-") for row in cdk_rows))
        self.assertTrue(all(row["code_hash"] and row["code_nonce"] for row in cdk_rows))
        self.assertTrue(all(db.validate_cdk(code, secret=PURCHASE_SECRET) for code in completed))
        self.assertEqual(0, len(db.get_pending_cdk_orders()))

    def test_cancel_and_expire_only_affect_pending_orders(self):
        cancel_order = db.create_cdk_order(
            user_id=1,
            chat_id=1001,
            quantity=1,
            total_price=10000,
            payment_content="PAY cancel",
        )
        self.assertTrue(db.cancel_cdk_order(cancel_order["id"], user_id=1, chat_id=1001))
        self.assertEqual("canceled", db.get_cdk_order(cancel_order["id"])["status"])
        self.assertIsNone(
            db.complete_cdk_order(
                order_id=cancel_order["id"],
                transaction_id="txn-cancel",
                matched_amount=10000,
                secret=PURCHASE_SECRET,
            )
        )

        expired_order = db.create_cdk_order(
            user_id=2,
            chat_id=1002,
            quantity=1,
            total_price=10000,
            payment_content="PAY expire",
            expires_at=946684800,
        )
        expired = db.expire_cdk_orders(now=1786726800)
        self.assertEqual([expired_order["id"]], [order["id"] for order in expired])
        self.assertEqual("expired", db.get_cdk_order(expired_order["id"])["status"])
        self.assertEqual(0, len(db.get_pending_cdk_orders(now=1786726800)))

    def test_create_cdk_order_expires_stale_order_before_creating_replacement(self):
        stale = db.create_cdk_order(
            user_id=77,
            chat_id=7700,
            quantity=1,
            total_price=10000,
            payment_content="PAY stale",
            expires_at=946684800,
        )

        replacement = db.create_cdk_order(
            user_id=77,
            chat_id=7700,
            quantity=3,
            total_price=30000,
            payment_content="PAY replacement",
            expires_at=4070908800,
        )

        self.assertNotEqual(stale["id"], replacement["id"])
        self.assertEqual("expired", db.get_cdk_order(stale["id"])["status"])
        self.assertEqual("pending", replacement["status"])
        self.assertEqual("PAY replacement", replacement["payment_content"])
        pending = db.get_pending_cdk_orders(now=1786726800)
        self.assertEqual([replacement["id"]], [order["id"] for order in pending])

    def test_create_cdk_order_reuses_active_pending_order_for_repeated_and_concurrent_requests(self):
        first = db.create_cdk_order(
            user_id=88,
            chat_id=8800,
            quantity=1,
            total_price=10000,
            payment_content="PAY first",
            expires_at=4070908800,
        )

        repeated = db.create_cdk_order(
            user_id=88,
            chat_id=8800,
            quantity=5,
            total_price=50000,
            payment_content="PAY repeated",
            expires_at=4070908800,
        )

        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(1, repeated["quantity"])
        self.assertEqual("PAY first", repeated["payment_content"])

        barrier = threading.Barrier(8)
        results = []
        errors = []
        lock = threading.Lock()

        def create_from_thread(idx):
            try:
                barrier.wait(timeout=5)
                order = db.create_cdk_order(
                    user_id=99,
                    chat_id=9900,
                    quantity=idx + 1,
                    total_price=(idx + 1) * 10000,
                    payment_content=f"PAY concurrent {idx}",
                    expires_at=4070908800,
                )
                with lock:
                    results.append(order)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=create_from_thread, args=(idx,)) for idx in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], errors)
        self.assertEqual(8, len(results))
        self.assertEqual(1, len({order["id"] for order in results}))
        rows = self._rows("SELECT * FROM cdk_orders WHERE user_id = ? AND status = 'pending'", (99,))
        self.assertEqual(1, len(rows))

    def test_get_active_pending_order_for_user_ignores_other_users_and_expired_rows(self):
        active = db.create_cdk_order(
            user_id=55,
            chat_id=550,
            quantity=1,
            total_price=39000,
            payment_content="PAY active-55",
            expires_at=4070908800,
        )
        db.create_cdk_order(
            user_id=66,
            chat_id=660,
            quantity=1,
            total_price=39000,
            payment_content="PAY expired-66",
            expires_at=946684800,
        )

        self.assertEqual(active["id"], db.get_active_cdk_order_for_user(55, now=1786726800)["id"])
        self.assertIsNone(db.get_active_cdk_order_for_user(66, now=1786726800))
        self.assertIsNone(db.get_active_cdk_order_for_user(77, now=1786726800))


if __name__ == "__main__":
    unittest.main()
