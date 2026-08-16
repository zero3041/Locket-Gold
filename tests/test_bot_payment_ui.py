import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app import bot


def _callback_update(data, *, user_id=101, chat_id=202):
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat_id=chat_id,
            message_id=303,
            edit_caption=AsyncMock(),
            reply_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query), query


def _context():
    telegram_bot = SimpleNamespace(
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
    )
    return SimpleNamespace(
        bot=telegram_bot,
        application=SimpleNamespace(bot=telegram_bot),
        user_data={},
    )


class BuyCdkUiTests(unittest.TestCase):
    def test_quantity_keyboard_offers_exactly_one_to_five(self):
        keyboard = bot.get_buy_cdk_keyboard("VI").inline_keyboard

        callbacks = [button.callback_data for row in keyboard for button in row]

        self.assertEqual(callbacks, [
            "buy_cdk_qty_1",
            "buy_cdk_qty_2",
            "buy_cdk_qty_3",
            "buy_cdk_qty_4",
            "buy_cdk_qty_5",
        ])

    def test_buy_button_is_callback_not_external_contact_url(self):
        keyboard = bot.get_cdk_contact_keyboard().inline_keyboard
        button = keyboard[0][0]

        self.assertEqual(button.callback_data, "buy_cdk")
        self.assertIsNone(button.url)

    def test_only_admin_generated_cdk_gets_donate_photo(self):
        self.assertTrue(bot.should_send_donate_photo("admin"))
        self.assertFalse(bot.should_send_donate_photo("purchase"))
        self.assertFalse(bot.should_send_donate_photo(None))

    def test_cdk_attempt_limiter_blocks_sixth_attempt_in_window(self):
        limiter = bot.CdkAttemptLimiter(max_attempts=5, window_seconds=300)

        self.assertEqual([limiter.allow(7, now=100) for _ in range(5)], [True] * 5)
        self.assertFalse(limiter.allow(7, now=100))
        self.assertTrue(limiter.allow(7, now=401))


