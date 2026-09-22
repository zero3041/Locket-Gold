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


class KeyPlanUiTests(unittest.TestCase):
    def test_quantity_keyboard_offers_exactly_one_to_five(self):
        keyboard = bot.get_qty_keyboard("VI", "1m").inline_keyboard
        callbacks = [button.callback_data for row in keyboard for button in row]

        self.assertEqual(callbacks, [
            "buy_key_qty_1m_1",
            "buy_key_qty_1m_2",
            "buy_key_qty_1m_3",
            "buy_key_qty_1m_4",
            "buy_key_qty_1m_5",
        ])

    def test_product_label_brands_the_permanent_plan(self):
        self.assertIn("Vĩnh Viễn", bot._product_label("1m", "VI"))
        self.assertIn("1 Năm", bot._product_label("1y", "VI"))

    def test_rate_limiter_blocks_sixth_attempt_in_window(self):
        limiter = bot.RateLimiter(max_attempts=5, window_seconds=300)
        self.assertEqual([limiter.allow(7, now=100) for _ in range(5)], [True] * 5)
        self.assertFalse(limiter.allow(7, now=100))
        self.assertTrue(limiter.allow(7, now=401))


class KeyQrTests(unittest.IsolatedAsyncioTestCase):
    def test_vietqr_response_must_be_a_real_image(self):
        with self.assertRaises(bot.VietQrImageError):
            bot._validate_vietqr_image(b"invalid acqId")

    async def test_order_qr_contains_amount_content_and_safe_callbacks(self):
        telegram_bot = AsyncMock()
        order = {
            "id": 12,
            "quantity": 3,
            "total_price": 150_000,
            "payment_content": "LKABC123",
            "plan": "1y",
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
            await bot._send_key_order_qr(telegram_bot, 88, order, "VI")

        kwargs = telegram_bot.send_photo.await_args.kwargs
        qr_url = download_qr.await_args.args[0]
        self.assertIn("amount=150000", qr_url)
        self.assertIn("addInfo=LKABC123", qr_url)
        self.assertEqual("key-payment-12.png", kwargs["photo"].filename)
        callbacks = [
            button.callback_data
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(callbacks, ["key_order_check_12", "key_order_cancel_12"])


class KeyOrderCallbackGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_creation_rate_limit_blocks_before_database_write(self):
        update, query = _callback_update("buy_key_qty_1m_1", user_id=7, chat_id=70)
        context = _context()

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot, "payment_config_errors", return_value=[]),
            patch.object(bot.db, "get_active_cdk_order_for_user", return_value=None),
            patch.object(bot.payment_order_limiter, "allow", return_value=False),
            patch.object(bot.db, "create_cdk_order") as create_order,
            patch.object(bot, "_send_key_order_qr", new=AsyncMock()) as send_qr,
        ):
            await bot.callback_handler(update, context)

        create_order.assert_not_called()
        send_qr.assert_not_awaited()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_rate_limited_user_can_still_resend_existing_pending_order(self):
        update, query = _callback_update("buy_key_qty_1m_5", user_id=7, chat_id=70)
        context = _context()
        existing_order = {
            "id": 42,
            "user_id": 7,
            "chat_id": 70,
            "quantity": 1,
            "total_price": 39000,
            "payment_content": "LKEXISTING",
            "plan": "1m",
        }

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot, "payment_config_errors", return_value=[]),
            patch.object(bot.db, "get_active_cdk_order_for_user", return_value=existing_order),
            patch.object(bot.payment_order_limiter, "allow", return_value=False) as limiter,
            patch.object(bot.db, "create_cdk_order") as create_order,
            patch.object(bot, "_send_key_order_qr", new=AsyncMock()) as send_qr,
        ):
            await bot.callback_handler(update, context)

        limiter.assert_not_called()
        create_order.assert_not_called()
        send_qr.assert_awaited_once_with(context.bot, 70, existing_order, "VI")

    async def test_changing_plan_cancels_the_old_pending_order(self):
        update, query = _callback_update("buy_key_qty_1y_1", user_id=7, chat_id=70)
        context = _context()
        pending_1m = {
            "id": 41,
            "user_id": 7,
            "chat_id": 70,
            "quantity": 1,
            "total_price": 39000,
            "payment_content": "LKOLD",
            "plan": "1m",
        }
        new_order = {
            "id": 42,
            "user_id": 7,
            "chat_id": 70,
            "quantity": 1,
            "total_price": 50000,
            "payment_content": "LKNEW",
            "plan": "1y",
        }

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot, "payment_config_errors", return_value=[]),
            patch.object(bot.db, "get_active_cdk_order_for_user", return_value=pending_1m),
            patch.object(bot.payment_order_limiter, "allow", return_value=True),
            patch.object(bot, "_gen_payment_content", return_value="LKNEW"),
            patch.object(bot.db, "cancel_cdk_order", return_value=True) as cancel,
            patch.object(bot.db, "create_cdk_order", return_value=new_order) as create_order,
            patch.object(bot, "_send_key_order_qr", new=AsyncMock()) as send_qr,
        ):
            await bot.callback_handler(update, context)

        cancel.assert_called_once_with(41, user_id=7)
        create_order.assert_called_once()
        self.assertEqual("1y", create_order.call_args.kwargs["plan"])
        send_qr.assert_awaited_once_with(context.bot, 70, new_order, "VI")

    async def test_qr_generation_failure_cancels_order_and_tells_user(self):
        update, query = _callback_update("buy_key_qty_1m_1", user_id=7, chat_id=70)
        context = _context()
        order = {
            "id": 42,
            "user_id": 7,
            "chat_id": 70,
            "quantity": 1,
            "total_price": 39000,
            "payment_content": "LKABC123",
            "plan": "1m",
        }

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot, "payment_config_errors", return_value=[]),
            patch.object(bot, "CDK_UNIT_PRICE", 39000),
            patch.object(bot.db, "get_active_cdk_order_for_user", return_value=None),
            patch.object(bot, "payment_order_limiter") as limiter,
            patch.object(bot, "_gen_payment_content", return_value="LKABC123"),
            patch.object(bot.db, "create_cdk_order", return_value=order),
            patch.object(
                bot,
                "_send_key_order_qr",
                new=AsyncMock(side_effect=bot.VietQrImageError("invalid VietQR image")),
            ),
            patch.object(bot.db, "cancel_cdk_order", return_value=True) as cancel,
        ):
            limiter.allow.return_value = True
            await bot.callback_handler(update, context)

        cancel.assert_called_once_with(42, user_id=7, chat_id=70)
        query.message.reply_text.assert_awaited_once()
        self.assertIn("không tạo được mã QR", query.message.reply_text.await_args.args[0])

    async def test_existing_order_from_another_chat_is_not_disclosed(self):
        update, query = _callback_update("buy_key_qty_1m_1", user_id=7, chat_id=70)
        context = _context()
        existing_order = {
            "id": 42,
            "user_id": 7,
            "chat_id": 999,
            "quantity": 1,
            "total_price": 39000,
            "payment_content": "LKEXISTING",
            "plan": "1m",
        }

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot, "payment_config_errors", return_value=[]),
            patch.object(bot, "CDK_UNIT_PRICE", 39000),
            patch.object(bot.db, "get_active_cdk_order_for_user", return_value=existing_order),
            patch.object(bot.payment_order_limiter.allow.__self__, "allow", return_value=True),
            patch.object(bot, "_gen_payment_content", return_value="LKNEW123"),
            patch.object(bot.db, "create_cdk_order") as create_order,
            patch.object(bot, "_send_key_order_qr", new=AsyncMock()) as send_qr,
        ):
            await bot.callback_handler(update, context)

        send_qr.assert_not_awaited()
        create_order.assert_not_called()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_buy_key_quantity_fails_closed_when_payment_config_is_invalid(self):
        update, query = _callback_update("buy_key_qty_1m_2", user_id=7, chat_id=70)
        context = _context()

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot, "payment_config_errors", return_value=["SEPAY_API_TOKEN"]),
            patch.object(bot.db, "create_cdk_order") as create_order,
            patch.object(bot, "_send_key_order_qr", new=AsyncMock()) as send_qr,
        ):
            await bot.callback_handler(update, context)

        create_order.assert_not_called()
        send_qr.assert_not_awaited()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertIn("Thanh toán", query.answer.await_args.args[0])

    async def test_check_rejects_order_that_belongs_to_another_user(self):
        update, query = _callback_update("key_order_check_42", user_id=7, chat_id=70)
        context = _context()

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot.db, "get_cdk_order", return_value={
                "id": 42,
                "user_id": 999,
                "chat_id": 70,
                "status": "pending",
            }),
            patch.object(bot, "_complete_key_payment", new=AsyncMock()) as complete,
        ):
            await bot.callback_handler(update, context)

        complete.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertIn("Đơn không tồn tại", query.answer.await_args.args[0])

    async def test_manual_payment_check_rate_limit_blocks_sepay_request(self):
        update, query = _callback_update("key_order_check_42", user_id=7, chat_id=70)
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
            patch.object(bot, "_complete_key_payment", new=AsyncMock()) as complete,
        ):
            await bot.callback_handler(update, context)

        complete.assert_not_awaited()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])


class KeyOrderSepayBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_sepay_budget_blocks_external_request(self):
        application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        order = {"id": 9, "payment_content": "LK9", "total_price": 39000, "plan": "1m"}

        with (
            patch.object(bot.sepay_global_limiter, "allow", return_value=False),
            patch.object(bot.sepay, "SePayClient") as client,
        ):
            result = await bot._complete_key_payment(application, order, "VI")

        self.assertFalse(result)
        client.assert_not_called()

    async def test_cancel_rejects_order_that_does_not_match_user_and_chat(self):
        update, query = _callback_update("key_order_cancel_42", user_id=7, chat_id=70)
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

    async def test_completed_order_check_resends_existing_keys_without_sepay_recheck(self):
        update, query = _callback_update("key_order_check_42", user_id=7, chat_id=70)
        context = _context()
        completed_order = {
            "id": 42,
            "user_id": 7,
            "chat_id": 70,
            "status": "completed",
            "transaction_id": "txn-42",
            "matched_amount": 100000,
            "plan": "1y",
        }

        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot.db, "get_cdk_order", return_value=completed_order),
            patch.object(bot.db, "complete_cdk_order", return_value=["LOCK-AAA", "LOCK-BBB"]) as complete_order,
            patch.object(bot, "_complete_key_payment", new=AsyncMock()) as sepay_complete,
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
        query.answer.assert_awaited_once()
        context.bot.send_message.assert_awaited_once()
        sent_text = context.bot.send_message.await_args.kwargs["text"]
        self.assertIn("LOCK-AAA", sent_text)
        self.assertIn("LOCK-BBB", sent_text)

    async def test_non_completed_order_status_is_not_rechecked_or_resent(self):
        update, query = _callback_update("key_order_check_42", user_id=7, chat_id=70)
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
            patch.object(bot, "_complete_key_payment", new=AsyncMock()) as sepay_complete,
        ):
            await bot.callback_handler(update, context)

        complete_order.assert_not_called()
        sepay_complete.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])


