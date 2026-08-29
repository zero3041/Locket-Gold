import asyncio
import html
import logging
import os
import secrets
import time
from io import BytesIO
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, ReplyKeyboardMarkup, KeyboardButton, BotCommand, BotCommandScopeDefault, BotCommandScopeChat, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeAllChatAdministrators, InputFile, MenuButtonCommands
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import NetworkError, TelegramError, TimedOut
from app.config import *
from app import database as db
from app.services import locket, nextdns
from app.services import sepay

logger = logging.getLogger(__name__)

request_queue = asyncio.Queue()
pending_items = []
queue_lock = asyncio.Lock()
last_cdk_batch = {}
processing_cdk_orders = set()


class CdkAttemptLimiter:
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


cdk_attempt_limiter = CdkAttemptLimiter()
payment_order_limiter = CdkAttemptLimiter(
    max_attempts=CDK_ORDER_CREATE_MAX,
    window_seconds=CDK_ORDER_CREATE_WINDOW_SECONDS,
)
payment_manual_check_limiter = CdkAttemptLimiter(
    max_attempts=CDK_MANUAL_CHECK_MAX,
    window_seconds=CDK_MANUAL_CHECK_WINDOW_SECONDS,
)
sepay_global_limiter = CdkAttemptLimiter(
    max_attempts=SEPAY_GLOBAL_CHECK_MAX,
    window_seconds=SEPAY_GLOBAL_CHECK_WINDOW_SECONDS,
)

REPLY_ACTIONS = {}
def _build_reply_actions():
    for _lang in ("VI", "EN"):
        REPLY_ACTIONS[T("btn_input", _lang)] = "menu_input"
        REPLY_ACTIONS[T("btn_dns", _lang)] = "menu_dns"
        REPLY_ACTIONS[T("btn_guide", _lang)] = "menu_guide"
        REPLY_ACTIONS[T("btn_lang", _lang)] = "menu_lang"
        REPLY_ACTIONS[T("btn_help", _lang)] = "menu_help"
        REPLY_ACTIONS[T("btn_cdk_admin", _lang)] = "menu_cdk"
        REPLY_ACTIONS[T("btn_buy_cdk", _lang)] = "buy_cdk"
_build_reply_actions()

AVATAR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "avatars")

