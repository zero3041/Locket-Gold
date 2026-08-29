import json
import os
import re
from pathlib import Path


def _load_dotenv(path):
    """Load a local ignored .env while preserving process-level overrides."""
    dotenv_path = Path(path)
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _env_int(name, default=0, minimum=None):
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else int(default)
    except (TypeError, ValueError):
        return int(default)
    if minimum is not None and value < minimum:
        return int(default)
    return value

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
NEXTDNS_KEY = os.environ.get("NEXTDNS_KEY", "").strip()
REVENUECAT_APP_KEY = os.environ.get("REVENUECAT_APP_KEY", "").strip()

# Multiple NextDNS keys (each from a different NextDNS account) for load sharing.
# NEXTDNS_KEYS env (comma separated) overrides the list below.
# The bot rotates through them and auto-fails-over when a key errors.
NEXTDNS_KEYS = [NEXTDNS_KEY] if NEXTDNS_KEY else []
env_nextdns_keys = [k.strip() for k in os.environ.get("NEXTDNS_KEYS", "").split(",") if k.strip()]
if env_nextdns_keys:
    NEXTDNS_KEYS = env_nextdns_keys

# Optional outbound proxy for reaching Telegram when the direct route is slow or
# blocked, e.g. "http://127.0.0.1:1080" or "socks5://user:pass@host:port".
# Leave unset for a direct connection. SOCKS proxies require: pip install "httpx[socks]"
PROXY_URL = os.environ.get("PROXY_URL", "").strip() or None