class RedeemFlowTests(unittest.IsolatedAsyncioTestCase):
    def _redeem_update(self, key="LOCK-KEY", target="alice", user_id=7, chat_id=70, args=None):
        message = SimpleNamespace(
            text=f"/redeem {key} {target}",
            reply_text=AsyncMock(),
        )
        message.reply_text.return_value = SimpleNamespace(
            edit_text=AsyncMock(),
            delete=AsyncMock(),
        )
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=chat_id),
            message=message,
        ), SimpleNamespace(
            args=args if args is not None else [key, target],
            bot=SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock()),
            application=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())),
            user_data={},
        )

    async def test_invalid_key_does_not_call_the_activation_engine(self):
        update, context = self._redeem_update()
        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot.db, "consume_key", return_value=(False, "not_found", None, 0, None)),
            patch.object(bot.activation, "activate", new=AsyncMock()) as activate,
        ):
            await bot.cmd_redeem(update, context)
        activate.assert_not_awaited()
        status = update.message.reply_text.return_value
        self.assertIn("không tồn tại", status.edit_text.await_args.args[0])

    async def test_failed_activation_refunds_the_key_spin(self):
        update, context = self._redeem_update()
        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot.db, "consume_key", return_value=(True, "ok", "1m", 2, "purchase")),
            patch.object(bot.activation, "activate", new=AsyncMock(return_value={
                "ok": False, "code": "no_source", "message": "empty",
            })) as activate,
            patch.object(bot.db, "refund_key_spin") as refund,
        ):
            await bot.cmd_redeem(update, context)
        activate.assert_awaited_once()
        refund.assert_called_once()
        status = update.message.reply_text.return_value
        self.assertIn("Kho nguồn", status.edit_text.await_args.args[0])

    async def test_successful_activation_logs_history_and_notifies_admin(self):
        update, context = self._redeem_update()
        result = {
            "ok": True, "code": "ok", "message": "done",
            "uid": "U" * 28, "expires": "2027-01-01 00:00:00", "days_left": 100,
            "source": "sourceuser", "source_used": 2, "source_slots_left": 3,
        }
        with (
            patch.object(bot.db, "get_lang", return_value="VI"),
            patch.object(bot.db, "consume_key", return_value=(True, "ok", "1y", 0, "purchase")),
            patch.object(bot.activation, "activate", new=AsyncMock(return_value=result)),
            patch.object(bot.db, "save_activation") as save_activation,
            patch.object(bot.db, "mark_uid_activated", return_value=True) as mark_activated,
            patch.object(bot.db, "log_key_redemption") as log,
            patch.object(bot, "notify_admin_success", new=AsyncMock()) as notify,
        ):
            await bot.cmd_redeem(update, context)
        save_activation.assert_called_once()
        mark_activated.assert_called_once_with("U" * 28)
        log.assert_called_once()
        notify.assert_awaited_once()
        status = update.message.reply_text.return_value
        self.assertIn("THÀNH CÔNG", status.edit_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
