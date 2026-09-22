"""Locket Gold bot — key-based activation on top of the alias engine.

Activation model:
  * Users buy keys with /nap (VietQR / SePay, 1-month or 1-year plan).
  * Activation happens with /redeem <key> <locket_link>, which reserves a
    source from the shared pool and aliases its Gold onto the destination.
  * /check and /chk inspect accounts; /scan harvests links from TikTok/Threads
    and auto-fills the source pool with eligible accounts.
"""

import asyncio
import html
import logging
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime
from io import BytesIO

import aiohttp
from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import database as db
from app.config import *  # noqa: F401,F403 — shared config/texts/prices
from app.services import activation, locket, scan_locket, sepay

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AVATAR_DIR = os.path.join(BASE_DIR, "avatars")
SOURCE_FILE = os.path.join(BASE_DIR, "current_source.txt")

processing_key_orders = set()

# Concurrency guards
activation_slots = asyncio.Semaphore(2)
check_slots = asyncio.Semaphore(CHECK_MAX_CONCURRENT)
scan_slots = asyncio.Semaphore(SCAN_MAX_CONCURRENT)
_user_locks = defaultdict(asyncio.Lock)


class RateLimiter:
    def __init__(self, max_attempts=5, window_seconds=300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts = {}

    def allow(self, user_id, now=None):
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        recent = tuple(ts for ts in self._attempts.get(user_id, ()) if ts > cutoff)
        if len(recent) >= self.max_attempts:
            self._attempts = {**self._attempts, user_id: recent}
            return False
        self._attempts = {**self._attempts, user_id: (*recent, current)}
        return True


payment_order_limiter = RateLimiter(
    max_attempts=CDK_ORDER_CREATE_MAX,
    window_seconds=CDK_ORDER_CREATE_WINDOW_SECONDS,
)
payment_manual_check_limiter = RateLimiter(
    max_attempts=CDK_MANUAL_CHECK_MAX,
    window_seconds=CDK_MANUAL_CHECK_WINDOW_SECONDS,
)
sepay_global_limiter = RateLimiter(
    max_attempts=SEPAY_GLOBAL_CHECK_MAX,
    window_seconds=SEPAY_GLOBAL_CHECK_WINDOW_SECONDS,
)


def _msg(lang, vi, en):
    return vi if lang == "VI" else en


def _esc(value):
    return html.escape(str(value or ""))


def format_vnd(amount):
    return f"{int(amount):,}".replace(",", ".") + " VND"


def _days_suffix(lang, days):
    if days and days > 0:
        return _msg(lang, f" (còn {days} ngày)", f" ({days} days left)")
    return ""


def _user_display(username):
    text = str(username or "")
    return text if text.startswith("http") else f"@{text}"


def _gen_payment_content():
    return "LK" + secrets.token_hex(8).upper()


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

REPLY_ACTIONS = {}


def _build_reply_actions():
    for _lang in ("VI", "EN"):
        REPLY_ACTIONS[T("btn_input", _lang)] = "menu_input"
        REPLY_ACTIONS[T("btn_redeem", _lang)] = "menu_redeem"
        REPLY_ACTIONS[T("btn_buy_key", _lang)] = "buy_key"
        REPLY_ACTIONS[T("btn_scan", _lang)] = "menu_scan"
        REPLY_ACTIONS[T("btn_account", _lang)] = "menu_account"
        REPLY_ACTIONS[T("btn_guide", _lang)] = "menu_guide"
        REPLY_ACTIONS[T("btn_lang", _lang)] = "menu_lang"
        REPLY_ACTIONS[T("btn_help", _lang)] = "menu_help"
        REPLY_ACTIONS[T("btn_cdk_admin", _lang)] = "menu_genkey"


_build_reply_actions()


def get_main_menu_keyboard(lang, user_id=None):
    rows = [
        [InlineKeyboardButton(T("btn_input", lang), callback_data="menu_input")],
        [InlineKeyboardButton(T("btn_redeem", lang), callback_data="menu_redeem")],
        [InlineKeyboardButton(T("btn_buy_key", lang), callback_data="buy_key")],
        [InlineKeyboardButton(T("btn_scan", lang), callback_data="menu_scan")],
        [InlineKeyboardButton(T("btn_account", lang), callback_data="menu_account")],
        [InlineKeyboardButton(T("btn_guide", lang), callback_data="menu_guide")],
        [
            InlineKeyboardButton(T("btn_lang", lang), callback_data="menu_lang"),
            InlineKeyboardButton(T("btn_help", lang), callback_data="menu_help"),
        ],
    ]
    if user_id == ADMIN_ID:
        rows.insert(4, [InlineKeyboardButton(T("btn_cdk_admin", lang), callback_data="menu_genkey")])
    return InlineKeyboardMarkup(rows)


def get_reply_keyboard(lang, user_id=None):
    rows = [
        [KeyboardButton(T("btn_input", lang)), KeyboardButton(T("btn_redeem", lang))],
        [KeyboardButton(T("btn_buy_key", lang)), KeyboardButton(T("btn_account", lang))],
        [KeyboardButton(T("btn_scan", lang))],
        [KeyboardButton(T("btn_guide", lang)), KeyboardButton(T("btn_lang", lang))],
        [KeyboardButton(T("btn_help", lang))],
    ]
    if user_id == ADMIN_ID:
        rows.append([KeyboardButton(T("btn_cdk_admin", lang))])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="🔎 Nhập Username Locket...",
    )


def _product_label(plan, lang):
    """Customer-facing name of the sold product (single permanent plan)."""
    return plan_label(plan, lang) if (plan or "").lower() == "1y" else T("product_name", lang)