async def fetch_avatar_bytes(avatar_url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(avatar_url, timeout=15) as res:
                if res.status == 200:
                    return await res.read()
    except Exception as e:
        logger.error(f"Avatar download error: {e}")
    return None

async def notify_admin_success(app, user_id, username, uid, worker_id, token_name):
    avatar_path = None
    try:
        profile = await locket.resolve_profile(username)
        avatar_url = profile.get("avatar") if profile else None
        if avatar_url:
            os.makedirs(AVATAR_DIR, exist_ok=True)
            data = await fetch_avatar_bytes(avatar_url)
            if data:
                safe_name = "".join(c for c in username if c.isalnum() or c in "._-")[:40] or uid
                avatar_path = os.path.join(AVATAR_DIR, f"{uid}_{safe_name}.jpg")
                with open(avatar_path, "wb") as f:
                    f.write(data)
    except Exception as e:
        logger.error(f"Avatar save error: {e}")

    caption = (
        f"{E_SUCCESS} <b>KÍCH HOẠT THÀNH CÔNG</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E_USER} <b>User</b>: <code>{user_id}</code>\n"
        f"{E_TAG}: <code>{username}</code>\n"
        f"{E_ID}: <code>{uid}</code>\n"
        f"{E_ANDROID} <b>Worker</b>: #{worker_id} ({token_name})"
    )
    try:
        if avatar_path:
            with open(avatar_path, "rb") as f:
                await app.bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=f,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
        else:
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
    except Exception as e:
        logger.error(f"Admin notify error: {e}")


async def create_profile_rotating(log_callback=None, profile_name=None):
    for key in NEXTDNS_KEYS:
        pid, link = await nextdns.create_profile(key, log_callback, profile_name)
        if link:
            return pid, link
    return None, None

class Clr:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

async def update_pending_positions(app):
    for i, item in enumerate(pending_items):
        position = i + 1
        ahead = i
        try:
            # Update position text
            await app.bot.edit_message_text(
                chat_id=item['chat_id'],
                message_id=item['message_id'],
                text=T("queued", item['lang']).format(item['username'], position, ahead),
                parse_mode=ParseMode.HTML
            )
            
            # Notify if almost turn (ahead == 2)
            if ahead == 2:
                try:
                    await app.bot.send_message(
                        chat_id=item['chat_id'],
                        text=T("queue_almost", item['lang']),
                        parse_mode=ParseMode.HTML
                    )
                except:
                    pass
        except:
            pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    
    if not db.get_user_usage(user_id):
        pass 

    await update.message.reply_text(
        T("welcome", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard(lang, user_id)
    )
    await update.message.reply_text(
        T("menu_msg", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=get_reply_keyboard(lang, user_id)
    )

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    await update.message.reply_text(
        T("menu_msg", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_menu_keyboard(lang, user_id)
    )
    await update.message.reply_text(
        "⌨️",
        reply_markup=get_reply_keyboard(lang, user_id)
    )


def format_vnd(amount):
    return f"{int(amount):,}".replace(",", ".") + " VND"


def should_send_donate_photo(cdk_source):
    """The thank-you/donate QR is reserved for admin-issued CDKs."""
    return cdk_source == "admin"


def get_buy_cdk_keyboard(lang):
    rows = []
    for start in (1, 3, 5):
        row = []
        for quantity in range(start, min(start + 2, 6)):
            total = CDK_UNIT_PRICE * quantity
            label = f"{quantity} CDK"
            if total > 0:
                label += f" • {format_vnd(total)}"
            row.append(InlineKeyboardButton(label, callback_data=f"buy_cdk_qty_{quantity}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def buy_cdk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = db.get_lang(update.effective_user.id) or DEFAULT_LANG
    if payment_config_errors():
        await update.message.reply_text(T("buy_cdk_config_error", lang), parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(
        T("buy_cdk_title", lang),
        parse_mode=ParseMode.HTML,
        reply_markup=get_buy_cdk_keyboard(lang),
    )


def _new_payment_content():
    return "CDK" + secrets.token_hex(8).upper()


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


async def _send_cdk_order_qr(bot, chat_id, order, lang):
    qr_url = sepay.build_vietqr_url(
        BANK_BIN,
        BANK_ACCOUNT,
        order["total_price"],
        order["payment_content"],
        account_name=BANK_OWNER,
    )
    caption = (
        f"💳 <b>THANH TOÁN CDK #{order['id']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎟️ Số lượng: <b>{order['quantity']} CDK</b>\n"
        f"💰 Tổng tiền: <b>{format_vnd(order['total_price'])}</b>\n\n"
        f"🏦 Ngân hàng: {html.escape(BANK_NAME)}\n"
        f"👤 Chủ tài khoản: {html.escape(BANK_OWNER)}\n"
        f"💳 Số tài khoản: <code>{html.escape(BANK_ACCOUNT)}</code>\n"
        f"📝 Nội dung: <code>{html.escape(order['payment_content'])}</code>\n\n"
        f"⚠️ Chuyển đúng số tiền và nội dung. Đơn hết hạn sau "
        f"{CDK_ORDER_TIMEOUT_MINUTES} phút."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Kiểm tra thanh toán", callback_data=f"buy_cdk_check_{order['id']}")],
        [InlineKeyboardButton("❌ Hủy đơn", callback_data=f"buy_cdk_cancel_{order['id']}")],
    ])
    qr_image = await _download_vietqr_image(qr_url)
    await bot.send_photo(
        chat_id=chat_id,
        photo=InputFile(BytesIO(qr_image), filename=f"cdk-payment-{order['id']}.png"),
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def _complete_cdk_payment(application, order, lang, manual_query=None):
    order_id = order["id"]
    if order_id in processing_cdk_orders:
        if manual_query:
            await manual_query.answer("⏳ Đơn đang được xử lý", show_alert=True)
        return False

    if not sepay_global_limiter.allow("global"):
        if manual_query:
            await manual_query.answer(
                "⏳ Hệ thống đang kiểm tra nhiều giao dịch, vui lòng thử lại sau.",
                show_alert=True,
            )
        return False

    processing_cdk_orders.add(order_id)
    try:
        async with sepay.SePayClient(SEPAY_API_TOKEN, base_url=SEPAY_API_URL) as client:
            transaction = await client.find_matching_transaction(
                order["payment_content"],
                order["total_price"],
                account_number=BANK_ACCOUNT,
            )
        if not transaction:
            if manual_query:
                await manual_query.answer(T("buy_cdk_waiting", lang), show_alert=True)
            return False

        codes = db.complete_cdk_order(
            order_id=order_id,
            transaction_id=transaction["id"],
            matched_amount=transaction["amount_in"],
            secret=CDK_SECRET,
        )
        if not codes:
            logger.error("CDK order completion rejected: order_id=%s transaction_id=%s", order_id, transaction["id"])
            if manual_query:
                await manual_query.answer("⚠️ Giao dịch cần admin kiểm tra", show_alert=True)
            return False

        if manual_query:
            await manual_query.answer("✅ Thanh toán thành công")
        await application.bot.send_message(
            chat_id=order["chat_id"],
            text=T("buy_cdk_paid", lang).format(codes="\n".join(codes)),
            parse_mode=ParseMode.HTML,
        )
        try:
            await application.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"💰 <b>ĐƠN CDK ĐÃ THANH TOÁN</b>\n"
                    f"Đơn: <code>#{order_id}</code>\n"
                    f"User: <code>{order['user_id']}</code>\n"
                    f"Số lượng: {order['quantity']}\n"
                    f"Số tiền: {format_vnd(transaction['amount_in'])}\n"
                    f"SePay transaction: <code>{html.escape(transaction['id'])}</code>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.warning("Could not notify admin for CDK order %s: %s", order_id, exc)
        return True
    except sepay.SePayError as exc:
        logger.warning("SePay check failed for order %s: %s", order_id, exc)
        if manual_query:
            await manual_query.answer("⚠️ Sepay tạm thời không phản hồi", show_alert=True)
        return False
    finally:
        processing_cdk_orders.discard(order_id)


async def cdk_payment_poller(application):
    while True:
        try:
            expired = db.expire_cdk_orders()
            for order in expired:
                # Web-store orders have no Telegram chat to notify.
                if not order.get("chat_id"):
                    continue
                try:
                    await application.bot.send_message(
                        chat_id=order["chat_id"],
                        text=f"⌛ Đơn mua CDK #{order['id']} đã hết hạn.",
                    )
                except Exception:
                    pass
            for order in db.get_pending_cdk_orders()[:SEPAY_MAX_ORDERS_PER_POLL]:
                # Web-store orders are completed by web_store.py's own poller.
                if not order.get("chat_id"):
                    continue
                lang = db.get_lang(order["user_id"]) or DEFAULT_LANG
                await _complete_cdk_payment(application, order, lang)
        except Exception as exc:
            logger.error("CDK payment poller error: %s", exc)
        await asyncio.sleep(SEPAY_POLL_INTERVAL_SECONDS)

async def web_activation_poller(application):
    """Feed paid/free web-store activations into the shared activation queue."""
    while True:
        try:
            for activation in db.list_queued_web_activations(limit=20):
                if not db.claim_web_activation(activation["id"]):
                    continue  # Another poller round claimed it first.
                item = {
                    'user_id': activation["visitor_id"],
                    'uid': activation["uid"],
                    'username': activation["username"],
                    'chat_id': None,
                    'message_id': None,
                    'lang': DEFAULT_LANG,
                    'cdk': activation.get("cdk_code"),
                    'cdk_source': 'web',
                    'web_activation_id': activation["id"],
                }
                await request_queue.put(item)
                print(f"{Clr.BLUE}[Web]{Clr.ENDC} Web activation #{activation['id']} queued: UID={activation['uid']}")
        except Exception as exc:
            logger.error("Web activation poller error: %s", exc)
        await asyncio.sleep(SEPAY_POLL_INTERVAL_SECONDS)


async def setlang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_language_select(update)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    
    help_text = T("help_msg", lang)
    if user_id == ADMIN_ID:
        help_text += T("admin_help", lang)
        
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return

    stats = db.get_stats()
    cdk = db.cdk_stats()
    msg = (
        f"{E_STAT} <b>SYSTEM STATISTICS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E_USER} <b>Active Users</b>: {stats['unique_users']}\n"
        f"{E_GLOBE} <b>Total Requests</b>: {stats['total']}\n"
        f"{E_SUCCESS} <b>Success</b>: {stats['success']}\n"
        f"{E_ERROR} <b>Failed</b>: {stats['fail']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{E_ANDROID} <b>Active Workers</b>: {NUM_WORKERS}\n"
        f"🔑 <b>Token Sets</b>: {len(TOKEN_SETS)}\n"
        f"🌐 <b>DNS Keys</b>: {len(NEXTDNS_KEYS)}\n"
        f"🎟️ <b>CDK</b>: {cdk['used']}/{cdk['total']} (còn {cdk['unused']})\n"
        f"⏳ <b>Queue Size</b>: {request_queue.qsize()}\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# --- Admin Commands ---
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
            
        # Update progress every 5 users or at the end
        if (i + 1) % 5 == 0 or (i + 1) == total:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=(
                        f"{E_LOADING} <b>Broadcasting...</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"🔄 <b>Progress</b>: {i+1}/{total}\n"
                        f"{E_SUCCESS} <b>Success</b>: {success}\n"
                        f"{E_ERROR} <b>Failed</b>: {fail}"
                    ),
                    parse_mode=ParseMode.HTML
                )
            except:
                pass
        
        await asyncio.sleep(0.05) # Prevent flood limits

    # Final completion message
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                f"{E_SUCCESS} <b>Broadcast Complete!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"👥 <b>Total</b>: {total}\n"
                f"{E_SUCCESS} <b>Success</b>: {success}\n"
                f"{E_ERROR} <b>Failed</b>: {fail}"
            ),
            parse_mode=ParseMode.HTML
        )
    except:
        pass

async def noti_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    
    if user_id != ADMIN_ID:
        return
        
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Usage: /noti {message}")
        return

    users = db.get_all_users()
    if not users:
        await update.message.reply_text("No users found.")
        return

    status_msg = await update.message.reply_text(
        f"{E_LOADING} <b>Starting broadcast to {len(users)} users...</b>",
        parse_mode=ParseMode.HTML
    )
    
    asyncio.create_task(broadcast_worker(context.bot, users, msg, status_msg.chat_id, status_msg.message_id))

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = db.get_lang(user_id) or DEFAULT_LANG
    
    if user_id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /rs {user_id}")
        return
        
    try:
        target_id = int(context.args[0])
        db.reset_usage(target_id)
        await update.message.reply_text(T("admin_reset", lang).format(target_id))
    except ValueError:
        await update.message.reply_text("Invalid User ID")

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
        file_id = photo.file_id
        db.set_config("donate_photo", file_id)
        await update.message.reply_text(f"✅ Updated Donate Photo ID:\n<code>{file_id}</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Please reply to a photo with /setdonate to set it.")

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
        file_id = video.file_id
        db.set_config("video_file_id", file_id)
        await update.message.reply_text(f"✅ Updated Guide Video ID:\n<code>{file_id}</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Reply to a video with /setvideo to set it (or send a video with /setvideo).")

async def set_video_dns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    video = None
    if update.message.reply_to_message and update.message.reply_to_message.video:
        video = update.message.reply_to_message.video
    elif update.message.video:
        video = update.message.video

    if video:
        file_id = video.file_id
        db.set_config("video_dns_file_id", file_id)
        await update.message.reply_text(f"✅ Updated DNS Guide Video ID:\n<code>{file_id}</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Reply to a video with /setvideodns to set it (or send a video with /setvideodns).")

async def show_language_select(update: Update):
    keyboard = [
        [InlineKeyboardButton("Tiếng Việt 🇻🇳", callback_data="setlang_VI")],
        [InlineKeyboardButton("English 🇺🇸", callback_data="setlang_EN")]
    ]
    text = T("lang_select", "EN")
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    lang = db.get_lang(user_id) or DEFAULT_LANG

    # Reply Keyboard buttons act like the Inline menu buttons (MenuBuilder-style:
    # every lower-menu button is bound to an action/command).
    action = REPLY_ACTIONS.get(text)
    if action:
        await run_menu_action(action, update, context, lang)
        return

    # Accept the username whether the user taps "reply" to the prompt OR just types
    # it after pressing the input button. Requiring a ForceReply alone silently
    # dropped messages, because selective ForceReply doesn't target the user when
    # the prompt is a reply to the bot's own message in a private chat.
    reply = update.message.reply_to_message
    is_reply_to_bot = bool(reply and reply.from_user and reply.from_user.is_bot)
    if not is_reply_to_bot and not context.user_data.get("awaiting_username") \
            and not context.user_data.get("awaiting_cdk") and not context.user_data.get("awaiting_cdk_qty"):
        return

    if context.user_data.pop("awaiting_cdk_qty", None):
        if user_id != ADMIN_ID:
            return
        try:
            n = int(text)
        except ValueError:
            n = 0
        if n < 1 or n > 500:
            await update.message.reply_text(T("cdk_qty_invalid", lang), parse_mode=ParseMode.HTML)
            return
        if len(CDK_SECRET) < 32:
            await update.message.reply_text(T("buy_cdk_config_error", lang), parse_mode=ParseMode.HTML)
            return
        codes = db.gen_cdk(n, user_id, cdk_secret=CDK_SECRET, source="admin")
        if not codes:
            await update.message.reply_text(T("cdk_stolen", lang), parse_mode=ParseMode.HTML)
            return
        last_cdk_batch[user_id] = codes
        await update.message.reply_text(
            T("cdk_done_header", lang).format(n=len(codes)),
            parse_mode=ParseMode.HTML
        )
        for code in codes:
            await update.message.reply_text(f"<code>{code}</code>", parse_mode=ParseMode.HTML)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(T("cdk_btn_copy", lang), callback_data="cdk_copy")]])
        await update.message.reply_text("📋", reply_markup=kb)
        return

    if context.user_data.pop("awaiting_cdk", None):
        cdk_code = text.strip().upper()
        if not cdk_attempt_limiter.allow(user_id):
            context.user_data["awaiting_cdk"] = True
            await update.message.reply_text(T("cdk_rate_limited", lang), parse_mode=ParseMode.HTML)
            return
        if not db.reserve_cdk(
            cdk_code, user_id, secret=CDK_SECRET,
            ttl_seconds=CDK_RESERVATION_TTL_SECONDS,
        ):
            context.user_data["awaiting_cdk"] = True
            await update.message.reply_text(
                T("cdk_invalid", lang),
                parse_mode=ParseMode.HTML,
                reply_markup=get_cdk_contact_keyboard()
            )
            return
        cdk_source = db.get_cdk_source(cdk_code, secret=CDK_SECRET) or "admin"
        saved = context.user_data.pop("awaiting_upg", None)
        if saved:
            msg = await update.message.reply_text(T("cdk_valid", lang), parse_mode=ParseMode.HTML)
            res = await enqueue_activation(
                context, user_id, saved["uid"], saved["username"],
                update.message.chat_id, msg.message_id, lang,
                cdk=cdk_code, cdk_source=cdk_source,
            )
            if res == "limit":
                db.release_cdk(cdk_code, user_id, secret=CDK_SECRET)
                await msg.edit_text(T("limit_reached", lang), parse_mode=ParseMode.HTML)
            return
        context.user_data["validated_cdk"] = {"code": cdk_code, "source": cdk_source}
        await update.message.reply_text(T("cdk_success", lang), parse_mode=ParseMode.HTML)
        context.user_data["awaiting_username"] = True
        await update.message.reply_text(T("prompt_input", lang), parse_mode=ParseMode.HTML)
        return

    context.user_data.pop("awaiting_username", None)

    if "locket.cam/" in text:
        username = text.split("locket.cam/")[-1].split("?")[0]
    elif len(text) < 50 and " " not in text:
        username = text
    else:
        username = text

    msg = await update.message.reply_text(T("resolving", lang), parse_mode=ParseMode.HTML)
    
    profile = await locket.resolve_profile(username)
    if not profile:
        # Not found: drop the error message and reopen the main menu
        try:
            await msg.delete()
        except:
            pass
        await update.message.reply_text(
            T("menu_msg", lang),
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu_keyboard(lang, user_id)
        )
        return
    uid = profile['uid']
        
    # Admin bypass limit check
    if user_id != ADMIN_ID and not db.check_can_request(user_id):
        await msg.edit_text(T("limit_reached", lang), parse_mode=ParseMode.HTML)
        return
        
    await msg.edit_text(T("checking_status", lang), parse_mode=ParseMode.HTML)
    status = await locket.check_status(uid)
    
    status_text = T("free_status", lang)
    if status and status.get("active"):
        status_text = T("gold_active", lang).format(status['expires'])
    
    safe_username = username[:30]
    keyboard = [[InlineKeyboardButton(T("btn_upgrade", lang), callback_data=f"upg|{uid}|{safe_username}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    user_info = (
        f"{T('user_info_title', lang)}\n"
        f"{E_ID}: <code>{uid}</code>\n"
        f"{E_TAG}: <code>{username}</code>\n"
        f"{E_STAT} <b>Status</b>: {status_text}\n\n"
        f"👇"
    )

    avatar = profile.get('avatar')
    if avatar:
        try:
            await msg.delete()
            await update.message.reply_photo(
                photo=avatar,
                caption=user_info,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            return
        except Exception:
            pass

    await msg.edit_text(
        user_info,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )

async def enqueue_activation(context, user_id, uid, username, chat_id, message_id, lang, cdk=None, cdk_source=None):
    if user_id != ADMIN_ID and not db.check_can_request(user_id):
        return "limit"
    item = {
        'user_id': user_id,
        'uid': uid,
        'username': username,
        'chat_id': chat_id,
        'message_id': message_id,
        'lang': lang,
        'cdk': cdk,
        'cdk_source': cdk_source,
    }
    async with queue_lock:
        pending_items.append(item)
        position = len(pending_items)
        ahead = position - 1
    await request_queue.put(item)
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=T("queued", lang).format(username, position, ahead),
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass
    return "ok"

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    lang = db.get_lang(user_id) or DEFAULT_LANG

    if data.startswith("setlang_"):
        new_lang = data.split("_")[1]
        db.set_lang(user_id, new_lang)
        lang = new_lang
        await query.answer(f"Language: {new_lang}")
        await query.message.edit_text(
            T("menu_msg", lang),
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu_keyboard(lang, user_id)
        )
        return

    if data == "menu_lang":
        await run_menu_action("menu_lang", update, context, lang)
        return
        
    if data == "menu_help":
        await run_menu_action("menu_help", update, context, lang)
        return

    if data == "menu_guide":
        await run_menu_action("menu_guide", update, context, lang)
        return

    if data == "dns_video":
        video_file_id = db.get_config("video_dns_file_id", "")
        if not video_file_id:
            try:
                await query.answer("⚠️")
            except:
                pass
            return
        try:
            await query.message.reply_video(video=video_file_id)
        except Exception:
            try:
                await query.answer("⚠️")
            except:
                pass
        return

    if data == "menu_back":
        menu_text = T("menu_msg", lang)
        keyboard = get_main_menu_keyboard(lang, user_id)
        try:
            await query.message.edit_text(menu_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except Exception:
            try:
                await query.edit_message_caption(caption=menu_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except Exception:
                await query.answer("⚠️")
        return

    if data == "menu_dns":
        # Standalone "permanent block DNS" — no activation needed, no usage limit.
        try:
            await query.answer("🛡️ DNS...")
        except:
            pass
        status_msg = await query.message.reply_text(
            T("dns_creating", lang),
            parse_mode=ParseMode.HTML
        )
        pid, link = await create_profile_rotating(profile_name="LocketVIP-Permanent")
        if link:
            await status_msg.edit_text(
                T("dns_permanent", lang).format(link, pid),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        else:
            await status_msg.edit_text(T("dns_error", lang), parse_mode=ParseMode.HTML)
        return

    if data == "menu_cdk":
        await run_menu_action("menu_cdk", update, context, lang)
        return

    if data == "buy_cdk":
        await run_menu_action("buy_cdk", update, context, lang)
        return

    if data.startswith("buy_cdk_qty_"):
        if payment_config_errors():
            await query.answer(T("buy_cdk_config_error", lang), show_alert=True)
            return
        try:
            quantity = int(data.rsplit("_", 1)[1])
        except (TypeError, ValueError):
            quantity = 0
        if quantity not in range(1, 6):
            await query.answer("❌ Số lượng không hợp lệ", show_alert=True)
            return
        order = db.get_active_cdk_order_for_user(user_id)
        if order is None and not payment_order_limiter.allow(user_id):
            await query.answer(
                "⏳ Bạn tạo đơn quá nhanh. Vui lòng dùng đơn hiện tại hoặc thử lại sau.",
                show_alert=True,
            )
            return
        payment_content = _new_payment_content()
        if order is None:
            order = db.create_cdk_order(
                user_id=user_id,
                chat_id=chat_id,
                quantity=quantity,
                total_price=CDK_UNIT_PRICE * quantity,
                payment_content=payment_content,
                expires_at=int(time.time()) + CDK_ORDER_TIMEOUT_MINUTES * 60,
            )
        is_new_order = order["payment_content"] == payment_content
        if not is_new_order and order["chat_id"] != chat_id:
            await query.answer(
                "⚠️ Bạn đang có đơn chờ thanh toán ở cuộc trò chuyện khác.",
                show_alert=True,
            )
            return
        await query.answer(
            "⏳ Đang tạo mã QR..." if is_new_order else "ℹ️ Gửi lại đơn đang chờ thanh toán"
        )
        try:
            await _send_cdk_order_qr(context.bot, chat_id, order, lang)
        except (VietQrImageError, ValueError, TelegramError) as exc:
            if is_new_order:
                db.cancel_cdk_order(order["id"], user_id=user_id, chat_id=chat_id)
            logger.error("VietQR generation failed for order %s: %s", order["id"], exc)
            failure_detail = (
                "Đơn mới đã được hủy; vui lòng thử lại hoặc báo admin kiểm tra BANK_BIN."
                if is_new_order
                else "Đơn hiện tại vẫn được giữ; vui lòng thử gửi lại QR sau."
            )
            await query.message.reply_text(
                f"❌ Hiện không tạo được mã QR thanh toán. {failure_detail}",
            )
        return

    if data.startswith("buy_cdk_check_"):
        try:
            order_id = int(data.rsplit("_", 1)[1])
        except (TypeError, ValueError):
            await query.answer(T("buy_cdk_not_found", lang), show_alert=True)
            return
        order = db.get_cdk_order(order_id)
        if (
            not order
            or order["user_id"] != user_id
            or order["chat_id"] != chat_id
        ):
            await query.answer(T("buy_cdk_not_found", lang), show_alert=True)
            return
        if order["status"] == "completed":
            codes = db.complete_cdk_order(
                order_id=order_id,
                transaction_id=order["transaction_id"],
                matched_amount=order["matched_amount"],
                secret=CDK_SECRET,
            )
            if not codes:
                await query.answer("⚠️ Không thể đọc lại CDK, vui lòng báo admin", show_alert=True)
                return
            await query.answer("✅ Gửi lại CDK")
            await context.bot.send_message(
                chat_id=chat_id,
                text=T("buy_cdk_paid", lang).format(codes="\n".join(codes)),
                parse_mode=ParseMode.HTML,
            )
            return
        if order["status"] != "pending":
            await query.answer(T("buy_cdk_not_found", lang), show_alert=True)
            return
        if not payment_manual_check_limiter.allow(user_id):
            await query.answer(
                "⏳ Bạn kiểm tra quá nhanh. Vui lòng chờ rồi thử lại.",
                show_alert=True,
            )
            return
        await _complete_cdk_payment(context.application, order, lang, manual_query=query)
        return

    if data.startswith("buy_cdk_cancel_"):
        try:
            order_id = int(data.rsplit("_", 1)[1])
        except (TypeError, ValueError):
            await query.answer(T("buy_cdk_not_found", lang), show_alert=True)
            return
        canceled = db.cancel_cdk_order(order_id, user_id=user_id, chat_id=chat_id)
        if not canceled:
            await query.answer(T("buy_cdk_not_found", lang), show_alert=True)
            return
        await query.answer("✅ Đã hủy đơn")
        try:
            await query.message.edit_caption(
                caption=f"❌ Đơn mua CDK #{order_id} đã hủy.",
                reply_markup=None,
            )
        except Exception:
            pass
        return

    if data == "cdk_copy":
        codes = last_cdk_batch.get(user_id)
        if codes:
            block = "\n".join(codes)
            for i in range(0, len(block), 3800):
                await query.message.reply_text(f"<pre>{block[i:i + 3800]}</pre>", parse_mode=ParseMode.HTML)
        else:
            await query.answer("⚠️")
        return

    if data == "menu_input":
        await run_menu_action("menu_input", update, context, lang)
        return

    if data.startswith("upg|"):
        parts = data.split("|")
        uid = parts[1]
        username = parts[2] if len(parts) > 2 else uid

        if user_id != ADMIN_ID and not db.check_can_request(user_id):
            try:
                await query.answer(T("limit_reached", lang), show_alert=True)
            except:
                pass
            return

        if user_id != ADMIN_ID:
            validated_cdk = context.user_data.pop("validated_cdk", None)
            if validated_cdk:
                try:
                    await query.answer("🚀 Queue...")
                except Exception:
                    pass
                res = await enqueue_activation(
                    context, user_id, uid, username,
                    query.message.chat_id, query.message.message_id, lang,
                    cdk=validated_cdk["code"],
                    cdk_source=validated_cdk["source"],
                )
                if res == "limit":
                    db.release_cdk(validated_cdk["code"], user_id, secret=CDK_SECRET)
                    await query.answer(T("limit_reached", lang), show_alert=True)
                return
            saved_uids = db.get_activation_uids(user_id)
            skip_cdk = (uid in saved_uids and db.has_cdk(user_id))
            if not skip_cdk:
                try:
                    await query.answer("🎟️ Vui lòng nhập CDK", show_alert=True)
                except:
                    pass
                context.user_data["awaiting_cdk"] = True
                context.user_data["awaiting_upg"] = {"uid": uid, "username": username}
                if saved_uids:
                    prompt = T("cdk_switch_uid", lang).format(", ".join(saved_uids))
                else:
                    prompt = T("cdk_prompt_user", lang)
                await query.message.reply_text(
                    prompt,
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_cdk_contact_keyboard()
                )
                return

        try:
            await query.answer("🚀 Queue...")
        except:
            pass

        res = await enqueue_activation(context, user_id, uid, username, query.message.chat_id, query.message.message_id, lang)
        if res == "limit":
            try:
                await query.answer(T("limit_reached", lang), show_alert=True)
            except:
                pass
        return
        
        await request_queue.put(item)
        return

async def queue_worker(app, worker_id):
    # Select token based on worker ID (round-robin)
    # worker_id is 1-based, so subtract 1
    token_idx = (worker_id - 1) % len(TOKEN_SETS)
    token_config = TOKEN_SETS[token_idx]
    token_name = f"Token-{token_idx+1}"
    
    print(f"Worker #{worker_id} started using {token_name}...")
    
    while True:
        item = None
        try:
            item = await request_queue.get()
            
            user_id = item['user_id']
            uid = item['uid']
            username = item['username']
            chat_id = item['chat_id']
            message_id = item['message_id']
            lang = item['lang']
            cdk_code = item.get('cdk')
            cdk_source = item.get('cdk_source')
            web_activation_id = item.get('web_activation_id')
            is_web = web_activation_id is not None
            
            async with queue_lock:
                if item in pending_items:
                    pending_items.remove(item)
                await update_pending_positions(app) # Enabled queue updates
            
            print(f"{Clr.BLUE}[Worker #{worker_id}][{token_name}] Processing:{Clr.ENDC} UID={uid} | UserID={user_id}")
            
            async def edit(text):
                if is_web:
                    db.update_web_activation(web_activation_id, progress=text)
                    return
                try:
                    await app.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                except Exception as e:
                    if "Message is not modified" in str(e):
                        pass
                    elif "Message to edit not found" in str(e):
                        pass
                    else:
                        logger.error(f"Edit msg error: {e}")

            # Double check limit before processing (unless admin)
            # Web auto-activations are paid orders — bypass the free daily limit.
            if not is_web and user_id != ADMIN_ID and not db.check_can_request(user_id):
                if cdk_code:
                    db.release_cdk(cdk_code, user_id, secret=CDK_SECRET)
                await edit(T("limit_reached", lang))
                request_queue.task_done()
                continue
            # Web activations carry a freshly generated, non-reserved CDK.
            if cdk_code and not is_web and not db.lock_reserved_cdk(
                cdk_code, user_id, secret=CDK_SECRET,
            ):
                await edit(T("cdk_invalid", lang))
                request_queue.task_done()
                continue
            
            logs = [f"[Worker #{worker_id}] Processing Request..."]
            loop = asyncio.get_running_loop()
            
            def safe_log_callback(msg):
                clean_msg = msg.replace(Clr.BLUE, "").replace(Clr.GREEN, "").replace(Clr.WARNING, "").replace(Clr.FAIL, "").replace(Clr.ENDC, "").replace(Clr.BOLD, "")
                logs.append(clean_msg)
                asyncio.run_coroutine_threadsafe(update_log_ui(), loop)

            async def update_log_ui():
                display_logs = "\n".join(logs[-10:])
                text = (
                    f"{E_LOADING} <b>⚡ SYSTEM EXPLOIT RUNNING...</b>\n"
                    f"<pre>{display_logs}</pre>"
                )
                if is_web:
                    db.update_web_activation(web_activation_id, progress=text)
                    return
                try:
                    await app.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                except:
                    pass

            await update_log_ui()
            
            # Use dynamic token config
            success, msg_result = await locket.inject_gold(uid, token_config, safe_log_callback)
            
            # Log request to DB
            db.log_request(user_id, uid, "SUCCESS" if success else "FAIL")
            
            if success:
                if cdk_code:
                    if is_web:
                        # Web CDK was generated fresh for this order (not reserved).
                        db.redeem_cdk(cdk_code, user_id, secret=CDK_SECRET)
                    else:
                        redeemed = db.redeem_reserved_cdk(cdk_code, user_id, secret=CDK_SECRET)
                        if not redeemed:
                            logger.critical("Reserved CDK redemption failed after activation: user_id=%s uid=%s", user_id, uid)
                db.save_activation(user_id, uid, username)

                if user_id != ADMIN_ID and not is_web:
                    db.increment_usage(user_id)

                await notify_admin_success(app, user_id, username, uid, worker_id, token_name)

                pid, link = await create_profile_rotating(safe_log_callback, profile_name="LocketVIP-Permanent")

                dns_text = ""
                if link:
                   dns_text = T('dns_msg', lang).format(link, pid)
                else:
                   dns_text = f"{E_ERROR} NextDNS Error: Check API Key"
                
                final_msg = (
                    f"{T('success_title', lang)}\n\n"
                    f"{E_TAG}: <code>{username}</code>\n"
                    f"{E_ID}: <code>{uid}</code>\n"
                    f"{E_CALENDAR} <b>Plan</b>: Gold (Vĩnh Viễn)\n"
                    f"{dns_text}"
                )

                if is_web:
                    db.update_web_activation(
                        web_activation_id,
                        status="success",
                        result=final_msg,
                        dns_link=link or "",
                        completed_at=int(time.time()),
                    )
                    # Keep the per-token cooldown, then move on.
                    await asyncio.sleep(45)
                    request_queue.task_done()
                    continue
                
                await asyncio.sleep(2.0)
                
                # Delete progress message and send photo with caption
                try:
                    await app.bot.delete_message(chat_id=chat_id, message_id=message_id)
                except:
                    pass

                reply_markup = None
                video_dns_id = db.get_config("video_dns_file_id", "")
                if video_dns_id:
                    reply_markup = InlineKeyboardMarkup([[
                        InlineKeyboardButton(T("btn_video_dns", lang), callback_data="dns_video")
                    ]])

                if should_send_donate_photo(cdk_source):
                    try:
                        current_photo = db.get_config("donate_photo", DONATE_PHOTO)
                        if not current_photo:
                            raise ValueError("donate photo is not configured")
                        await app.bot.send_photo(
                            chat_id=chat_id,
                            photo=current_photo,
                            caption=final_msg,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup
                        )
                    except Exception as e:
                        logger.error(f"Send photo error: {e}")
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=final_msg,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                            reply_markup=reply_markup
                        )
                else:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=final_msg,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                        reply_markup=reply_markup
                    )

                # Wait 45s for THIS token/worker
                await asyncio.sleep(45)
            else:
                if cdk_code:
                    db.release_cdk(cdk_code, user_id, secret=CDK_SECRET)
                if is_web:
                    db.update_web_activation(
                        web_activation_id,
                        status="failed",
                        result=msg_result,
                        completed_at=int(time.time()),
                    )
                    request_queue.task_done()
                    continue
                final_msg = f"{T('fail_title', lang)}\nInfo:\n<code>{msg_result}</code>"
                await edit(final_msg)
                
            request_queue.task_done()
            
        except Exception as e:
            logger.error(f"Worker #{worker_id} Exception: {e}")
            if item is not None:
                if item.get('cdk'):
                    db.release_cdk(item['cdk'], item['user_id'], secret=CDK_SECRET)
                request_queue.task_done()

def get_main_menu_keyboard(lang, user_id=None):
    rows = [
        [InlineKeyboardButton(T("btn_input", lang), callback_data="menu_input")],
        [InlineKeyboardButton(T("btn_buy_cdk", lang), callback_data="buy_cdk")],
        [InlineKeyboardButton(T("btn_dns", lang), callback_data="menu_dns")],
        [InlineKeyboardButton(T("btn_guide", lang), callback_data="menu_guide")],
        [InlineKeyboardButton(T("btn_lang", lang), callback_data="menu_lang"),
         InlineKeyboardButton(T("btn_help", lang), callback_data="menu_help")]
    ]
    if user_id == ADMIN_ID:
        rows.insert(2, [InlineKeyboardButton(T("btn_cdk_admin", lang), callback_data="menu_cdk")])
    return InlineKeyboardMarkup(rows)

def get_reply_keyboard(lang, user_id=None):
    rows = [
        [KeyboardButton(T("btn_input", lang)), KeyboardButton(T("btn_dns", lang))],
        [KeyboardButton(T("btn_buy_cdk", lang))],
        [KeyboardButton(T("btn_guide", lang)), KeyboardButton(T("btn_lang", lang))],
        [KeyboardButton(T("btn_help", lang))]
    ]
    if user_id == ADMIN_ID:
        rows.append([KeyboardButton(T("btn_cdk_admin", lang))])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="🔎 Nhập Username Locket..."
    )

def get_cdk_contact_keyboard():
    """Open the in-bot CDK purchase flow; no external support link."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛒 Mua CDK", callback_data="buy_cdk")
    ]])

async def run_menu_action(action, update, context, lang):
    """Shared action runner for both Inline buttons (callback) and Reply Keyboard buttons (text)."""
    query = update.callback_query
    user_id = update.effective_user.id
    if query:
        try:
            await query.answer()
        except:
            pass

    if action == "menu_input":
        context.user_data["awaiting_username"] = True
        if query:
            await query.message.reply_text(
                T("prompt_input", lang),
                parse_mode=ParseMode.HTML,
                reply_markup=ForceReply(input_field_placeholder="Username...")
            )
        else:
            await update.message.reply_text(
                T("prompt_input", lang),
                parse_mode=ParseMode.HTML,
                reply_markup=ForceReply(input_field_placeholder="Username...")
            )
        return

    if action == "menu_dns":
        if query:
            await query.message.reply_text(T("dns_creating", lang), parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(T("dns_creating", lang), parse_mode=ParseMode.HTML)
        pid, link = await create_profile_rotating(profile_name="LocketVIP-Permanent")
        if link:
            if query:
                await query.message.reply_text(
                    T("dns_permanent", lang).format(link, pid),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
            else:
                await update.message.reply_text(
                    T("dns_permanent", lang).format(link, pid),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
        else:
            if query:
                await query.message.reply_text(T("dns_error", lang), parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(T("dns_error", lang), parse_mode=ParseMode.HTML)
        return

    if action == "menu_guide":
        guide_text = T("guide_msg", lang)
        video_file_id = db.get_config("video_file_id", VIDEO_FILE_ID)
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
        target = query.message if query else update.message
        if video_file_id:
            try:
                await target.reply_video(
                    video=video_file_id,
                    caption=guide_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=back_kb
                )
                return
            except Exception:
                pass
        await target.reply_text(guide_text, parse_mode=ParseMode.HTML, reply_markup=back_kb)
        return

    if action == "menu_lang":
        await show_language_select(update)
        return

    if action == "menu_help":
        help_text = T("help_msg", lang)
        if user_id == ADMIN_ID:
            help_text += T("admin_help", lang)
        target = query.message if query else update.message
        await target.reply_text(
            help_text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
        )
        return

    if action == "menu_cdk":
        if user_id != ADMIN_ID:
            return
        context.user_data["awaiting_cdk_qty"] = True
        target = query.message if query else update.message
        await target.reply_text(T("cdk_qty_prompt", lang), parse_mode=ParseMode.HTML)
        return

    if action == "buy_cdk":
        target = query.message if query else update.message
        if payment_config_errors():
            await target.reply_text(T("buy_cdk_config_error", lang), parse_mode=ParseMode.HTML)
            return
        await target.reply_text(
            T("buy_cdk_title", lang),
            parse_mode=ParseMode.HTML,
            reply_markup=get_buy_cdk_keyboard(lang),
        )
        return

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Catch-all so a transient Telegram network blip doesn't dump a full traceback."""
    err = context.error
    # TimedOut / NetworkError are transient (slow or dropped connection to Telegram).
    # The update is simply skipped; the user can tap the button again.
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning(f"{Clr.WARNING}[Network] Telegram timeout (transient): {err}{Clr.ENDC}")
        return
    logger.error(f"{Clr.FAIL}[Error] Unhandled exception:{Clr.ENDC} {err}", exc_info=err)

def run_bot():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")
    if ADMIN_ID <= 0:
        raise RuntimeError("ADMIN_ID environment variable must be a positive Telegram user ID")
    if not TOKEN_SETS:
        raise RuntimeError("TOKEN_SETS_JSON environment variable is required")
    if not REVENUECAT_APP_KEY:
        raise RuntimeError("REVENUECAT_APP_KEY environment variable is required")
    db.init_db()
    logging.basicConfig(
        format='%(message)s',
        level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.ERROR)
    logging.getLogger("telegram").setLevel(logging.ERROR)
    logging.getLogger("aiohttp").setLevel(logging.ERROR)

    builder = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(20.0)        # default 5s is too tight for a flaky route to Telegram
        .read_timeout(20.0)
        .write_timeout(20.0)
        .pool_timeout(20.0)
        .connection_pool_size(16)     # default 1 — shared by poller + workers
        .get_updates_connect_timeout(20.0)
        .get_updates_read_timeout(40.0)
    )
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
        print("🌐 Using configured proxy for Telegram")
    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("muacdk", buy_cdk_command))
    app.add_handler(CommandHandler("setlang", setlang_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("noti", noti_command))
    app.add_handler(CommandHandler("rs", reset_command))
    app.add_handler(CommandHandler("setdonate", set_donate_command))
    app.add_handler(CommandHandler("setvideo", set_video_command))
    app.add_handler(CommandHandler("setvideodns", set_video_dns_command))
    app.add_handler(CommandHandler("stats", stats_command))
    
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(error_handler)

    async def post_init(application):
        bot = application.bot
        # 0) Wipe EVERY command scope (old commands set by BotFather / older builds
        #    leak through per-scope). Covers default, all chats/groups/admins, the
        #    admin chat, and language-specific scopes.
        wipe_scopes = [
            BotCommandScopeDefault(),
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllGroupChats(),
            BotCommandScopeAllChatAdministrators(),
            BotCommandScopeChat(chat_id=ADMIN_ID),
        ]
        for _scope in wipe_scopes:
            try:
                await bot.delete_my_commands(scope=_scope)
            except Exception:
                pass
        for _lang in ("vi", "en", "ru", "es", "pt", "id", "th", "zh", "ko"):
            try:
                await bot.delete_my_commands(scope=BotCommandScopeDefault(), language_code=_lang)
            except Exception:
                pass

        # 1) Commands — default scope for users, extended scope for the admin chat.
        user_cmds = [
            BotCommand("start", "Khởi động bot & Menu chính"),
            BotCommand("menu", "Mở Menu chính"),
            BotCommand("muacdk", "Mua CDK (chọn 1-5 mã)"),
            BotCommand("help", "Xem trợ giúp"),
            BotCommand("setlang", "Đổi ngôn ngữ (VI/EN)"),
        ]
        admin_cmds = [
            BotCommand("stats", "Xem thống kê hệ thống"),
            BotCommand("noti", "Gửi thông báo tới tất cả user"),
            BotCommand("rs", "Reset lượt dùng cho user"),
            BotCommand("setdonate", "Đặt ảnh thành công"),
            BotCommand("setvideo", "Đặt video hướng dẫn"),
            BotCommand("setvideodns", "Đặt video cài DNS"),
        ]
        try:
            await bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
            await bot.set_my_commands(user_cmds + admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
            # 4) Menu Button — tapping it opens the bot's command list.
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception as e:
            logger.error(f"set_my_commands error: {e}")

        # Dynamically create workers based on config
        for i in range(1, NUM_WORKERS + 1):
            asyncio.create_task(queue_worker(application, i))
        asyncio.create_task(web_activation_poller(application))
        if not payment_config_errors():
            asyncio.create_task(cdk_payment_poller(application))
        else:
            logger.warning("CDK payment flow disabled; missing/invalid ENV: %s", ", ".join(payment_config_errors()))

    app.post_init = post_init
    print(f"Bot is running... ({NUM_WORKERS} workers)")
    app.run_polling()
