import os
import tempfile
import unittest

from app import database as db

SECRET = "web-store-secret-value-with-at-least-32-characters"


class WebStoreDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.sqlite3")
        self.original_db_name = db.DB_NAME
        db.DB_NAME = self.db_path
        db.init_db()

    def tearDown(self):
        db.DB_NAME = self.original_db_name
        self.tmp.cleanup()

    def test_get_cdk_detail_reports_valid_used_and_not_found(self):
        codes = db.gen_cdk(2, admin_id=100, cdk_secret=SECRET)

        detail = db.get_cdk_detail(codes[0], secret=SECRET)
        self.assertTrue(detail["found"])
        self.assertEqual("valid", detail["status"])
        self.assertEqual("admin", detail["source"])

        self.assertTrue(db.redeem_cdk(codes[0], user_id=42, secret=SECRET))
        used = db.get_cdk_detail(codes[0], secret=SECRET)
        self.assertTrue(used["found"])
        self.assertEqual("used", used["status"])
        self.assertEqual(42, used["used_by"])

        missing = db.get_cdk_detail("LOCK-NOT-EXISTING", secret=SECRET)
        self.assertFalse(missing["found"])
        self.assertEqual("not_found", missing["status"])

    def test_get_cdk_detail_reports_active_reservation(self):
        codes = db.gen_cdk(1, admin_id=100, cdk_secret=SECRET)
        db.reserve_cdk(codes[0], user_id=7, secret=SECRET, ttl_seconds=3600)

        detail = db.get_cdk_detail(codes[0], secret=SECRET)
        self.assertTrue(detail["found"])
        self.assertEqual("reserved", detail["status"])

    def test_list_cdk_orders_and_stats(self):
        order1 = db.create_cdk_order(
            user_id=-123, chat_id=None, quantity=2,
            total_price=200000, payment_content="CDKAAAA",
            expires_at=None,
        )
        order2 = db.create_cdk_order(
            user_id=-456, chat_id=None, quantity=1,
            total_price=100000, payment_content="CDKBBBB",
            expires_at=None,
        )
        codes = db.complete_cdk_order(
            order_id=order2["id"], transaction_id="tx-web-1",
            matched_amount=100000, cdk_secret=SECRET,
        )
        self.assertEqual(1, len(codes))

        orders = db.list_cdk_orders()
        self.assertEqual(2, len(orders))
        pending = db.list_cdk_orders(status="pending")
        self.assertEqual(1, len(pending))
        self.assertEqual(order1["id"], pending[0]["id"])

        stats = db.cdk_order_stats()
        self.assertEqual(2, stats["total"])
        self.assertEqual(1, stats["pending"])
        self.assertEqual(1, stats["completed"])
        self.assertEqual(100000, stats["revenue"])

    def test_list_cdk_codes_decodes_secure_codes(self):
        codes = db.gen_cdk(3, admin_id=100, cdk_secret=SECRET)
        listed = db.list_cdk_codes(limit=10, secret=SECRET)

        self.assertEqual(3, len(listed))
        listed_codes = {row.get("code") for row in listed}
        self.assertEqual(set(codes), listed_codes)
        for row in listed:
            self.assertEqual(0, row["used"])
            self.assertEqual("admin", row["source"])

    def test_web_activation_lifecycle_and_paid_uids(self):
        act_id = db.create_web_activation(
            order_id=10, visitor_id=-7, uid="UID-AAA", username="alice",
        )
        act = db.get_web_activation(id=act_id)
        self.assertEqual("awaiting_payment", act["status"])
        self.assertEqual("alice", act["username"])

        self.assertTrue(db.update_web_activation(act_id, status="queued", cdk_code="LOCK-XXXX"))
        queued = db.list_queued_web_activations()
        self.assertEqual(1, len(queued))
        self.assertEqual(act_id, queued[0]["id"])

        self.assertTrue(db.claim_web_activation(act_id))
        self.assertFalse(db.claim_web_activation(act_id))
        act = db.get_web_activation(id=act_id)
        self.assertEqual("processing", act["status"])
        self.assertEqual("LOCK-XXXX", act["cdk_code"])

        self.assertTrue(db.update_web_activation(act_id, status="success", dns_link="https://dns.example"))
        act = db.get_web_activation(id=act_id)
        self.assertEqual("success", act["status"])
        self.assertEqual("https://dns.example", act["dns_link"])

        self.assertFalse(db.is_uid_paid("UID-AAA"))
        db.mark_uid_paid("UID-AAA", order_id=10)
        self.assertTrue(db.is_uid_paid("UID-AAA"))
        db.mark_uid_paid("UID-AAA", order_id=11)
        self.assertTrue(db.is_uid_paid("UID-AAA"))

        stats = db.web_activation_stats()
        self.assertEqual(1, stats["success"])

    def test_web_activation_by_order_and_free_reactivation(self):
        paid_id = db.create_web_activation(None, visitor_id=-9, uid="UID-BBB", username="bob", status="queued")
        self.assertEqual("queued", db.get_web_activation(id=paid_id)["status"])
        self.assertIsNone(db.get_web_activation(id=paid_id)["order_id"])

        order_id = 77
        act_id = db.create_web_activation(order_id, visitor_id=-9, uid="UID-CCC", username="carol")
        found = db.get_web_activation(order_id=order_id)
        self.assertEqual(act_id, found["id"])

        rows = db.list_web_activations(limit=10)
        self.assertEqual(2, len(rows))

    def test_get_web_activation_by_uid_returns_latest(self):
        old = db.create_web_activation(None, visitor_id=-1, uid="UID-ZZZ", username="zoe", status="queued")
        db.update_web_activation(old, status="success", cdk_code="LOCK-FIRST")
        new = db.create_web_activation(None, visitor_id=-2, uid="UID-ZZZ", username="zoe", status="queued")

        latest = db.get_web_activation(uid="UID-ZZZ")
        self.assertEqual(new, latest["id"])
        self.assertEqual("queued", latest["status"])

        self.assertIsNone(db.get_web_activation(uid="UID-NOPE"))

        self.assertTrue(db.delete_web_activation(old))
        self.assertTrue(db.delete_web_activation(new))
        self.assertIsNone(db.get_web_activation(uid="UID-ZZZ"))