def _load_token_sets():
    raw = os.environ.get("TOKEN_SETS_JSON", "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("TOKEN_SETS_JSON must be valid JSON") from exc
    if not isinstance(parsed, list) or not parsed:
        raise RuntimeError("TOKEN_SETS_JSON must be a non-empty JSON array")
    valid_items = all(
        isinstance(item, dict)
        and isinstance(item.get("fetch_token"), str)
        and bool(item["fetch_token"].strip())
        and isinstance(item.get("app_transaction"), str)
        and bool(item["app_transaction"].strip())
        and isinstance(item.get("is_sandbox"), bool)
        for item in parsed
    )
    if not valid_items:
        raise RuntimeError(
            "Each TOKEN_SETS_JSON item requires non-empty fetch_token/app_transaction "
            "strings and a boolean is_sandbox"
        )
    return parsed


TOKEN_SETS = _load_token_sets()


ADMIN_ID = _env_int("ADMIN_ID")
NUM_WORKERS = _env_int("NUM_WORKERS", 2, minimum=1)
DONATE_PHOTO = os.environ.get("DONATE_PHOTO", "").strip()
VIDEO_FILE_ID = os.environ.get("VIDEO_FILE_ID", "").strip()

# Payment/CDK secrets intentionally have no source-code fallback.  Configure
# them in the process environment (see .env.example); the bot keeps non-payment
# features available and shows a configuration error if /muacdk is used before
# all required values are present.
SEPAY_API_TOKEN = os.environ.get(
    "SEPAY_API_TOKEN", os.environ.get("SEPAY_API_KEY", "")
).strip()
SEPAY_API_URL = "https://userapi.sepay.vn/v2/transactions"
BANK_BIN = os.environ.get("BANK_BIN", "").strip()
BANK_ACCOUNT = os.environ.get("BANK_ACCOUNT", "").strip()
BANK_NAME = os.environ.get("BANK_NAME", "").strip()
BANK_OWNER = os.environ.get("BANK_OWNER", "").strip()
CDK_SECRET = os.environ.get("CDK_SECRET", "").strip()
CDK_UNIT_PRICE = _env_int("CDK_UNIT_PRICE")
CDK_ORDER_TIMEOUT_MINUTES = max(
    5, _env_int("CDK_ORDER_TIMEOUT_MINUTES", 20, minimum=5)
)
SEPAY_POLL_INTERVAL_SECONDS = max(
    15, _env_int("SEPAY_POLL_INTERVAL_SECONDS", 30, minimum=15)
)
CDK_ORDER_CREATE_MAX = _env_int("CDK_ORDER_CREATE_MAX", 3, minimum=1)
CDK_ORDER_CREATE_WINDOW_SECONDS = _env_int(
    "CDK_ORDER_CREATE_WINDOW_SECONDS", 300, minimum=10
)
CDK_MANUAL_CHECK_MAX = _env_int("CDK_MANUAL_CHECK_MAX", 3, minimum=1)
CDK_MANUAL_CHECK_WINDOW_SECONDS = _env_int(
    "CDK_MANUAL_CHECK_WINDOW_SECONDS", 60, minimum=10
)
SEPAY_GLOBAL_CHECK_MAX = _env_int("SEPAY_GLOBAL_CHECK_MAX", 60, minimum=1)
SEPAY_GLOBAL_CHECK_WINDOW_SECONDS = _env_int(
    "SEPAY_GLOBAL_CHECK_WINDOW_SECONDS", 60, minimum=10
)
SEPAY_MAX_ORDERS_PER_POLL = _env_int("SEPAY_MAX_ORDERS_PER_POLL", 20, minimum=1)
CDK_RESERVATION_TTL_SECONDS = max(
    3600, _env_int("CDK_RESERVATION_TTL_SECONDS", 21600, minimum=3600)
)

# Web store (web_store.py)
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"
WEB_PORT = _env_int("WEB_PORT", 8080, minimum=1)
WEB_ADMIN_USER = os.environ.get("WEB_ADMIN_USER", "admin").strip() or "admin"
# Prefer a PBKDF2-SHA256 hash (pbkdf2$iterations$salt_b64$hash_b64); plaintext
# WEB_ADMIN_PASSWORD is still accepted as a fallback (legacy setups).
WEB_ADMIN_PASSWORD = os.environ.get("WEB_ADMIN_PASSWORD", "").strip()
WEB_ADMIN_PASSWORD_HASH = os.environ.get("WEB_ADMIN_PASSWORD_HASH", "").strip()
# Falls back to CDK_SECRET when unset (must stay stable across restarts).
WEB_SESSION_SECRET = (
    os.environ.get("WEB_SESSION_SECRET", "").strip() or CDK_SECRET
)


def payment_config_errors():
    required = {
        "SEPAY_API_TOKEN": SEPAY_API_TOKEN,
        "BANK_BIN": BANK_BIN,
        "BANK_ACCOUNT": BANK_ACCOUNT,
        "BANK_NAME": BANK_NAME,
        "BANK_OWNER": BANK_OWNER,
        "CDK_SECRET": CDK_SECRET,
        "CDK_UNIT_PRICE": CDK_UNIT_PRICE,
    }
    errors = [name for name, value in required.items() if not value]
    if CDK_SECRET and len(CDK_SECRET) < 32:
        errors.append("CDK_SECRET(min_32_chars)")
    if BANK_BIN and not BANK_BIN.isdigit():
        errors.append("BANK_BIN(digits_only)")
    if BANK_ACCOUNT and not BANK_ACCOUNT.isdigit():
        errors.append("BANK_ACCOUNT(digits_only)")
    if CDK_UNIT_PRICE < 0:
        errors.append("CDK_UNIT_PRICE(positive_integer)")
    return errors

E_LOADING = '<tg-emoji emoji-id="5350752364246606166">✍️</tg-emoji>'
E_LIMIT   = '<tg-emoji emoji-id="5424857974784925603">🚫</tg-emoji>'
E_SUCCESS = '<tg-emoji emoji-id="5260463209562776385">✅</tg-emoji>'
E_ERROR   = '<tg-emoji emoji-id="5318840353510408444">🔴</tg-emoji>'
E_TIP     = '<tg-emoji emoji-id="4968003407315993509">💡</tg-emoji>'
E_MENU    = '<tg-emoji emoji-id="5449601904147440135">👑</tg-emoji>'

E_USER    = '<tg-emoji emoji-id="5974048815789903111">👤</tg-emoji>'
E_ID      = '<tg-emoji emoji-id="5974526806995242353">🆔</tg-emoji>'
E_TAG     = '<tg-emoji emoji-id="5240228673738527951">🏷️</tg-emoji>'
E_STAT    = '<tg-emoji emoji-id="4967519884192777037">📊</tg-emoji>'
E_GLOBE   = '<tg-emoji emoji-id="5231489647946768652">🌐</tg-emoji>'
E_SOS     = '<tg-emoji emoji-id="6301027265899661025">🆘</tg-emoji>'
E_SHIELD  = '<tg-emoji emoji-id="5352888345972187597">🛡️</tg-emoji>'
E_CALENDAR = '<tg-emoji emoji-id="5413879192267805083">📅</tg-emoji>'
E_IOS     = '<tg-emoji emoji-id="5350556204500263431">🍏</tg-emoji>'
E_ANDROID = '<tg-emoji emoji-id="5303145396254563405">🤖</tg-emoji>'


DEFAULT_LANG = "VI"

TEXTS = {
    "VI": {
        "welcome": f"{E_SUCCESS} <b>Locket Gold Activator</b>\n\nChào mừng! Vui lòng chọn ngôn ngữ hoặc sử dụng menu bên dưới.",
        "menu_msg": f"{E_MENU} <b>Bảng Điều Khiển</b>\n\n👇 Bấm nút bên dưới để nhập Username kích hoạt Gold.",
        "btn_input": "🔑 Nhập User Locket",
        "btn_lang": "🌐 Đổi Ngôn Ngữ",
        "btn_help": "🆘 Hỗ Trợ",
        "btn_guide": "📺 Hướng Dẫn Sử Dụng",
        "btn_cdk_user": "🎟️ Nhập CDK",
        "btn_cdk_admin": "🎟️ Tạo CDK",
        "btn_buy_cdk": "🛒 Mua CDK",
        "buy_cdk_title": "🛒 <b>MUA CDK</b>\n\nChọn số lượng CDK cần mua (1-5):",
        "buy_cdk_config_error": f"{E_ERROR} Thanh toán đang tạm khóa do thiếu cấu hình. Vui lòng báo admin.",
        "buy_cdk_not_found": f"{E_ERROR} Đơn không tồn tại, không thuộc tài khoản này hoặc đã được xử lý.",
        "buy_cdk_waiting": "⏳ Chưa nhận được giao dịch phù hợp. Vui lòng thử lại sau ít phút.",
        "buy_cdk_paid": f"{E_SUCCESS} <b>THANH TOÁN THÀNH CÔNG</b>\n\nCDK của bạn:\n\n<pre>{{codes}}</pre>\n\nMỗi CDK chỉ dùng được một lần.",
        "btn_video_dns": "📺 Xem Video Cài DNS",
        "cdk_qty_prompt": f"{E_LOADING} Nhập <b>số lượng CDK</b> cần tạo (tối đa 500):",
        "cdk_qty_invalid": f"{E_ERROR} Số lượng không hợp lệ. Vui lòng nhập số từ 1-500.",
        "cdk_generated": f"{E_SUCCESS} <b>Đã tạo {{n}} CDK:</b>\n\n<pre>{{codes}}</pre>",
        "cdk_done_header": f"{E_SUCCESS} <b>Đã tạo {{n}} CDK</b> — bấm vào từng mã để copy:",
        "cdk_btn_copy": "📋 Sao Chép Codes",
        "cdk_prompt_user": (
            f"{E_LOADING} Bạn cần có <b>CDK</b> để kích hoạt Gold.\n"
            f"Vui lòng nhập mã CDK của bạn vào tin nhắn trả lời bên dưới:"
        ),
        "cdk_invalid": f"{E_ERROR} CDK không hợp lệ hoặc đã được sử dụng. Vui lòng kiểm tra lại.",
        "cdk_rate_limited": f"{E_LIMIT} Bạn nhập sai quá nhiều lần. Vui lòng thử lại sau 5 phút.",
        "cdk_switch_uid": f"{E_LOADING} Tài khoản này đã kích hoạt cho Locket: <code>{{0}}</code>.\nĐể kích hoạt Locket mới, vui lòng nhập <b>CDK mới</b>:",
        "cdk_success": f"{E_SUCCESS} <b>CDK hợp lệ!</b> Giờ nhập Username Locket của bạn:",
        "cdk_valid": f"{E_SUCCESS} <b>CDK hợp lệ!</b> Đang kích hoạt Gold...",
        "cdk_need_first": f"{E_LIMIT} Bạn cần nhập <b>CDK</b> trước khi kích hoạt. Bấm nút '🎟️ Nhập CDK' ở menu.",
        "cdk_stolen": f"{E_ERROR} Không có CDK để tạo hoặc đã hết lượt.",
        "guide_msg": (
            f"{E_MENU} <b>HƯỚNG DẪN SỬ DỤNG</b>\n\n"
            f"1️⃣ Bấm <b>'🔑 Nhập User Locket'</b> trong menu.\n"
            f"2️⃣ Nhập <b>Username</b> hoặc <b>Link Locket</b> của bạn.\n"
            f"3️⃣ Chờ bot xử lý — vị trí trong hàng chờ sẽ được thông báo.\n"
            f"4️⃣ Khi thấy <b>KÍCH HOẠT THÀNH CÔNG</b> → mở app Locket kiểm tra Gold.\n"
            f"5️⃣ Bấm <b>'🛡️ Tạo DNS Chặn'</b> và cài DNS ngay để Gold không bị mất.\n\n"
            f"{E_TIP} Nếu cần, xem video hướng dẫn bên dưới!"
        ),
        "btn_dns": "🛡️ Tạo DNS Chặn (Free)",
        "dns_creating": f"{E_SHIELD} <b>Đang tạo DNS chặn vĩnh viễn...</b>",
        "dns_error": f"{E_ERROR} Lỗi tạo DNS. Vui lòng thử lại sau hoặc kiểm tra NextDNS Key.",
        "dns_permanent": (
            f"{E_SHIELD} <b>DNS CHẶN VĨNH VIỄN ĐÃ SẴN SÀNG</b>\n"
            f"(Chống mất Gold — cài 1 lần, dùng mãi, không giới hạn thời gian)\n\n"
            f"{E_IOS} <b>iOS</b>: <a href='{{}}'>Bấm vào đây để cài</a>\n"
            f"(Mở bằng <b>Safari</b> → Cho phép → Cài đặt Profile)\n\n"
            f"{E_ANDROID} <b>Android</b>: <code>{{}}.dns.nextdns.io</code>\n"
            f"(Cài đặt → Mạng → Private DNS → dán chuỗi trên)\n\n"
            f"{E_TIP} <b>Lưu ý</b>: Cài lúc nào cũng được — DNS này chặn vĩnh viễn để không bị mất Gold!"
        ),
        "prompt_input": f"{E_LOADING} Vui lòng nhập <b>Username</b> hoặc <b>Link Locket</b> của bạn vào tin nhắn trả lời bên dưới:",
        "lang_select": "🌐 Vui lòng chọn ngôn ngữ / Please select language:",
        "lang_set": f"{E_SUCCESS} Đã cài đặt ngôn ngữ: Tiếng Việt",
        "help_msg": (
            f"<b>{E_MENU} Danh Sách Lệnh:</b>\n\n"
            f"/start - Khởi động bot & Menu chính\n"
            f"/setlang - Đổi ngôn ngữ (VI/EN)\n"
            f"/help - Xem trợ giúp này\n\n"
            f"<b>{E_TIP} Cách dùng:</b>\n"
            f"1. Bấm nút '🔑 Nhập User Locket'\n"
            f"2. Điền Username hoặc Link\n"
            f"3. Bot sẽ kiểm tra và kích hoạt Gold."
        ),
        "admin_help": (
            f"\n\n<b>👑 Admin Control:</b>\n"
            f"/stats - Xem thống kê hệ thống\n"
            f"/noti [msg] - Gửi thông báo tới tất cả user\n"
            f"/rs [id] - Reset lượt dùng cho user\n"
             f"/setdonate - Đặt ảnh thành công (reply vào ảnh)\n"
             f"/setvideo - Đặt video hướng dẫn (reply vào video)\n"
             f"/setvideodns - Đặt video cài DNS (reply vào video)\n"
            f"🎟️ Tạo CDK - từ menu chính (admin)"
        ),
        "resolving": f"{E_LOADING} <b>Đang phân giải UID...</b>",
        "not_found": f"{E_ERROR} Không tìm thấy User.",
        "limit_reached": f"{E_LIMIT} Đã đạt giới hạn request (5/5).",
        "queue_almost": f"{E_LOADING} <b>Sắp đến lượt bạn!</b>\nCòn <b>2 người</b> nữa là đến lượt bạn. Hãy chuẩn bị sẵn sàng! 🚀",
        "admin_noti_sent": f"{E_SUCCESS} Đã gửi thông báo đến tất cả user.",
        "admin_reset": f"{E_SUCCESS} Đã reset lượt dùng cho user {{}}.",
        "admin_only": f"{E_ERROR} Bạn không có quyền sử dụng lệnh này.",
        "checking_status": f"{E_LOADING} <b>Đang kiểm tra Entitlement...</b>",
        "free_status": "Free (Chưa Active)",
        "gold_active": f"{E_SUCCESS} <b>Gold Đã Active</b> (Hết hạn: {{}})",
        "user_info_title": f"{E_USER} <b>User Information</b>",
        "btn_upgrade": "🚀 KÍCH HOẠT NGAY",
        "queued": f"{E_LOADING} <b>Đã thêm vào hàng chờ</b>\nTarget: <code>{{0}}</code>\nVị trí: <b>#{{1}}</b> (Còn {{2}} người trước bạn)...",
        "processing": (
            f"{E_LOADING} <b>⚡ SYSTEM EXPLOIT RUNNING...</b>\n"
            f"<pre>"
            f"[*] Target:  {{}}\n"
            f"[*] Method:  RevenueCat_Bypass_v2\n"
            f"[>] Action:  Injecting Malicious Receipt\n"
            f"[>] Status:  Bypassing Validation...\n"
            f"[?] Waiting: Server Response..."
            f"</pre>"
        ),
        "success_title": f"{E_SUCCESS} <b>KÍCH HOẠT THÀNH CÔNG</b>",
        "generating_dns": f"{E_SHIELD} Đang tạo Anti-Revoke DNS...",
        "fail_title": f"{E_ERROR} <b>Kích hoạt thất bại</b>",
        "dns_msg": (
            f"{E_SHIELD} <b>HƯỚNG DẪN QUAN TRỌNG</b>:\n"
            f"1️⃣ Vào App Locket kiểm tra đã có <b>Gold</b> chưa.\n"
            f"2️⃣ Nếu đã có, tiến hành <b>CÀI DNS NGAY</b> (trong 45s):\n\n"
            f"{E_IOS} <b>iOS</b>: <a href='{{}}'>Bấm vào đây để cài</a>\n"
            f"(Mở link bằng <b>Safari</b> -> Cho phép -> Cài đặt Profile)\n\n"
            f"{E_ANDROID} <b>Android</b>: <code>{{}}.dns.nextdns.io</code>\n"
            f"(Cài đặt → Mạng → Private DNS)\n\n"
            f"{E_TIP} <b>Lưu ý</b>: Bắt buộc cài DNS để không bị mất Gold!"
        )
    },
    "EN": {
        "welcome": f"{E_SUCCESS} <b>Locket Gold Activator</b>\n\nWelcome! Please select your language or use the menu below.",
        "menu_msg": f"{E_MENU} <b>Control Panel</b>\n\n👇 Click the button below to enter Username.",
        "btn_input": "🔑 Input Locket User",
        "btn_lang": "🌐 Change Language",
        "btn_help": "🆘 Help",
        "btn_guide": "📺 Usage Guide",
        "btn_cdk_user": "🎟️ Enter CDK",
        "btn_cdk_admin": "🎟️ Generate CDK",
        "btn_buy_cdk": "🛒 Buy CDK",
        "buy_cdk_title": "🛒 <b>BUY CDK</b>\n\nChoose the number of CDKs (1-5):",
        "buy_cdk_config_error": f"{E_ERROR} Payments are temporarily unavailable because configuration is incomplete.",
        "buy_cdk_not_found": f"{E_ERROR} This order does not exist, belongs to another user, or was already processed.",
        "buy_cdk_waiting": "⏳ No matching payment yet. Please try again in a few minutes.",
        "buy_cdk_paid": f"{E_SUCCESS} <b>PAYMENT CONFIRMED</b>\n\nYour CDKs:\n\n<pre>{{codes}}</pre>\n\nEach CDK can only be used once.",
        "btn_video_dns": "📺 Watch DNS Setup Video",
        "cdk_qty_prompt": f"{E_LOADING} Enter the <b>number of CDKs</b> to generate (max 500):",
        "cdk_qty_invalid": f"{E_ERROR} Invalid quantity. Enter a number from 1-500.",
        "cdk_generated": f"{E_SUCCESS} <b>Generated {{n}} CDKs:</b>\n\n<pre>{{codes}}</pre>",
        "cdk_done_header": f"{E_SUCCESS} <b>Generated {{n}} CDKs</b> — tap each code to copy:",
        "cdk_btn_copy": "📋 Copy Codes",
        "cdk_prompt_user": (
            f"{E_LOADING} You need a <b>CDK</b> to activate Gold.\n"
            f"Enter your CDK code in the reply below:"
        ),
        "cdk_invalid": f"{E_ERROR} Invalid or already used CDK. Please check again.",
        "cdk_rate_limited": f"{E_LIMIT} Too many attempts. Please try again in 5 minutes.",
        "cdk_switch_uid": f"{E_LOADING} This account already activated these Lockets: <code>{{0}}</code>.\nTo activate a new Locket, enter a <b>new CDK</b>:",
        "cdk_success": f"{E_SUCCESS} <b>Valid CDK!</b> Now enter your Locket username:",
        "cdk_valid": f"{E_SUCCESS} <b>Valid CDK!</b> Activating Gold...",
        "cdk_need_first": f"{E_LIMIT} You need to enter a <b>CDK</b> first. Tap '🎟️ Enter CDK' in the menu.",
        "cdk_stolen": f"{E_ERROR} No CDKs to generate or limit reached.",
        "guide_msg": (
            f"{E_MENU} <b>USAGE GUIDE</b>\n\n"
            f"1️⃣ Tap <b>'🔑 Enter Locket User'</b> in the menu.\n"
            f"2️⃣ Enter your <b>Username</b> or <b>Locket link</b>.\n"
            f"3️⃣ Wait for the bot — your queue position will be shown.\n"
            f"4️⃣ When you see <b>ACTIVATION SUCCESSFUL</b> → open Locket and check Gold.\n"
            f"5️⃣ Tap <b>'🛡️ Create Block DNS'</b> and install the DNS right away so Gold stays active.\n\n"
            f"{E_TIP} Watch the guide video below if needed!"
        ),
        "btn_dns": "🛡️ Create Block DNS (Free)",
        "dns_creating": f"{E_SHIELD} <b>Creating permanent block DNS...</b>",
        "dns_error": f"{E_ERROR} DNS creation failed. Please try again or check your NextDNS Key.",
        "dns_permanent": (
            f"{E_SHIELD} <b>PERMANENT BLOCK DNS READY</b>\n"
            f"(Anti-revoke — install once, use forever, no time limit)\n\n"
            f"{E_IOS} <b>iOS</b>: <a href='{{}}'>Click here to install</a>\n"
            f"(Open in <b>Safari</b> → Allow → Install Profile)\n\n"
            f"{E_ANDROID} <b>Android</b>: <code>{{}}.dns.nextdns.io</code>\n"
            f"(Settings → Network → Private DNS → paste the string above)\n\n"
            f"{E_TIP} <b>Note</b>: Install anytime — this DNS blocks permanently to keep Gold!"
        ),
        "prompt_input": f"{E_LOADING} Please enter your <b>Username</b> or <b>Locket Link</b> in the reply below:",
        "lang_select": "🌐 Please select language:",
        "lang_set": f"{E_SUCCESS} Language set: English",
        "help_msg": (
            f"<b>{E_MENU} Commands:</b>\n\n"
            f"/start - Main Menu\n"
            f"/setlang - Change Language\n"
            f"/help - Show this help\n\n"
            f"<b>{E_TIP} How to use:</b>\n"
            f"1. Click '🔑 Input Locket User'\n"
            f"2. Enter Username or Link\n"
            f"3. Bot will activate Gold."
        ),
        "admin_help": (
            f"\n\n<b>👑 Admin Control:</b>\n"
            f"/stats - View system statistics\n"
            f"/noti [msg] - Broadcast message to all users\n"
            f"/rs [id] - Reset user usage limit\n"
             f"/setdonate - Set success photo (reply to a photo)\n"
             f"/setvideo - Set guide video (reply to a video)\n"
             f"/setvideodns - Set DNS guide video (reply to a video)\n"
            f"🎟️ Generate CDK - from main menu (admin)"
        ),
        "resolving": f"{E_LOADING} <b>Resolving UID...</b>",
        "not_found": f"{E_ERROR} User not found.",
        "limit_reached": f"{E_LIMIT} Daily limit reached (5/5).",
        "queue_almost": f"{E_LOADING} <b>Almost your turn!</b>\n<b>2 people</b> ahead of you. Get ready! 🚀",
        "admin_noti_sent": f"{E_SUCCESS} Notification sent to all users.",
        "admin_reset": f"{E_SUCCESS} Usage reset for user {{}}.",
        "admin_only": f"{E_ERROR} You don't have permission.",
        "checking_status": f"{E_LOADING} <b>Checking Entitlements...</b>",
        "free_status": "Free (Inactive)",
        "gold_active": f"{E_SUCCESS} <b>Gold Active</b> (Exp: {{}})",
        "user_info_title": f"{E_USER} <b>User Information</b>",
        "btn_upgrade": "🚀 ACTIVATE NOW",
        "queued": f"{E_LOADING} <b>Added to Queue</b>\nTarget: <code>{{0}}</code>\nPosition: <b>#{{1}}</b> ({{2}} people ahead)...",
        "processing": (
            f"{E_LOADING} <b>⚡ SYSTEM EXPLOIT RUNNING...</b>\n"
            f"<pre>"
            f"[*] Target:  {{}}\n"
            f"[*] Method:  RevenueCat_Bypass_v2\n"
            f"[>] Action:  Injecting Malicious Receipt\n"
            f"[>] Status:  Bypassing Validation...\n"
            f"[?] Waiting: Server Response..."
            f"</pre>"
        ),
        "success_title": f"{E_SUCCESS} <b>ACTIVATION SUCCESSFUL</b>",
        "generating_dns": f"{E_SHIELD} Generating Anti-Revoke DNS...",
        "fail_title": f"{E_ERROR} <b>Activation Failed</b>",
        "dns_msg": (
            f"{E_SHIELD} <b>IMPORTANT INSTRUCTIONS</b>:\n"
            f"1️⃣ Check Locket App for <b>Gold</b> status.\n"
            f"2️⃣ If active, <b>INSTALL DNS IMMEDIATELY</b> (within 45s):\n\n"
            f"{E_IOS} <b>iOS</b>: <a href='{{}}'>Click to Install</a>\n"
            f"(Open link in <b>Safari</b> -> Allow -> Install Profile)\n\n"
            f"{E_ANDROID} <b>Android</b>: <code>{{}}.dns.nextdns.io</code>\n"
            f"(Settings → Network → Private DNS)\n\n"
            f"{E_TIP} <b>Note</b>: DNS is required to keep Gold active!"
        )
    }
}

def T(key, lang=None):
    if not lang:
        lang = DEFAULT_LANG
    return TEXTS.get(lang, TEXTS["VI"]).get(key, key)