class BuyCdkQrTests(unittest.IsolatedAsyncioTestCase):
    def test_vietqr_response_must_be_a_real_image(self):
        with self.assertRaises(bot.VietQrImageError):
            bot._validate_vietqr_image(b"invalid acqId")

    async def test_order_qr_contains_amount_content_and_safe_callbacks(self):
        telegram_bot = AsyncMock()
        order = {
            "id": 12,
            "quantity": 3,
            "total_price": 150_000,
            "payment_content": "CDKABC123",
        }

        with (
            patch.object(bot, "BANK_BIN", "970422"),
            patch.object(bot, "BANK_ACCOUNT", "1234567890"),
            patch.object(bot, "BANK_NAME", "MB"),
            patch.object(bot, "BANK_OWNER", "LOCKET SHOP"),
            patch.object(
                bot,
                "_download_vietqr_image",
                new=AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + b"x" * 100),
            ) as download_qr,
        ):
            await bot._send_cdk_order_qr(telegram_bot, 88, order, "VI")

        kwargs = telegram_bot.send_photo.await_args.kwargs
        qr_url = download_qr.await_args.args[0]
        self.assertIn("amount=150000", qr_url)
        self.assertIn("addInfo=CDKABC123", qr_url)
        self.assertEqual("cdk-payment-12.png", kwargs["photo"].filename)
        callbacks = [
            button.callback_data
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(callbacks, ["buy_cdk_check_12", "buy_cdk_cancel_12"])


class BuyCdkCallbackGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_creation_rate_limit_blocks_before_database_write(self):
        update, query = _callback_update("buy_cdk_qty_1", user_id=7, chat_id=70)
        context = _context()

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot, "payment_config_errors", return_value=[]),
            patch.object(bot.db, "get_active_cdk_order_for_user", return_value=None),
            patch.object(bot.payment_order_limiter, "allow", return_value=False),
            patch.object(bot.db, "create_cdk_order") as create_order,
            patch.object(bot, "_send_cdk_order_qr", new=AsyncMock()) as send_qr,
        ):
            await bot.callback_handler(update, context)

        create_order.assert_not_called()
        send_qr.assert_not_awaited()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_rate_limited_user_can_still_resend_existing_pending_order(self):
        update, query = _callback_update("buy_cdk_qty_5", user_id=7, chat_id=70)
        context = _context()
        existing_order = {
            "id": 42,
            "user_id": 7,
            "chat_id": 70,
            "quantity": 1,
            "total_price": 39000,
            "payment_content": "CDKEXISTING",
        }

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot, "payment_config_errors", return_value=[]),
            patch.object(bot.db, "get_active_cdk_order_for_user", return_value=existing_order),
            patch.object(bot.payment_order_limiter, "allow", return_value=False) as limiter,
            patch.object(bot.db, "create_cdk_order") as create_order,
            patch.object(bot, "_send_cdk_order_qr", new=AsyncMock()) as send_qr,
        ):
            await bot.callback_handler(update, context)

        limiter.assert_not_called()
        create_order.assert_not_called()
        send_qr.assert_awaited_once_with(context.bot, 70, existing_order, "VI")

    async def test_any_supported_qr_delivery_failure_cancels_new_order(self):
        update, query = _callback_update("buy_cdk_qty_1", user_id=7, chat_id=70)
        context = _context()
        order = {
            "id": 42,
            "user_id": 7,
            "chat_id": 70,
            "quantity": 1,
            "total_price": 39000,
            "payment_content": "CDKNEW123",
        }

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot, "payment_config_errors", return_value=[]),
            patch.object(bot, "CDK_UNIT_PRICE", 39000),
            patch.object(bot.db, "get_active_cdk_order_for_user", return_value=None),
            patch.object(bot.payment_order_limiter, "allow", return_value=True),
            patch.object(bot, "_new_payment_content", return_value="CDKNEW123"),
            patch.object(bot.db, "create_cdk_order", return_value=order),
            patch.object(
                bot,
                "_send_cdk_order_qr",
                new=AsyncMock(side_effect=ValueError("invalid bank account")),
            ),
            patch.object(bot.db, "cancel_cdk_order", return_value=True) as cancel,
        ):
            await bot.callback_handler(update, context)

        cancel.assert_called_once_with(42, user_id=7, chat_id=70)
        query.message.reply_text.assert_awaited_once()

    async def test_existing_order_from_another_chat_is_not_disclosed(self):
        update, query = _callback_update("buy_cdk_qty_1", user_id=7, chat_id=70)
        context = _context()
        existing_order = {
            "id": 42,
            "user_id": 7,
            "chat_id": 999,
            "quantity": 1,
            "total_price": 39000,
            "payment_content": "CDKEXISTING",
        }

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot, "payment_config_errors", return_value=[]),
            patch.object(bot, "CDK_UNIT_PRICE", 39000),
            patch.object(bot.db, "get_active_cdk_order_for_user", return_value=existing_order),
            patch.object(bot.payment_order_limiter, "allow", return_value=True),
            patch.object(bot, "_new_payment_content", return_value="CDKNEW123"),
            patch.object(bot.db, "create_cdk_order") as create_order,
            patch.object(bot, "_send_cdk_order_qr", new=AsyncMock()) as send_qr,
        ):
            await bot.callback_handler(update, context)

        send_qr.assert_not_awaited()
        create_order.assert_not_called()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_qr_generation_failure_cancels_order_and_tells_user(self):
        update, query = _callback_update("buy_cdk_qty_1", user_id=7, chat_id=70)
        context = _context()
        order = {
            "id": 42,
            "user_id": 7,
            "chat_id": 70,
            "quantity": 1,
            "total_price": 39000,
            "payment_content": "CDKABC123",
        }

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot, "payment_config_errors", return_value=[]),
            patch.object(bot, "CDK_UNIT_PRICE", 39000),
            patch.object(bot.db, "get_active_cdk_order_for_user", return_value=None),
            patch.object(bot, "_new_payment_content", return_value="CDKABC123"),
            patch.object(bot.db, "create_cdk_order", return_value=order),
            patch.object(
                bot,
                "_send_cdk_order_qr",
                new=AsyncMock(side_effect=bot.VietQrImageError("invalid VietQR image")),
            ),
            patch.object(bot.db, "cancel_cdk_order", return_value=True) as cancel,
        ):
            await bot.callback_handler(update, context)

        cancel.assert_called_once_with(42, user_id=7, chat_id=70)
        query.message.reply_text.assert_awaited_once()
        self.assertIn("không tạo được mã QR", query.message.reply_text.await_args.args[0])

    async def test_buy_cdk_quantity_fails_closed_when_payment_config_is_invalid(self):
        update, query = _callback_update("buy_cdk_qty_2", user_id=7, chat_id=70)
        context = _context()

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot, "payment_config_errors", return_value=["SEPAY_API_TOKEN"]),
            patch.object(bot.db, "create_cdk_order") as create_order,
            patch.object(bot, "_send_cdk_order_qr", new=AsyncMock()) as send_qr,
        ):
            await bot.callback_handler(update, context)

        create_order.assert_not_called()
        send_qr.assert_not_awaited()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertIn("Thanh toán", query.answer.await_args.args[0])

    async def test_check_rejects_order_that_belongs_to_another_user(self):
        update, query = _callback_update("buy_cdk_check_42", user_id=7, chat_id=70)
        context = _context()

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot.db, "get_cdk_order", return_value={
                "id": 42,
                "user_id": 999,
                "chat_id": 70,
                "status": "pending",
            }),
            patch.object(bot, "_complete_cdk_payment", new=AsyncMock()) as complete,
        ):
            await bot.callback_handler(update, context)

        complete.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertIn("Đơn không tồn tại", query.answer.await_args.args[0])

    async def test_manual_payment_check_rate_limit_blocks_sepay_request(self):
        update, query = _callback_update("buy_cdk_check_42", user_id=7, chat_id=70)
        context = _context()

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot.db, "get_cdk_order", return_value={
                "id": 42,
                "user_id": 7,
                "chat_id": 70,
                "status": "pending",
            }),
            patch.object(bot.payment_manual_check_limiter, "allow", return_value=False),
            patch.object(bot, "_complete_cdk_payment", new=AsyncMock()) as complete,
        ):
            await bot.callback_handler(update, context)

        complete.assert_not_awaited()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])


class BuyCdkSepayBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_sepay_budget_blocks_external_request(self):
        application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        order = {"id": 9, "payment_content": "CDK9", "total_price": 39000}

        with (
            patch.object(bot.sepay_global_limiter, "allow", return_value=False),
            patch.object(bot.sepay, "SePayClient") as client,
        ):
            result = await bot._complete_cdk_payment(application, order, "VI")

        self.assertFalse(result)
        client.assert_not_called()

    async def test_cancel_rejects_order_that_does_not_match_user_and_chat(self):
        update, query = _callback_update("buy_cdk_cancel_42", user_id=7, chat_id=70)
        context = _context()

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot.db, "cancel_cdk_order", return_value=False) as cancel,
        ):
            await bot.callback_handler(update, context)

        cancel.assert_called_once_with(42, user_id=7, chat_id=70)
        query.message.edit_caption.assert_not_awaited()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertIn("Đơn không tồn tại", query.answer.await_args.args[0])

    async def test_completed_order_check_resends_existing_cdks_without_sepay_recheck(self):
        update, query = _callback_update("buy_cdk_check_42", user_id=7, chat_id=70)
        context = _context()
        completed_order = {
            "id": 42,
            "user_id": 7,
            "chat_id": 70,
            "status": "completed",
            "transaction_id": "txn-42",
            "matched_amount": 100000,
        }

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot.db, "get_cdk_order", return_value=completed_order),
            patch.object(bot.db, "complete_cdk_order", return_value=["LOCK-AAA", "LOCK-BBB"]) as complete_order,
            patch.object(bot, "_complete_cdk_payment", new=AsyncMock()) as sepay_complete,
            patch.object(bot, "CDK_SECRET", "secret-value-with-at-least-32-characters"),
        ):
            await bot.callback_handler(update, context)

        sepay_complete.assert_not_awaited()
        complete_order.assert_called_once_with(
            order_id=42,
            transaction_id="txn-42",
            matched_amount=100000,
            secret="secret-value-with-at-least-32-characters",
        )
        query.answer.assert_awaited_once_with("✅ Gửi lại CDK")
        context.bot.send_message.assert_awaited_once()
        sent_text = context.bot.send_message.await_args.kwargs["text"]
        self.assertIn("LOCK-AAA", sent_text)
        self.assertIn("LOCK-BBB", sent_text)

    async def test_non_completed_order_status_is_not_rechecked_or_resent(self):
        update, query = _callback_update("buy_cdk_check_42", user_id=7, chat_id=70)
        context = _context()

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot.db, "get_cdk_order", return_value={
                "id": 42,
                "user_id": 7,
                "chat_id": 70,
                "status": "canceled",
            }),
            patch.object(bot.db, "complete_cdk_order") as complete_order,
            patch.object(bot, "_complete_cdk_payment", new=AsyncMock()) as sepay_complete,
        ):
            await bot.callback_handler(update, context)

        complete_order.assert_not_called()
        sepay_complete.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])


class ActivationDonateBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_purchase_validated_cdk_activation_keeps_purchase_source_for_no_donate_photo(self):
        update, query = _callback_update("upg|uid123|alice", user_id=7, chat_id=70)
        context = _context()
        context.user_data["validated_cdk"] = {
            "code": "LOCK-PURCHASE",
            "source": "purchase",
        }

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot.db, "check_can_request", return_value=True),
            patch.object(bot, "enqueue_activation", new=AsyncMock(return_value="queued")) as enqueue,
            patch.object(bot.db, "release_cdk") as release_cdk,
        ):
            await bot.callback_handler(update, context)

        enqueue.assert_awaited_once()
        self.assertEqual(enqueue.await_args.kwargs["cdk"], "LOCK-PURCHASE")
        self.assertEqual(enqueue.await_args.kwargs["cdk_source"], "purchase")
        release_cdk.assert_not_called()
        query.answer.assert_awaited_once_with("🚀 Queue...")

    async def test_admin_activation_bypasses_cdk_and_does_not_mark_purchase_source(self):
        update, query = _callback_update("upg|uid123|alice", user_id=1, chat_id=70)
        context = _context()

        with (
            patch.object(bot, "ADMIN_ID", 1),
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot, "enqueue_activation", new=AsyncMock(return_value="queued")) as enqueue,
        ):
            await bot.callback_handler(update, context)

        enqueue.assert_awaited_once()
        self.assertNotIn("cdk", enqueue.await_args.kwargs)
        self.assertNotIn("cdk_source", enqueue.await_args.kwargs)
        query.answer.assert_awaited_once_with("🚀 Queue...")

    async def test_worker_sends_donate_photo_for_admin_cdk_but_plain_message_for_purchase_cdk(self):
        async def run_once(cdk_source):
            class StopWorker(BaseException):
                pass

            class OneItemQueue:
                def __init__(self):
                    self.task_done = Mock()

                async def get(self):
                    return {
                        "user_id": 7,
                        "uid": "uid123",
                        "username": "alice",
                        "chat_id": 70,
                        "message_id": 700,
                        "lang": "VI",
                        "cdk": "LOCK-TEST",
                        "cdk_source": cdk_source,
                    }

            app = SimpleNamespace(bot=SimpleNamespace(
                edit_message_text=AsyncMock(),
                delete_message=AsyncMock(),
                send_photo=AsyncMock(),
                send_message=AsyncMock(),
            ))
            fake_queue = OneItemQueue()
            sleeps = [None, StopWorker()]

            async def fake_sleep(_seconds):
                result = sleeps.pop(0)
                if isinstance(result, BaseException):
                    raise result
                return result

            with (
                patch.object(bot, "TOKEN_SETS", [{"fetch_token": "f", "app_transaction": "a"}]),
                patch.object(bot, "request_queue", fake_queue),
                patch.object(bot, "pending_items", []),
                patch.object(bot.db, "check_can_request", return_value=True),
                patch.object(bot.db, "lock_reserved_cdk", return_value=True),
                patch.object(bot.db, "redeem_reserved_cdk", return_value=True),
                patch.object(bot.db, "save_activation"),
                patch.object(bot.db, "increment_usage"),
                patch.object(bot.db, "log_request"),
                patch.object(bot.db, "get_config", side_effect=lambda key, default="": "donate-photo-id" if key == "donate_photo" else ""),
                patch.object(bot.locket, "inject_gold", new=AsyncMock(return_value=(True, "ok"))),
                patch.object(bot, "notify_admin_success", new=AsyncMock()),
                patch.object(bot, "create_profile_rotating", new=AsyncMock(return_value=("dns-id", "https://dns.example/profile"))),
                patch.object(bot.asyncio, "sleep", new=AsyncMock(side_effect=fake_sleep)),
                patch.object(bot, "CDK_SECRET", "secret-value-with-at-least-32-characters"),
                patch.object(bot, "DONATE_PHOTO", "fallback-photo"),
            ):
                with self.assertRaises(StopWorker):
                    await bot.queue_worker(app, worker_id=1)

            fake_queue.task_done.assert_not_called()
            return app

        admin_app = await run_once("admin")
        purchase_app = await run_once("purchase")

        admin_app.bot.send_photo.assert_awaited_once()
        admin_app.bot.send_message.assert_not_awaited()
        purchase_app.bot.send_photo.assert_not_awaited()
        purchase_app.bot.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