def get_qty_keyboard(lang, plan):
    price = price_for_plan(plan)
    rows = []
    for start in (1, 3, 5):
        row = []
        for quantity in range(start, min(start + 2, 6)):
            label = f"{quantity} key"
            total = price * quantity
            if total > 0:
                label += f" • {format_vnd(total)}"
            row.append(InlineKeyboardButton(label, callback_data=f"buy_key_qty_{plan}_{quantity}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def get_back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    await update.message.reply_text(
        T("welcome", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard(lang, user_id),
    )
    await update.message.reply_text(
        T("menu_msg", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=get_reply_keyboard(lang, user_id),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    await update.message.reply_text(
        T("menu_msg", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard(lang, user_id),
    )
    await update.message.reply_text("⌨️", reply_markup=get_reply_keyboard(lang, user_id))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    text = T("help_msg", lang)
    if user_id == ADMIN_ID:
        text += T("admin_help", lang)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def setlang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_language_select(update)


async def show_language_select(update: Update):
    keyboard = [
        [InlineKeyboardButton("Tiếng Việt 🇻🇳", callback_data="setlang_VI")],
        [InlineKeyboardButton("English 🇺🇸", callback_data="setlang_EN")],
    ]
    text = T("lang_select", "EN")
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# ---------------------------------------------------------------------------
# Account info (/sodu)
# ---------------------------------------------------------------------------

async def cmd_sodu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    await _send_account(update.message, user_id, lang)


async def _send_account(target, user_id, lang):
    stats = db.user_key_stats(user_id)
    unused = stats["unused"]
    admin_tag = _msg(lang, " (Admin)", " (Admin)") if user_id == ADMIN_ID else ""
    text = T("account_info", lang).format(
        user_id=user_id,
        admin_tag=admin_tag,
        unused=stats["unused_total"],
        unused_1m=unused.get("1m", 0),
        unused_1y=unused.get("1y", 0),
        orders=stats["orders"],
        spent=f"{stats['spent']:,}".replace(",", "."),
        redeemed=stats["redeemed"],
    )

    keys = db.list_user_keys(user_id, limit=5)
    if keys:
        lines = []
        for key in keys:
            lines.append(
                f"• <code>{_esc(key.get('code') or key['code_hash'][:12])}</code> — "
                f"{plan_label(key['plan'], lang)} ({key['spins_left']}/{key['spins']})"
            )
        text += "\n\n" + T("account_history", lang) + "\n" + "\n".join(lines)
    history = db.list_key_redemptions(user_id=user_id, limit=5)
    if history:
        lines = []
        for row in history:
            stamp = datetime.fromtimestamp(row["created_at"] or 0).strftime("%d/%m %H:%M")
            lines.append(
                T("account_history_line", lang).format(
                    time=stamp,
                    target=_esc(row["target"]),
                    plan=plan_label(row["plan"], lang),
                )
            )
        text += "\n" + "\n".join(lines)
    elif not keys:
        text += "\n" + T("account_no_history", lang)

    await target.reply_text(text, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Payment: buy keys (/nap, /muacdk)
# ---------------------------------------------------------------------------

class VietQrImageError(RuntimeError):
    pass


def _validate_vietqr_image(body):
    if not isinstance(body, bytes) or len(body) < 100:
        raise VietQrImageError("VietQR did not return an image")
    if len(body) > 5 * 1024 * 1024:
        raise VietQrImageError("VietQR image is unexpectedly large")
    is_png = body.startswith(b"\x89PNG\r\n\x1a\n")
    is_jpeg = body.startswith(b"\xff\xd8\xff")
    if not (is_png or is_jpeg):
        raise VietQrImageError("VietQR returned invalid image data")
    return body


async def _download_vietqr_image(qr_url):
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(qr_url) as response:
                if response.status < 200 or response.status >= 300:
                    raise VietQrImageError(f"VietQR HTTP {response.status}")
                return _validate_vietqr_image(await response.read())
    except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
        raise VietQrImageError("Could not download VietQR image") from exc


async def _send_key_order_qr(bot, chat_id, order, lang):
    plan = order.get("plan") or "1m"
    qr_url = sepay.build_vietqr_url(
        BANK_BIN,
        BANK_ACCOUNT,
        order["total_price"],
        order["payment_content"],
        account_name=BANK_OWNER,
    )
    caption = (
        f"💳 <b>{_msg(lang, 'THANH TOÁN KEY', 'KEY PAYMENT')} #{order['id']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 {_msg(lang, 'Gói', 'Plan')}: <b>{_product_label(plan, lang)}</b>\n"
        f"🎟️ {_msg(lang, 'Số lượng', 'Quantity')}: <b>{order['quantity']}</b>\n"
        f"💰 {_msg(lang, 'Tổng tiền', 'Total')}: <b>{format_vnd(order['total_price'])}</b>\n\n"
        f"🏦 {_esc(BANK_NAME)}\n"
        f"👤 {_esc(BANK_OWNER)}\n"
        f"💳 <code>{_esc(BANK_ACCOUNT)}</code>\n"
        f"📝 <code>{_esc(order['payment_content'])}</code>\n\n"
        f"⚠️ {_msg(lang, 'Chuyển đúng số tiền và nội dung. Đơn hết hạn sau', 'Transfer the exact amount and note. Order expires in')} "
        f"{CDK_ORDER_TIMEOUT_MINUTES} {_msg(lang, 'phút.', 'minutes.')}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(_msg(lang, "🔄 Kiểm tra thanh toán", "🔄 Check payment"), callback_data=f"key_order_check_{order['id']}")],
        [InlineKeyboardButton(_msg(lang, "❌ Hủy đơn", "❌ Cancel order"), callback_data=f"key_order_cancel_{order['id']}")],
    ])
    qr_image = await _download_vietqr_image(qr_url)
    await bot.send_photo(
        chat_id=chat_id,
        photo=InputFile(BytesIO(qr_image), filename=f"key-payment-{order['id']}.png"),
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


def _paid_message(lang, codes, plan):
    return T("buy_key_paid", lang).format(
        codes="\n".join(codes),
        code=codes[0] if codes else "",
        plan=_product_label(plan, lang),
    )


async def _complete_key_payment(application, order, lang, manual_query=None):
    order_id = order["id"]
    if order_id in processing_key_orders:
        if manual_query:
            await manual_query.answer(_msg(lang, "⏳ Đơn đang được xử lý", "⏳ Order is being processed"), show_alert=True)
        return False

    if not sepay_global_limiter.allow("global"):
        if manual_query:
            await manual_query.answer(
                _msg(lang, "⏳ Hệ thống đang kiểm tra nhiều giao dịch, vui lòng thử lại sau.",
                     "⏳ Too many payment checks right now, please retry shortly."),
                show_alert=True,
            )
        return False

    processing_key_orders.add(order_id)
    try:
        async with sepay.SePayClient(SEPAY_API_TOKEN, base_url=SEPAY_API_URL) as client:
            transaction = await client.find_matching_transaction(
                order["payment_content"],
                order["total_price"],
                account_number=BANK_ACCOUNT,
            )
        if not transaction:
            if manual_query:
                await manual_query.answer(T("buy_key_waiting", lang), show_alert=True)
            return False

        codes = db.complete_cdk_order(
            order_id=order_id,
            transaction_id=transaction["id"],
            matched_amount=transaction["amount_in"],
            secret=CDK_SECRET,
        )
        if not codes:
            logger.error("Key order completion rejected: order_id=%s transaction_id=%s", order_id, transaction["id"])
            if manual_query:
                await manual_query.answer(_msg(lang, "⚠️ Giao dịch cần admin kiểm tra", "⚠️ Transaction needs admin review"), show_alert=True)
            return False

        plan = order.get("plan") or "1m"
        if manual_query:
            await manual_query.answer(_msg(lang, "✅ Thanh toán thành công", "✅ Payment confirmed"))
        await application.bot.send_message(
            chat_id=order["chat_id"],
            text=_paid_message(lang, codes, plan),
            parse_mode=ParseMode.HTML,
        )
        try:
            await application.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"💰 <b>KEY ORDER PAID</b>\n"
                    f"Order: <code>#{order_id}</code>\n"
                    f"User: <code>{order['user_id']}</code>\n"
                    f"Plan: {plan_label(plan, 'EN')}\n"
                    f"Quantity: {order['quantity']}\n"
                    f"Amount: {format_vnd(transaction['amount_in'])}\n"
                    f"SePay: <code>{_esc(transaction['id'])}</code>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.warning("Could not notify admin for key order %s: %s", order_id, exc)
        return True
    except sepay.SePayError as exc:
        logger.warning("SePay check failed for order %s: %s", order_id, exc)
        if manual_query:
            await manual_query.answer(_msg(lang, "⚠️ SePay tạm thời không phản hồi", "⚠️ SePay is not responding"), show_alert=True)
        return False
    finally:
        processing_key_orders.discard(order_id)


async def key_payment_poller(application):
    while True:
        try:
            expired = db.expire_cdk_orders()
            for order in expired:
                if not order.get("chat_id"):
                    continue
                try:
                    await application.bot.send_message(
                        chat_id=order["chat_id"],
                        text=_msg(DEFAULT_LANG, f"⌛ Đơn mua key #{order['id']} đã hết hạn.",
                                  f"⌛ Key order #{order['id']} expired."),
                    )
                except Exception:
                    pass
            for order in db.get_pending_cdk_orders()[:SEPAY_MAX_ORDERS_PER_POLL]:
                if not order.get("chat_id"):
                    continue
                lang = db.get_lang(order["user_id"]) or DEFAULT_LANG
                await _complete_key_payment(application, order, lang)
        except Exception as exc:
            logger.error("Key payment poller error: %s", exc)
        await asyncio.sleep(SEPAY_POLL_INTERVAL_SECONDS)




async def cmd_nap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    if payment_config_errors():
        await update.message.reply_text(T("buy_key_config_error", lang), parse_mode=ParseMode.HTML)
        return
    # Single product catalog: the permanent plan (internally the 1m source tier).
    await update.message.reply_text(
        f"🛒 {T('btn_buy_key', lang)}\n\n"
        f"{T('buy_key_prompt_qty', lang)}",
        parse_mode=ParseMode.HTML,
        reply_markup=get_qty_keyboard(lang, "1m"),
    )


async def cmd_muacdk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Backward-compatible alias of /nap."""
    await cmd_nap(update, context)


# ---------------------------------------------------------------------------
# Redeem (/redeem)
# ---------------------------------------------------------------------------

def _split_redeem_args(args):
    if not args:
        return None, None
    if len(args) >= 2:
        return args[0].strip(), args[1].strip()
    # Tolerate "KEY link" pasted as one token separated by newline/comma.
    token = args[0].replace(",", " ").strip()
    parts = token.split()
    if len(parts) >= 2:
        return parts[0], parts[1]
    return parts[0], None


async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    key_code, target = _split_redeem_args(context.args)
    if not key_code or not target:
        await update.message.reply_text(T("redeem_usage", lang), parse_mode=ParseMode.HTML)
        return

    user_lock = _user_locks[user_id]
    if user_lock.locked():
        await update.message.reply_text(
            _msg(lang, "⏳ Yêu cầu trước của bạn đang xử lý, vui lòng đợi.",
                 "⏳ Your previous request is still running, please wait."),
            parse_mode=ParseMode.HTML,
        )
        return

    async with user_lock:
        status_msg = await update.message.reply_text(T("redeem_checking", lang), parse_mode=ParseMode.HTML)

        ok, reason, plan, left, key_source = db.consume_key(key_code, user_id, secret=CDK_SECRET)
        if not ok:
            text = T("redeem_invalid", lang) if reason == "not_found" else T("redeem_exhausted", lang)
            await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
            return

        key_display = key_code.strip().upper()

        async def progress(message):
            try:
                await status_msg.edit_text(f"⏳ {_esc(message)}", parse_mode=ParseMode.HTML)
            except Exception:
                pass

        async with activation_slots:
            result = await activation.activate(target, plan=plan, log=progress)

        if not result["ok"]:
            db.refund_key_spin(key_code, secret=CDK_SECRET)
            code = result.get("code")
            if code == "already_gold":
                text = T("redeem_already_gold", lang).format(
                    user=_esc(_user_display(target)),
                    days=result.get("days_left", 0),
                    expires=_esc(result.get("expires", "")),
                )
            elif code == "no_source":
                text = T("redeem_no_source", lang)
            elif "alias limit" in (result.get("message") or "").lower():
                text = T("redeem_alias_limit", lang)
            elif code in ("ip_blocked", "proxy_error"):
                text = T("redeem_ip_blocked", lang)
            else:
                text = T("redeem_failed", lang).format(error=_esc(result.get("message", "unknown")))
            await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
            return

        db.save_activation(user_id, result["uid"], target)
        db.mark_uid_activated(result["uid"])
        db.log_key_redemption(
            key_code.strip().upper(), user_id, target, result["uid"], plan,
            status="success", detail=f"source={result.get('source')}",
        )
        text = T("redeem_success", lang).format(
            user=_esc(_user_display(target)),
            uid=_esc(result["uid"]),
            expires=_esc(result.get("expires", "Unknown")),
            days=_days_suffix(lang, result.get("days_left", 0)),
            key=_esc(key_display),
            left=left,
        )
        donate_photo = db.get_config("donate_photo", DONATE_PHOTO) if key_source == "admin" else ""
        if donate_photo:
            try:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=donate_photo,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                )
                await status_msg.delete()
            except Exception:
                await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
        else:
            await status_msg.edit_text(text, parse_mode=ParseMode.HTML)

        await notify_admin_success(
            context.application, user_id, target, result["uid"], plan,
            result.get("source"), key_display, left,
        )


# ---------------------------------------------------------------------------
# Check (/check) and text handler
# ---------------------------------------------------------------------------

def _looks_like_locket(text):
    lowered = (text or "").lower()
    return "locket.cam" in lowered or "locket.camera" in lowered or "links/" in lowered


async def _check_account(username, proxy_url=None):
    """Returns (uid, status_dict, error_code)."""
    uid = await locket.resolve_uid(username, proxy_url=proxy_url)
    if uid in ("IP_BLOCKED", "PROXY_ERROR") or not uid:
        return uid, None, uid or "not_found"
    status = await locket.check_status(uid, proxy_url=proxy_url)
    return uid, status, status.get("error")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    if not context.args:
        await update.message.reply_text(T("check_usage", lang), parse_mode=ParseMode.HTML)
        return
    await _do_check(update, context, context.args[0], lang)


async def _do_check(update, context, raw, lang):
    status_msg = await update.effective_message.reply_text(T("resolving", lang), parse_mode=ParseMode.HTML)
    async with check_slots:
        uid, status, error = await _check_account(raw)
    if error in ("IP_BLOCKED",):
        await status_msg.edit_text(T("redeem_ip_blocked", lang), parse_mode=ParseMode.HTML)
        return
    if not uid or error == "not_found":
        await status_msg.edit_text(T("not_found", lang), parse_mode=ParseMode.HTML)
        return
    if error:
        await status_msg.edit_text(T("redeem_failed", lang).format(error=_esc(error)), parse_mode=ParseMode.HTML)
        return

    active = bool(status and status.get("active"))
    expires = (status or {}).get("expires", "Unknown")
    days = locket.plan_days_left(expires) if active else 0

    # Harvest: a healthy account found by /check enriches the shared source pool.
    source_note = ""
    if active and days >= GOLD_MIN_SOURCE_DAYS:
        existed = db.gold_source_exists(raw)
        expires_text = f"expires: {expires} (còn {days} ngày)"
        if db.add_gold_source(raw, uid=uid, expires=expires_text, min_days=GOLD_MIN_SOURCE_DAYS):
            source_note = "\n" + T("check_source_exists" if existed else "check_source_added", lang)

    text = T("check_result", lang).format(
        user=_esc(_user_display(raw)),
        status=(T("gold_active", lang).format(expires) if active else T("check_inactive", lang)),
        expires=_esc(expires if active else "-"),
        days=_days_suffix(lang, days),
    ) + source_note
    hint = raw.encode("utf-8")[:40].decode("utf-8", "ignore")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(T("btn_redeem", lang), callback_data=f"redeem_hint|{hint}")
    ]])
    await status_msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    text = (update.message.text or "").strip()

    action = REPLY_ACTIONS.get(text)
    if action:
        await run_menu_action(action, update, context, lang)
        return

    reply = update.message.reply_to_message
    is_reply_to_bot = bool(reply and reply.from_user and reply.from_user.is_bot)
    awaiting = context.user_data.pop("awaiting_username", False)
    if not (awaiting or is_reply_to_bot or _looks_like_locket(text)):
        return
    await _do_check(update, context, text, lang)


# ---------------------------------------------------------------------------
# Bulk check (/chk) + scan (/scan) shared classification
# ---------------------------------------------------------------------------

class BulkResult:
    def __init__(self):
        self.eligible = []
        self.expired = []
        self.no_gold = []
        self.not_found = []
        self.error = None

    @property
    def checked(self):
        return len(self.eligible) + len(self.expired) + len(self.no_gold) + len(self.not_found)


async def _classify_links(links, status_msg, lang, proxy_url=None, progress_label="⏳"):
    result = BulkResult()
    total = len(links)
    last_update = time.time()
    for idx, raw in enumerate(links, 1):
        uid, status, error = await _check_account(raw, proxy_url=proxy_url)
        if error == "IP_BLOCKED":
            result.error = "ip_blocked"
            break
        if not uid:
            result.not_found.append(raw)
        elif error:
            result.not_found.append(raw)
        elif status and status.get("active"):
            expires = status.get("expires", "Unknown")
            days = locket.plan_days_left(expires)
            if days >= GOLD_MIN_SOURCE_DAYS:
                result.eligible.append((raw, expires, days))
            else:
                result.expired.append((raw, expires, days))
        else:
            result.no_gold.append(raw)

        if time.time() - last_update >= 3 or idx == total:
            last_update = time.time()
            try:
                await status_msg.edit_text(
                    T("chk_progress", lang).format(
                        done=idx, total=total,
                        eligible=len(result.eligible),
                        other=len(result.expired) + len(result.no_gold) + len(result.not_found),
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
    return result


def _report_text(lang, file_name, result):
    added = 0
    if result.eligible:
        added = db.add_gold_source_many(result.eligible, min_days=GOLD_MIN_SOURCE_DAYS)
    added_note = T("chk_added_note", lang).format(n=added) if result.eligible else ""
    return T("chk_result", lang).format(
        file=_esc(file_name),
        total=result.checked,
        min_days=GOLD_MIN_SOURCE_DAYS,
        eligible=len(result.eligible),
        expired=len(result.expired),
        no_gold=len(result.no_gold),
        not_found=len(result.not_found),
        added_note=added_note,
    ), added


def _report_file(file_name, result, source_label=None):
    lines = [
        "# KẾT QUẢ KIỂM TRA LOCKET GOLD",
        f"# File: {file_name}",
    ]
    if source_label:
        lines.append(f"# Nguồn: {source_label}")
    lines += [
        f"# Tổng kiểm tra: {result.checked}",
        f"# Đủ điều kiện (>= {GOLD_MIN_SOURCE_DAYS} ngày): {len(result.eligible)}",
        f"# Hết hạn / dưới {GOLD_MIN_SOURCE_DAYS} ngày: {len(result.expired)}",
        f"# Chưa có Gold: {len(result.no_gold)}",
        f"# Không tìm thấy: {len(result.not_found)}",
        "=" * 60,
        "",
        "[1. ĐỦ ĐIỀU KIỆN (đã thêm vào kho nguồn)]:",
    ]
    for username, expires, days in result.eligible:
        lines.append(f"{_user_display(username)} | ACTIVE | expires: {expires} (còn {days} ngày)")
    lines += ["", "=" * 60, "[2. HẾT HẠN / DƯỚI NGƯỠNG]:"]
    for username, expires, days in result.expired:
        lines.append(f"{_user_display(username)} | EXPIRED/LOW | expires: {expires} (còn {days} ngày)")
    lines += ["", "=" * 60, "[3. CHƯA CÓ GOLD]:"]
    for username in result.no_gold:
        lines.append(_user_display(username))
    lines += ["", "=" * 60, "[4. KHÔNG TÌM THẤY / LỖI LINK]:"]
    for username in result.not_found:
        lines.append(_user_display(username))
    return "\n".join(lines) + "\n"


async def cmd_chk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    message = update.message
    document = None
    if message.document:
        document = message.document
    elif message.reply_to_message and message.reply_to_message.document:
        document = message.reply_to_message.document

    if not document:
        await message.reply_text(T("chk_usage", lang), parse_mode=ParseMode.HTML)
        return

    file_name = document.file_name or "links.txt"
    if not file_name.lower().endswith(".txt"):
        await message.reply_text(T("chk_not_txt", lang), parse_mode=ParseMode.HTML)
        return

    status_msg = await message.reply_text(T("chk_downloading", lang), parse_mode=ParseMode.HTML)
    try:
        tg_file = await document.get_file()
        data = await tg_file.download_as_bytearray()
        content = data.decode("utf-8", errors="ignore")
    except Exception as exc:
        await status_msg.edit_text(f"❌ {_esc(exc)}", parse_mode=ParseMode.HTML)
        return

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        await status_msg.edit_text(T("chk_empty", lang), parse_mode=ParseMode.HTML)
        return
    if len(lines) > CHK_MAX_LINES:
        await status_msg.edit_text(T("chk_too_many", lang).format(max=CHK_MAX_LINES), parse_mode=ParseMode.HTML)
        return

    async with check_slots:
        result = await _classify_links(lines, status_msg, lang, proxy_url=CHK_PROXY_URL)
    if result.error == "ip_blocked":
        await status_msg.edit_text(T("chk_ip_blocked", lang), parse_mode=ParseMode.HTML)
        return

    text, _added = _report_text(lang, file_name, result)
    await status_msg.edit_text(text, parse_mode=ParseMode.HTML)

    report = _report_file(file_name, result)
    buffer = BytesIO(report.encode("utf-8"))
    buffer.name = f"ket_qua_check_{file_name}"
    try:
        await message.reply_document(document=buffer, caption=f"📄 {file_name}")
    except Exception as exc:
        logger.error("Could not send check report: %s", exc)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    if not context.args:
        await update.message.reply_text(T("scan_usage", lang), parse_mode=ParseMode.HTML)
        return

    target_url = context.args[0].strip()
    status_msg = await update.message.reply_text(T("scan_running", lang), parse_mode=ParseMode.HTML)

    async with scan_slots:
        links, error = await asyncio.to_thread(
            scan_locket.scrape_comments_auto, target_url,
            max_comments=SCAN_MAX_COMMENTS, save_file=False,
        )
    if error:
        await status_msg.edit_text(f"❌ {_esc(error)}", parse_mode=ParseMode.HTML)
        return
    if not links:
        await status_msg.edit_text(T("scan_no_links", lang), parse_mode=ParseMode.HTML)
        return

    source_label = scan_locket.extract_target_id(target_url)
    async with check_slots:
        result = await _classify_links(links, status_msg, lang, proxy_url=CHK_PROXY_URL)
    if result.error == "ip_blocked":
        await status_msg.edit_text(T("chk_ip_blocked", lang), parse_mode=ParseMode.HTML)
        return

    added = db.add_gold_source_many(result.eligible, min_days=GOLD_MIN_SOURCE_DAYS) if result.eligible else 0
    added_note = T("chk_added_note", lang).format(n=added) if result.eligible else ""
    text = T("scan_result", lang).format(
        source=_esc(source_label),
        total=result.checked,
        min_days=GOLD_MIN_SOURCE_DAYS,
        eligible=len(result.eligible),
        expired=len(result.expired),
        no_gold=len(result.no_gold),
        not_found=len(result.not_found),
        added_note=added_note,
    )
    await status_msg.edit_text(text, parse_mode=ParseMode.HTML)

    links_buffer = BytesIO(("\n".join(links) + "\n").encode("utf-8"))
    links_buffer.name = f"locket_scanned_{source_label}.txt"
    try:
        await update.message.reply_document(
            document=links_buffer,
            caption=f"📁 {len(links)} link",
        )
    except Exception as exc:
        logger.error("Could not send scan links file: %s", exc)

    report_buffer = BytesIO(_report_file("scan", result, source_label=source_label).encode("utf-8"))
    report_buffer.name = f"ket_qua_check_{source_label}.txt"
    try:
        await update.message.reply_document(
            document=report_buffer,
            caption=f"📊 {len(result.eligible)}/{result.checked}",
        )
    except Exception as exc:
        logger.error("Could not send scan report: %s", exc)


# ---------------------------------------------------------------------------
# Admin: /genkey, /set, /checksources, /stats, /noti, /setdonate, /setvideo
# ---------------------------------------------------------------------------

async def cmd_genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    if user_id != ADMIN_ID:
        await update.message.reply_text(T("admin_only", lang), parse_mode=ParseMode.HTML)
        return
    if not context.args:
        await update.message.reply_text(T("genkey_usage", lang), parse_mode=ParseMode.HTML)
        return

    try:
        spins = int(context.args[0])
    except (TypeError, ValueError):
        await update.message.reply_text(T("genkey_invalid", lang), parse_mode=ParseMode.HTML)
        return
    if spins < 1 or spins > 500:
        await update.message.reply_text(T("genkey_invalid", lang), parse_mode=ParseMode.HTML)
        return
    plan = "1m"
    if len(context.args) >= 2 and context.args[1].strip().lower() in ("1y", "1nam", "year", "nam"):
        plan = "1y"
    if len(CDK_SECRET) < 32:
        await update.message.reply_text(T("buy_key_config_error", lang), parse_mode=ParseMode.HTML)
        return

    codes = db.gen_cdk(1, user_id, cdk_secret=CDK_SECRET, source="admin", plan=plan, spins=spins)
    if not codes:
        await update.message.reply_text(T("genkey_invalid", lang), parse_mode=ParseMode.HTML)
        return
    text = T("genkey_done", lang).format(plan=plan_label(plan, lang), spins=spins, codes=codes[0], code=codes[0])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_setsource(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    if user_id != ADMIN_ID:
        await update.message.reply_text(T("admin_only", lang), parse_mode=ParseMode.HTML)
        return

    if not context.args:
        stats = db.gold_source_stats()
        await update.message.reply_text(
            T("set_usage", lang).format(total=stats["total"], usable=stats["usable"]),
            parse_mode=ParseMode.HTML,
        )
        return

    raw = " ".join(context.args).strip()
    status_msg = await update.message.reply_text(T("set_checking", lang), parse_mode=ParseMode.HTML)
    uid, status, error = await _check_account(raw)
    if error == "IP_BLOCKED":
        await status_msg.edit_text(T("redeem_ip_blocked", lang), parse_mode=ParseMode.HTML)
        return
    if not uid:
        await status_msg.edit_text(T("set_invalid", lang), parse_mode=ParseMode.HTML)
        return
    if not status or not status.get("active"):
        await status_msg.edit_text(T("set_not_gold", lang), parse_mode=ParseMode.HTML)
        return

    expires = status.get("expires", "")
    days = locket.plan_days_left(expires)
    if days < GOLD_MIN_SOURCE_DAYS:
        await status_msg.edit_text(T("set_not_gold", lang), parse_mode=ParseMode.HTML)
        return

    expires_text = f"expires: {expires} (còn {days} ngày)"
    added = db.add_gold_source(raw, uid=uid, expires=expires_text, min_days=GOLD_MIN_SOURCE_DAYS)
    if not added:
        await status_msg.edit_text(T("set_invalid", lang), parse_mode=ParseMode.HTML)
        return
    source = next((s for s in db.list_gold_sources() if s["username"].lower() == db.normalize_source_username(raw).lower()), None)
    count = source["count"] if source else 0
    await status_msg.edit_text(
        T("set_done", lang).format(user=_esc(db.normalize_source_username(raw)), expires=_esc(expires), days=days, count=count),
        parse_mode=ParseMode.HTML,
    )


async def cmd_checksources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    if user_id != ADMIN_ID:
        await update.message.reply_text(T("admin_only", lang), parse_mode=ParseMode.HTML)
        return

    probe_arg = " ".join(context.args).strip().lower() if context.args else ""
    probe = probe_arg not in ("quick", "fast", "status")
    status_msg = await update.message.reply_text(T("checksources_running", lang), parse_mode=ParseMode.HTML)
    sources = db.list_gold_sources()
    total = len(sources)
    if not total:
        await status_msg.edit_text(T("set_usage", lang).format(total=0, usable=0), parse_mode=ParseMode.HTML)
        return

    usable = limit = expiring = no_gold = errors = removed = 0
    last_update = time.time()
    for idx, source in enumerate(sources, 1):
        outcome = await activation.check_source(source, probe=probe)
        state = outcome.get("status")
        if state == "ip_blocked":
            errors += 1
            break
        if state == "usable":
            usable += 1
        elif state == "alias_limit":
            limit += 1
            db.release_gold_source(source["id"], exhausted=True)
            removed += 1
        elif state == "expiring":
            expiring += 1
            db.remove_gold_source(source["username"])
            removed += 1
        elif state == "no_gold":
            no_gold += 1
            db.remove_gold_source(source["username"])
            removed += 1
        elif state == "not_found":
            no_gold += 1
            db.remove_gold_source(source["username"])
            removed += 1
        else:
            errors += 1

        if time.time() - last_update >= 3 or idx == total:
            last_update = time.time()
            try:
                await status_msg.edit_text(
                    T("checksources_progress", lang).format(done=idx, total=total, usable=usable, removed=removed),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    await status_msg.edit_text(
        T("checksources_report", lang).format(
            total=total, usable=usable, limit=limit, expiring=expiring,
            no_gold=no_gold, error=errors, removed=removed,
        ),
        parse_mode=ParseMode.HTML,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    stats = db.get_stats()
    cdk = db.cdk_stats()
    sources = db.gold_source_stats()
    msg = (
        f"{E_STAT} <b>SYSTEM STATISTICS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E_USER} <b>Users</b>: {stats['unique_users']}\n"
        f"{E_GLOBE} <b>Requests</b>: {stats['total']} ({stats['success']} ✅ / {stats['fail']} ❌)\n"
        f"🎟️ <b>Keys</b>: {cdk['used']}/{cdk['total']} (còn {cdk['unused']})\n"
        f"🗂️ <b>Nguồn Gold</b>: {sources['total']} (dùng được {sources['usable']})\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def broadcast_worker(bot, users, text, chat_id, message_id):
    success = 0
    fail = 0
    total = len(users)
    for i, uid in enumerate(users):
        try:
            await bot.send_message(chat_id=uid, text=f"📢 <b>ADMIN NOTIFICATION</b>\n\n{text}", parse_mode=ParseMode.HTML)
            success += 1
        except Exception:
            fail += 1
        if (i + 1) % 5 == 0 or (i + 1) == total:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=(
                        f"{E_LOADING} <b>Broadcasting...</b>\n"
                        f"🔄 {i + 1}/{total}\n{E_SUCCESS} {success}\n{E_ERROR} {fail}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        await asyncio.sleep(0.05)


async def noti_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    message = " ".join(context.args)
    if not message:
        await update.message.reply_text("Usage: /noti {message}")
        return
    users = db.get_all_users()
    if not users:
        await update.message.reply_text("No users found.")
        return
    status_msg = await update.message.reply_text(
        f"{E_LOADING} <b>Starting broadcast to {len(users)} users...</b>",
        parse_mode=ParseMode.HTML,
    )
    asyncio.create_task(broadcast_worker(context.bot, users, message, status_msg.chat_id, status_msg.message_id))


async def set_donate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    photo = None
    if update.message.reply_to_message and update.message.reply_to_message.photo:
        photo = update.message.reply_to_message.photo[-1]
    elif update.message.photo:
        photo = update.message.photo[-1]
    if photo:
        db.set_config("donate_photo", photo.file_id)
        await update.message.reply_text(f"✅ Updated Donate Photo ID:\n<code>{photo.file_id}</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Reply to a photo with /setdonate to set it.")


async def set_video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    video = None
    if update.message.reply_to_message and update.message.reply_to_message.video:
        video = update.message.reply_to_message.video
    elif update.message.video:
        video = update.message.video
    if video:
        db.set_config("video_file_id", video.file_id)
        await update.message.reply_text(f"✅ Updated Guide Video ID:\n<code>{video.file_id}</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Reply to a video with /setvideo to set it (or send a video with /setvideo).")


# ---------------------------------------------------------------------------
# Admin notification for a successful activation
# ---------------------------------------------------------------------------

async def fetch_avatar_bytes(avatar_url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(avatar_url, timeout=15) as res:
                if res.status == 200:
                    return await res.read()
    except Exception as exc:
        logger.error("Avatar download error: %s", exc)
    return None


async def notify_admin_success(app, user_id, username, uid, plan, source, key_code, key_left):
    avatar_path = None
    try:
        profile = await locket.resolve_profile(username)
        avatar_url = profile.get("avatar") if profile else None
        if avatar_url:
            os.makedirs(AVATAR_DIR, exist_ok=True)
            data = await fetch_avatar_bytes(avatar_url)
            if data:
                safe_name = "".join(c for c in str(username) if c.isalnum() or c in "._-")[:40] or uid
                avatar_path = os.path.join(AVATAR_DIR, f"{uid}_{safe_name}.jpg")
                with open(avatar_path, "wb") as handle:
                    handle.write(data)
    except Exception as exc:
        logger.error("Avatar save error: %s", exc)

    caption = (
        f"{E_SUCCESS} <b>KÍCH HOẠT THÀNH CÔNG</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E_USER} <b>User</b>: <code>{user_id}</code>\n"
        f"{E_TAG}: <code>{_esc(username)}</code>\n"
        f"{E_ID}: <code>{uid}</code>\n"
        f"📅 Plan: {plan_label(plan, 'EN')}\n"
        f"🗂️ Source: <code>@{_esc(source or '?')}</code>\n"
        f"🎟️ Key: <code>{_esc(key_code)}</code> ({key_left} spins left)"
    )
    try:
        if avatar_path:
            with open(avatar_path, "rb") as handle:
                await app.bot.send_photo(chat_id=ADMIN_ID, photo=handle, caption=caption, parse_mode=ParseMode.HTML)
        else:
            await app.bot.send_message(chat_id=ADMIN_ID, text=caption, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.error("Admin notify error: %s", exc)


# ---------------------------------------------------------------------------
# Menu actions
# ---------------------------------------------------------------------------

async def run_menu_action(action, update, context, lang):
    query = update.callback_query
    user_id = update.effective_user.id
    if query:
        try:
            await query.answer()
        except Exception:
            pass

    target = query.message if query else update.message

    if action == "menu_input":
        context.user_data["awaiting_username"] = True
        await target.reply_text(
            T("prompt_input", lang),
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(input_field_placeholder="Username..."),
        )
        return

    if action == "menu_redeem":
        await target.reply_text(T("redeem_usage", lang), parse_mode=ParseMode.HTML)
        return

    if action == "buy_key":
        if payment_config_errors():
            await target.reply_text(T("buy_key_config_error", lang), parse_mode=ParseMode.HTML)
            return
        await target.reply_text(
            f"🛒 {T('buy_key_title', lang)}",
            parse_mode=ParseMode.HTML,
            reply_markup=get_qty_keyboard(lang, "1m"),
        )
        return

    if action == "menu_scan":
        await target.reply_text(T("scan_usage", lang), parse_mode=ParseMode.HTML)
        return

    if action == "menu_account":
        await _send_account(target, user_id, lang)
        return

    if action == "menu_genkey":
        if user_id != ADMIN_ID:
            return
        await target.reply_text(T("genkey_usage", lang), parse_mode=ParseMode.HTML)
        return

    if action == "menu_guide":
        guide_text = T("guide_msg", lang)
        video_file_id = db.get_config("video_file_id", VIDEO_FILE_ID)
        if video_file_id:
            try:
                await target.reply_video(video=video_file_id, caption=guide_text, parse_mode=ParseMode.HTML, reply_markup=get_back_keyboard())
                return
            except Exception:
                pass
        await target.reply_text(guide_text, parse_mode=ParseMode.HTML, reply_markup=get_back_keyboard())
        return

    if action == "menu_lang":
        await show_language_select(update)
        return

    if action == "menu_help":
        help_text = T("help_msg", lang)
        if user_id == ADMIN_ID:
            help_text += T("admin_help", lang)
        await target.reply_text(help_text, parse_mode=ParseMode.HTML, reply_markup=get_back_keyboard())
        return


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    lang = db.get_lang(user_id) or DEFAULT_LANG

    if data.startswith("setlang_"):
        new_lang = data.split("_", 1)[1]
        db.set_lang(user_id, new_lang)
        await query.answer(f"Language: {new_lang}")
        await query.message.edit_text(
            T("menu_msg", new_lang),
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu_keyboard(new_lang, user_id),
        )
        return

    if data == "menu_back":
        menu_text = T("menu_msg", lang)
        keyboard = get_main_menu_keyboard(lang, user_id)
        try:
            await query.message.edit_text(menu_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except Exception:
            try:
                await query.message.edit_caption(caption=menu_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except Exception:
                await query.answer("⚠️")
        return

    if data.startswith("menu_") or data == "buy_key":
        action = "buy_key" if data == "buy_key" else data
        await run_menu_action(action, update, context, lang)
        return

    if data.startswith("redeem_hint|"):
        raw = data.split("|", 1)[1]
        await query.answer()
        await query.message.reply_text(
            f"{E_KEY} <code>/redeem &lt;mã_key&gt; {_esc(raw)}</code>\n\n"
            + _msg(lang, "Thay &lt;mã_key&gt; bằng Key của bạn (mua qua /nap).",
                   "Replace &lt;mã_key&gt; with your key (buy with /nap)."),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("buy_key_qty_"):
        if payment_config_errors():
            await query.answer(T("buy_key_config_error", lang), show_alert=True)
            return
        try:
            _, _, _, plan, quantity = data.split("_", 4)
            quantity = int(quantity)
        except (TypeError, ValueError):
            await query.answer("❌", show_alert=True)
            return
        if plan not in ("1m", "1y") or quantity not in range(1, 6):
            await query.answer("❌", show_alert=True)
            return

        order = db.get_active_cdk_order_for_user(user_id)
        if order is not None and (order.get("plan") or "1m") != plan:
            db.cancel_cdk_order(order["id"], user_id=user_id)
            order = None
        if order is None and not payment_order_limiter.allow(user_id):
            await query.answer(
                _msg(lang, "⏳ Bạn tạo đơn quá nhanh. Vui lòng thử lại sau.",
                     "⏳ You are creating orders too fast. Please retry shortly."),
                show_alert=True,
            )
            return
        payment_content = _gen_payment_content()
        if order is None:
            order = db.create_cdk_order(
                user_id=user_id,
                chat_id=chat_id,
                quantity=quantity,
                total_price=price_for_plan(plan) * quantity,
                payment_content=payment_content,
                expires_at=int(time.time()) + CDK_ORDER_TIMEOUT_MINUTES * 60,
                plan=plan,
            )
        is_new_order = order["payment_content"] == payment_content
        if not is_new_order and order["chat_id"] != chat_id:
            await query.answer(
                _msg(lang, "⚠️ Bạn đang có đơn chờ thanh toán ở cuộc trò chuyện khác.",
                     "⚠️ You already have a pending order in another chat."),
                show_alert=True,
            )
            return
        await query.answer(
            _msg(lang, "⏳ Đang tạo mã QR...", "⏳ Creating QR...") if is_new_order
            else _msg(lang, "ℹ️ Gửi lại đơn đang chờ thanh toán", "ℹ️ Resending your pending order")
        )
        try:
            await _send_key_order_qr(context.bot, chat_id, order, lang)
        except (VietQrImageError, ValueError, TelegramError) as exc:
            if is_new_order:
                db.cancel_cdk_order(order["id"], user_id=user_id, chat_id=chat_id)
            logger.error("VietQR generation failed for order %s: %s", order["id"], exc)
            detail = (
                _msg(lang, "Đơn mới đã được hủy; vui lòng thử lại hoặc báo admin kiểm tra BANK_BIN.",
                     "The new order was canceled; please retry or ask the admin to check BANK_BIN.")
                if is_new_order
                else _msg(lang, "Đơn hiện tại vẫn được giữ; vui lòng thử gửi lại QR sau.",
                          "The current order is kept; please try to resend the QR later.")
            )
            await query.message.reply_text(
                f"❌ {_msg(lang, 'Hiện không tạo được mã QR thanh toán.', 'Could not create the payment QR.')} {detail}"
            )
        return

    if data.startswith("key_order_check_"):
        try:
            order_id = int(data.rsplit("_", 1)[1])
        except (TypeError, ValueError):
            await query.answer(T("buy_key_not_found", lang), show_alert=True)
            return
        order = db.get_cdk_order(order_id)
        if not order or order["user_id"] != user_id or order["chat_id"] != chat_id:
            await query.answer(T("buy_key_not_found", lang), show_alert=True)
            return
        if order["status"] == "completed":
            codes = db.complete_cdk_order(
                order_id=order_id,
                transaction_id=order["transaction_id"],
                matched_amount=order["matched_amount"],
                secret=CDK_SECRET,
            )
            if not codes:
                await query.answer(_msg(lang, "⚠️ Không thể đọc lại key, báo admin.", "⚠️ Could not re-read keys, contact admin."), show_alert=True)
                return
            await query.answer(_msg(lang, "✅ Gửi lại key", "✅ Keys resent"))
            await context.bot.send_message(
                chat_id=chat_id,
                text=_paid_message(lang, codes, order.get("plan") or "1m"),
                parse_mode=ParseMode.HTML,
            )
            return
        if order["status"] != "pending":
            await query.answer(T("buy_key_not_found", lang), show_alert=True)
            return
        if not payment_manual_check_limiter.allow(user_id):
            await query.answer(
                _msg(lang, "⏳ Bạn kiểm tra quá nhanh. Vui lòng chờ.", "⏳ Too many checks. Please wait."),
                show_alert=True,
            )
            return
        await _complete_key_payment(context.application, order, lang, manual_query=query)
        return

    if data.startswith("key_order_cancel_"):
        try:
            order_id = int(data.rsplit("_", 1)[1])
        except (TypeError, ValueError):
            await query.answer(T("buy_key_not_found", lang), show_alert=True)
            return
        canceled = db.cancel_cdk_order(order_id, user_id=user_id, chat_id=chat_id)
        if not canceled:
            await query.answer(T("buy_key_not_found", lang), show_alert=True)
            return
        await query.answer(_msg(lang, "✅ Đã hủy đơn", "✅ Order canceled"))
        try:
            await query.message.edit_caption(caption=f"❌ #{order_id}", reply_markup=None)
        except Exception:
            pass
        return

    if data == "cdk_copy":
        await query.answer("⚠️")
        return

    await query.answer("⚠️")


# ---------------------------------------------------------------------------
# Error handling + startup
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning("[Network] Telegram timeout (transient): %s", err)
        return
    logger.error("[Error] Unhandled exception: %s", err, exc_info=err)


async def post_init(application):
    asyncio.create_task(key_payment_poller(application))

    async def _wipe_commands():
        wipe_scopes = [
            BotCommandScopeDefault(),
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllGroupChats(),
            BotCommandScopeAllChatAdministrators(),
            BotCommandScopeChat(chat_id=ADMIN_ID),
        ]
        for scope in wipe_scopes:
            try:
                await application.bot.delete_my_commands(scope=scope)
            except Exception:
                pass
        for language in ("vi", "en", "ru", "es", "pt", "id", "th", "zh", "ko"):
            try:
                await application.bot.delete_my_commands(scope=BotCommandScopeDefault(), language_code=language)
            except Exception:
                pass

        user_cmds = [
            BotCommand("start", "Khởi động bot & Menu chính"),
            BotCommand("menu", "Mở Menu chính"),
            BotCommand("nap", "Mua Key Gold (1 tháng / 1 năm)"),
            BotCommand("sodu", "Xem key còn lại & lịch sử"),
            BotCommand("redeem", "Kích hoạt Gold bằng Key"),
            BotCommand("check", "Kiểm tra Gold 1 tài khoản"),
            BotCommand("chk", "Kiểm tra hàng loạt từ file .txt"),
            BotCommand("scan", "Quét link Locket từ TikTok/Threads"),
            BotCommand("setlang", "Đổi ngôn ngữ (VI/EN)"),
            BotCommand("help", "Xem trợ giúp"),
        ]
        admin_cmds = [
            BotCommand("genkey", "Tạo Key thủ công"),
            BotCommand("set", "Xem/thêm nguồn Gold"),
            BotCommand("checksources", "Kiểm tra & dọn kho nguồn"),
            BotCommand("stats", "Thống kê hệ thống"),
            BotCommand("noti", "Gửi thông báo tới tất cả user"),
            BotCommand("setdonate", "Đặt ảnh thành công"),
            BotCommand("setvideo", "Đặt video hướng dẫn"),
        ]
        try:
            await application.bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
            await application.bot.set_my_commands(user_cmds + admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
            await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception as exc:
            logger.error("set_my_commands error: %s", exc)

    asyncio.create_task(_wipe_commands())


def run_bot():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")
    if ADMIN_ID <= 0:
        raise RuntimeError("ADMIN_ID environment variable must be a positive Telegram user ID")
    if not REVENUECAT_APP_KEY:
        raise RuntimeError("REVENUECAT_APP_KEY environment variable is required")

    db.init_db()
    imported = db.import_sources_from_file(SOURCE_FILE, min_days=GOLD_MIN_SOURCE_DAYS)
    if imported:
        logger.info("Imported %s sources from %s", imported, SOURCE_FILE)

    logging.basicConfig(format="%(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.ERROR)
    logging.getLogger("telegram").setLevel(logging.ERROR)
    logging.getLogger("aiohttp").setLevel(logging.ERROR)

    builder = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(20.0)
        .read_timeout(20.0)
        .write_timeout(20.0)
        .pool_timeout(20.0)
        .connection_pool_size(16)
        .get_updates_connect_timeout(20.0)
        .get_updates_read_timeout(40.0)
        .concurrent_updates(8)
        .post_init(post_init)
    )
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
        print("🌐 Using configured proxy for Telegram")
    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("nap", cmd_nap))
    app.add_handler(CommandHandler("muacdk", cmd_muacdk))
    app.add_handler(CommandHandler("sodu", cmd_sodu))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("chk", cmd_chk))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("setlang", setlang_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("set", cmd_setsource))
    app.add_handler(CommandHandler("setsource", cmd_setsource))
    app.add_handler(CommandHandler("checksources", cmd_checksources))
    app.add_handler(CommandHandler("cleansources", cmd_checksources))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("noti", noti_command))
    app.add_handler(CommandHandler("setdonate", set_donate_command))
    app.add_handler(CommandHandler("setvideo", set_video_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, cmd_chk))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    print("Bot is running... (alias activation engine)")
    app.run_polling(drop_pending_updates=True)
