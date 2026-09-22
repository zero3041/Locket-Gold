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
REVENUECAT_APP_KEY = os.environ.get("REVENUECAT_APP_KEY", "").strip()

# Optional outbound proxy for reaching Telegram when the direct route is slow or
# blocked, e.g. "http://127.0.0.1:1080" or "socks5://user:pass@host:port".
# Leave unset for a direct connection. SOCKS proxies require: pip install "httpx[socks]"
PROXY_URL = os.environ.get("PROXY_URL", "").strip() or None

# Optional proxy dedicated to /chk bulk lookups so heavy scanning does not share
# the bot's direct IP reputation.
CHK_PROXY_URL = os.environ.get("CHK_PROXY_URL", "").strip() or None

ADMIN_ID = _env_int("ADMIN_ID")
DONATE_PHOTO = os.environ.get("DONATE_PHOTO", "").strip()
VIDEO_FILE_ID = os.environ.get("VIDEO_FILE_ID", "").strip()

# Payment/key secrets intentionally have no source-code fallback. Configure them
# in the process environment (see .env.example); the bot keeps non-payment
# features available and shows a configuration error when payment is required.
SEPAY_API_TOKEN = os.environ.get(
    "SEPAY_API_TOKEN", os.environ.get("SEPAY_API_KEY", "")
).strip()
SEPAY_API_URL = "https://userapi.sepay.vn/v2/transactions"
BANK_BIN = os.environ.get("BANK_BIN", "").strip()
BANK_ACCOUNT = os.environ.get("BANK_ACCOUNT", "").strip()
BANK_NAME = os.environ.get("BANK_NAME", "").strip()
BANK_OWNER = os.environ.get("BANK_OWNER", "").strip()
CDK_SECRET = os.environ.get("CDK_SECRET", "").strip()

# Key prices. CDK_UNIT_PRICE is the 1-month price; the 1-year price defaults to
# five times the monthly price when CDK_UNIT_PRICE_1Y is not configured.
CDK_UNIT_PRICE = _env_int("CDK_UNIT_PRICE")
CDK_UNIT_PRICE_1Y = _env_int(
    "CDK_UNIT_PRICE_1Y", CDK_UNIT_PRICE * 5 if CDK_UNIT_PRICE else 0
)

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

# Source pool: a source is ignored/dropped when its Gold has fewer days left.
GOLD_MIN_SOURCE_DAYS = max(1, _env_int("GOLD_MIN_SOURCE_DAYS", 10, minimum=1))
# Free re-activation on the web store for UIDs that were activated before.
FREE_REACTIVATE_COOLDOWN_MINUTES = max(0, _env_int("FREE_REACTIVATE_COOLDOWN_MINUTES", 30, minimum=0))
FREE_REACTIVATE_DAILY_MAX = max(1, _env_int("FREE_REACTIVATE_DAILY_MAX", 5, minimum=1))
# Bulk /chk and /scan limits.
CHK_MAX_LINES = max(10, _env_int("CHK_MAX_LINES", 500, minimum=10))
SCAN_MAX_COMMENTS = max(50, _env_int("SCAN_MAX_COMMENTS", 1000, minimum=50))
SCAN_MAX_CONCURRENT = max(1, _env_int("SCAN_MAX_CONCURRENT", 2, minimum=1))
CHECK_MAX_CONCURRENT = max(1, _env_int("CHECK_MAX_CONCURRENT", 4, minimum=1))

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


def price_for_plan(plan):
    return CDK_UNIT_PRICE_1Y if (plan or "").lower() == "1y" else CDK_UNIT_PRICE


def payment_config_errors():
    required = {
        "SEPAY_API_TOKEN": SEPAY_API_TOKEN,
        "BANK_BIN": BANK_BIN,
        "BANK_ACCOUNT": BANK_ACCOUNT,
        "BANK_NAME": BANK_NAME,
        "BANK_OWNER": BANK_OWNER,
        "CDK_SECRET": CDK_SECRET,
        "CDK_UNIT_PRICE": CDK_UNIT_PRICE,
        "CDK_UNIT_PRICE_1Y": CDK_UNIT_PRICE_1Y,
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
    if CDK_UNIT_PRICE_1Y < 0:
        errors.append("CDK_UNIT_PRICE_1Y(positive_integer)")
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
E_KEY     = '<tg-emoji emoji-id="5449601904147440135">🎟️</tg-emoji>'
E_CALENDAR = '<tg-emoji emoji-id="5413879192267805083">📅</tg-emoji>'
E_MONEY   = '<tg-emoji emoji-id="5350752364246606166">💰</tg-emoji>'

PLAN_LABELS = {
    "VI": {"1m": "Gói Vĩnh Viễn", "1y": "Gói 1 Năm"},
    "EN": {"1m": "Permanent Plan", "1y": "1-Year Plan"},
}

DEFAULT_LANG = "VI"

TEXTS = {
    "VI": {
        "welcome": f"{E_SUCCESS} <b>Locket Gold &amp; Services</b>\n\nChào mừng! Chọn ngôn ngữ hoặc dùng menu bên dưới.",
        "menu_msg": f"{E_MENU} <b>Bảng Điều Khiển</b>\n\n👇 Chọn chức năng bên dưới.",
        "btn_input": "🔍 Kiểm Tra Gold",
        "btn_redeem": "🎟️ Kích Hoạt Bằng Key",
        "btn_buy_key": "🛒 Mua Key",
        "btn_scan": "⚡ Quét Link TikTok",
        "btn_account": "👤 Tài Khoản",
        "btn_lang": "🌐 Đổi Ngôn Ngữ",
        "btn_help": "🆘 Hỗ Trợ",
        "btn_guide": "📺 Hướng Dẫn",
        "btn_cdk_admin": "🎟️ Tạo Key",
        "product_name": "👑 Gói Vĩnh Viễn",
        "prompt_input": f"{E_LOADING} Nhập <b>Username</b> hoặc <b>Link Locket</b> vào tin nhắn trả lời bên dưới:",
        "lang_select": "🌐 Vui lòng chọn ngôn ngữ / Please select language:",
        "lang_set": f"{E_SUCCESS} Đã cài đặt ngôn ngữ: Tiếng Việt",
        "help_msg": (
            f"{E_MENU} <b>LOCKET GOLD &amp; SERVICES</b>\n\n"
            f"{E_MONEY} <b>NẠP TIỀN &amp; NHẬN KEY</b>\n"
            f"• /nap — Mua Key {E_KEY} <b>Gói Vĩnh Viễn</b>\n"
            f"• /sodu — Xem key còn lại &amp; lịch sử mua\n"
            f"• /redeem &lt;mã_key&gt; &lt;link_locket&gt; — Kích hoạt Gold\n\n"
            f"🔍 <b>KIỂM TRA GOLD</b>\n"
            f"• /check &lt;user_hoặc_link&gt; — Kiểm tra 1 tài khoản\n"
            f"• /chk (kèm file .txt) — Kiểm tra hàng loạt\n\n"
            f"⚡ <b>QUÉT LINK</b>\n"
            f"• /scan &lt;link_tiktok&gt; — Quét bình luận lấy link Locket\n\n"
            f"{E_TIP} Gold rớt sau thời gian dài sử dụng? Vào web bấm <b>Kích hoạt lại miễn phí</b>."
        ),
        "admin_help": (
            f"\n\n⚙️ <b>ADMIN</b>\n"
            f"• /genkey &lt;số_lượt&gt; [1m|1y] — Tạo Key thủ công\n"
            f"• /set &lt;link_nguồn&gt; — Xem/thêm nguồn vào kho\n"
            f"• /checksources [quick] — Kiểm tra &amp; dọn kho nguồn (mặc định thử alias sâu)\n"
            f"• /stats — Thống kê hệ thống\n"
            f"• /noti &lt;msg&gt; — Thông báo tới mọi user\n"
            f"• /setdonate — Đặt ảnh thành công (reply ảnh)\n"
            f"• /setvideo — Đặt video hướng dẫn (reply video)"
        ),
        "resolving": f"{E_LOADING} <b>Đang phân giải UID...</b>",
        "not_found": f"{E_ERROR} Không tìm thấy User.",
        "checking_status": f"{E_LOADING} <b>Đang kiểm tra Gold...</b>",
        "free_status": "Chưa có Gold",
        "gold_active": f"{E_SUCCESS} <b>Gold đang hoạt động</b> (hạn: {{}})",
        "user_info_title": f"{E_USER} <b>Thông tin tài khoản</b>",
        "btn_upgrade": "🎟️ Kích Hoạt Bằng Key",
        "admin_noti_sent": f"{E_SUCCESS} Đã gửi thông báo đến tất cả user.",
        "admin_only": f"{E_ERROR} Bạn không có quyền sử dụng lệnh này.",
        "success_title": f"{E_SUCCESS} <b>KÍCH HOẠT THÀNH CÔNG</b>",
        "fail_title": f"{E_ERROR} <b>Kích hoạt thất bại</b>",

        # Buy key
        "buy_key_title": "🛒 <b>MUA KEY — GÓI VĨNH VIỄN</b>\n\nChọn số lượng key cần mua:",
        "buy_key_config_error": f"{E_ERROR} Thanh toán đang tạm khóa do thiếu cấu hình. Vui lòng báo admin.",
        "buy_key_not_found": f"{E_ERROR} Đơn không tồn tại, không thuộc tài khoản này hoặc đã được xử lý.",
        "buy_key_waiting": "⏳ Chưa nhận được giao dịch phù hợp. Vui lòng thử lại sau ít phút.",
        "buy_key_paid": (
            f"{E_SUCCESS} <b>THANH TOÁN THÀNH CÔNG</b>\n\n"
            f"{E_KEY} Key của bạn:\n<pre>{{codes}}</pre>\n"
            f"Gói: <b>{{plan}}</b>\n\n"
            f"👉 Kích hoạt: <code>/redeem {{code}} &lt;link_locket&gt;</code>\n"
            f"Ví dụ: <code>/redeem {{code}} https://locket.cam/username</code>"
        ),
        "buy_key_prompt_qty": f"{E_LOADING} Nhập <b>số lượng key</b> cần mua (1-5):",
        "buy_key_qty_invalid": f"{E_ERROR} Số lượng không hợp lệ. Vui lòng nhập số từ 1-5.",
        "buy_key_order_created": (
            f"{E_MONEY} <b>ĐƠN HÀNG ĐÃ TẠO</b>\n\n"
            f"Gói: <b>{{plan}}</b>\n"
            f"Số lượng: <b>{{qty}}</b> key\n"
            f"Số tiền: <b>{{amount}} VNĐ</b>\n"
            f"Nội dung CK: <code>{{content}}</code> (giữ nguyên)\n\n"
            f"Quét VietQR bên trên để thanh toán. Key sẽ được gửi tự động sau 3-10 giây."
        ),

        # Redeem
        "redeem_usage": (
            f"{E_KEY} <b>KÍCH HOẠT GOLD BẰNG KEY</b>\n\n"
            f"Cú pháp: <code>/redeem &lt;mã_key&gt; &lt;link_locket&gt;</code>\n"
            f"Ví dụ: <code>/redeem LOCK-XXXX-XXXX-XXXX https://locket.cam/username</code>\n\n"
            f"{E_TIP} Chưa có Key? Dùng /nap để mua tự động qua VietQR."
        ),
        "redeem_checking": f"{E_LOADING} Đang kiểm tra Key và chuẩn bị kích hoạt Gold...",
        "redeem_invalid": f"{E_ERROR} Key không tồn tại hoặc không hợp lệ!",
        "redeem_exhausted": f"{E_ERROR} Key này đã hết lượt sử dụng!",
        "redeem_already_gold": (
            f"{E_LIMIT} Tài khoản <b>{{user}}</b> đã có Gold (còn {{days}} ngày, hạn {{expires}}).\n"
            f"Lượt Key đã được hoàn lại.\n"
            f"{E_TIP} Chỉ nạp đè được khi dùng Key gói 1 Năm hoặc nick còn dưới 7 ngày."
        ),
        "redeem_no_source": (
            f"{E_ERROR} <b>Kho nguồn đang trống</b> hoặc toàn bộ nguồn đã đạt 5/5 lượt.\n"
            f"Lượt Key đã được hoàn lại. Vui lòng liên hệ admin."
        ),
        "redeem_alias_limit": (
            f"{E_LIMIT} Nguồn đã chạm trần liên kết (alias limit).\n"
            f"Lượt Key đã được hoàn lại. Vui lòng thử lại sau ít phút."
        ),
        "redeem_ip_blocked": (
            f"{E_LIMIT} Locket/RevenueCat đang tạm chặn IP (403).\n"
            f"Lượt Key đã được hoàn lại. Vui lòng thử lại sau ít phút."
        ),
        "redeem_failed": (
            f"{E_ERROR} <b>Kích hoạt thất bại</b>\nChi tiết: <code>{{error}}</code>\n"
            f"Lượt Key đã được hoàn lại."
        ),
        "redeem_success": (
            f"{E_SUCCESS} <b>KÍCH HOẠT THÀNH CÔNG</b>\n\n"
            f"{E_TAG} User: <code>{{user}}</code>\n"
            f"{E_ID} UID: <code>{{uid}}</code>\n"
            f"{E_CALENDAR} Hạn Gold: <code>{{expires}}</code>{{days}}\n"
            f"{E_KEY} Key: <code>{{key}}</code> (còn {{left}} lượt)\n\n"
            f"{E_TIP} Mở app Locket kiểm tra Gold ngay bây giờ."
        ),

        # Account
        "account_info": (
            f"{E_USER} <b>TÀI KHOẢN CỦA BẠN</b>\n\n"
            f"ID: <code>{{user_id}}</code>{{admin_tag}}\n"
            f"{E_KEY} Key chưa dùng: <b>{{unused}}</b> (1 tháng: {{unused_1m}} | 1 năm: {{unused_1y}})\n"
            f"🧾 Đã mua: <b>{{orders}}</b> đơn — <b>{{spent}} VNĐ</b>\n"
            f"✅ Đã kích hoạt: <b>{{redeemed}}</b> lần\n\n"
            f"{E_TIP} Dùng /nap để mua Key, /redeem để kích hoạt."
        ),
        "account_history": "<b>Lịch sử gần đây:</b>",
        "account_history_line": "• {{time}} — {{target}} ({{plan}})",
        "account_no_history": "Chưa có lịch sử kích hoạt.",

        # Check
        "check_usage": (
            f"🔍 <b>KIỂM TRA GOLD</b>\n\n"
            f"Cú pháp: <code>/check &lt;username_hoặc_link&gt;</code>\n"
            f"Ví dụ: <code>/check pdlinhh</code>"
        ),
        "check_result": (
            f"🔍 <b>KẾT QUẢ KIỂM TRA</b>\n\n"
            f"{E_TAG} User: <code>{{user}}</code>\n"
            f"{E_STAT} Trạng thái: <b>{{status}}</b>\n"
            f"{E_CALENDAR} Hạn: <code>{{expires}}</code>{{days}}"
        ),
        "check_inactive": "Chưa có Gold",
        "check_source_added": f"🗂️ {E_SUCCESS} Đã thêm tài khoản này vào kho nguồn (Gold còn dài hạn).",
        "check_source_exists": "🗂️ Tài khoản này đã có trong kho nguồn.",

        # Bulk check
        "chk_usage": (
            f"📄 <b>KIỂM TRA HÀNG LOẠT</b>\n\n"
            f"Gửi file <b>.txt</b> (mỗi dòng 1 username/link) kèm chú thích /chk,\n"
            f"hoặc reply /chk vào file đã gửi."
        ),
        "chk_not_txt": f"{E_ERROR} Vui lòng gửi file định dạng .txt!",
        "chk_empty": f"{E_ERROR} File rỗng hoặc không có dòng nào!",
        "chk_too_many": f"{E_ERROR} File quá lớn (tối đa {{max}} dòng).",
        "chk_downloading": f"{E_LOADING} Đang tải và đọc danh sách từ file...",
        "chk_progress": "⏳ Tiến độ: {done}/{total} | 👑 Đủ ĐK: {eligible} | ❌ Không đạt: {other}",
        "chk_result": (
            f"📄 <b>KẾT QUẢ KIỂM TRA HÀNG LOẠT</b>\n\n"
            f"File: <code>{{file}}</code>\n"
            f"Tổng kiểm tra: <b>{{total}}</b>\n"
            f"👑 Đủ điều kiện (Gold, còn ≥ {{min_days}} ngày): <b>{{eligible}}</b>\n"
            f"⏰ Hết hạn / dưới {{min_days}} ngày: <b>{{expired}}</b>\n"
            f"⚪ Chưa có Gold: <b>{{no_gold}}</b>\n"
            f"❌ Không tìm thấy: <b>{{not_found}}</b>\n\n"
            f"{{added_note}}"
        ),
        "chk_added_note": f"{E_SUCCESS} Đã thêm <b>{{n}}</b> tài khoản đủ điều kiện vào kho nguồn.",
        "chk_ip_blocked": f"{E_LIMIT} IP đang bị chặn khi kiểm tra. Đã dừng để tránh bị khóa thêm.",

        # Scan
        "scan_usage": (
            f"⚡ <b>QUÉT LINK LOCKET TỪ TIKTOK / THREADS</b>\n\n"
            f"Cú pháp: <code>/scan &lt;link_video&gt;</code>\n"
            f"Ví dụ: <code>/scan https://www.tiktok.com/@user/video/1234567890</code>"
        ),
        "scan_running": f"{E_LOADING} Đang quét toàn bộ bình luận, vui lòng chờ...",
        "scan_no_links": "Không tìm thấy link Locket nào trong bình luận.",
        "scan_result": (
            f"⚡ <b>KẾT QUẢ QUÉT &amp; KIỂM TRA</b>\n\n"
            f"Nguồn: <code>{{source}}</code>\n"
            f"Tổng link quét được: <b>{{total}}</b>\n"
            f"👑 Đủ điều kiện: <b>{{eligible}}</b>\n"
            f"⏰ Hết hạn / dưới {{min_days}} ngày: <b>{{expired}}</b>\n"
            f"⚪ Chưa có Gold: <b>{{no_gold}}</b>\n"
            f"❌ Không tìm thấy: <b>{{not_found}}</b>\n\n"
            f"{{added_note}}"
        ),

        # Admin key generation
        "genkey_usage": (
            f"{E_KEY} <b>TẠO KEY</b>\n\n"
            f"Cú pháp: <code>/genkey &lt;số_lượt&gt; [1m|1y]</code>\n"
            f"Ví dụ: <code>/genkey 5 1y</code>"
        ),
        "genkey_invalid": f"{E_ERROR} Số lượt không hợp lệ (1-500).",
        "genkey_done": (
            f"{E_SUCCESS} <b>ĐÃ TẠO KEY</b>\n\n"
            f"Gói: <b>{{plan}}</b> — Số lượt: <b>{{spins}}</b>\n"
            f"<pre>{{codes}}</pre>\n"
            f"👉 Khách dùng: <code>/redeem {{code}} &lt;link_locket&gt;</code>"
        ),
        "cdk_qty_prompt": f"{E_LOADING} Nhập <b>số lượng key</b> cần tạo (tối đa 500):",
        "cdk_qty_invalid": f"{E_ERROR} Số lượng không hợp lệ. Vui lòng nhập số từ 1-500.",
        "cdk_done_header": f"{E_SUCCESS} <b>Đã tạo {{n}} Key</b> — bấm vào từng mã để copy:",
        "cdk_btn_copy": "📋 Sao Chép Codes",

        # Sources
        "set_usage": (
            f"⚙️ <b>KHO NGUỒN</b>\n\n"
            f"Tổng: <b>{{total}}</b> nguồn — khả dụng: <b>{{usable}}</b>\n\n"
            f"Cú pháp: <code>/set &lt;link_nguồn&gt;</code> để thêm nguồn mới."
        ),
        "set_checking": f"{E_LOADING} Đang kiểm tra nguồn...",
        "set_not_gold": f"{E_ERROR} Tài khoản này chưa có Gold hoặc hạn còn quá ngắn.",
        "set_done": (
            f"{E_SUCCESS} <b>ĐÃ THÊM NGUỒN</b>\n\n"
            f"User: <code>@{{user}}</code>\n"
            f"Hạn: <code>{{expires}}</code> (còn {{days}} ngày)\n"
            f"Đã dùng: {{count}}/5 lượt"
        ),
        "set_invalid": f"{E_ERROR} Link/username không hợp lệ.",
        "checksources_running": f"{E_LOADING} Đang kiểm tra kho nguồn...",
        "checksources_progress": "⏳ Đã kiểm tra {done}/{total} | ✅ Dùng được: {usable} | ⚠️ Loại: {removed}",
        "checksources_report": (
            f"{E_STAT} <b>BÁO CÁO KHO NGUỒN</b>\n\n"
            f"Tổng kiểm tra: <b>{{total}}</b>\n"
            f"✅ Dùng được: <b>{{usable}}</b>\n"
            f"⚠️ Chạm limit (đã loại): <b>{{limit}}</b>\n"
            f"⏰ Hết hạn / quá ngắn: <b>{{expiring}}</b>\n"
            f"⚪ Mất Gold: <b>{{no_gold}}</b>\n"
            f"❌ Lỗi mạng/IP: <b>{{error}}</b>\n\n"
            f"Đã dọn <b>{{removed}}</b> nguồn không còn dùng được."
        ),

        "guide_msg": (
            f"{E_MENU} <b>HƯỚNG DẪN SỬ DỤNG</b>\n\n"
            f"1️⃣ Mua Key: /nap (1 tháng) hoặc /nap 1y (1 năm).\n"
            f"2️⃣ Nhận Key tự động sau khi chuyển khoản.\n"
            f"3️⃣ Kích hoạt: /redeem &lt;key&gt; &lt;link_locket&gt;.\n"
            f"4️⃣ Kiểm tra: /check &lt;username&gt;.\n\n"
            f"{E_TIP} Xem video hướng dẫn bên dưới nếu cần!"
        ),
        "admin_reset": f"{E_SUCCESS} Đã reset lượt dùng cho user {{}}.",
        "queue_almost": "",
        "processing": "",
        "generating_dns": "",
    },
    "EN": {
        "welcome": f"{E_SUCCESS} <b>Locket Gold &amp; Services</b>\n\nWelcome! Pick a language or use the menu below.",
        "menu_msg": f"{E_MENU} <b>Control Panel</b>\n\n👇 Choose an action below.",
        "btn_input": "🔍 Check Gold",
        "btn_redeem": "🎟️ Redeem Key",
        "btn_buy_key": "🛒 Buy Key",
        "btn_scan": "⚡ Scan TikTok Link",
        "btn_account": "👤 Account",
        "btn_lang": "🌐 Change Language",
        "btn_help": "🆘 Help",
        "btn_guide": "📺 Guide",
        "btn_cdk_admin": "🎟️ Generate Key",
        "product_name": "👑 Permanent Plan",
        "prompt_input": f"{E_LOADING} Enter your <b>Username</b> or <b>Locket link</b> in the reply below:",
        "lang_select": "🌐 Please select language:",
        "lang_set": f"{E_SUCCESS} Language set: English",
        "help_msg": (
            f"{E_MENU} <b>LOCKET GOLD &amp; SERVICES</b>\n\n"
            f"{E_MONEY} <b>TOP UP &amp; GET KEYS</b>\n"
            f"• /nap — Buy a {E_KEY} <b>Permanent Plan</b> key\n"
            f"• /sodu — View remaining keys &amp; purchase history\n"
            f"• /redeem &lt;key&gt; &lt;locket_link&gt; — Activate Gold\n\n"
            f"🔍 <b>GOLD CHECKS</b>\n"
            f"• /check &lt;user_or_link&gt; — Check one account\n"
            f"• /chk (with a .txt file) — Bulk check\n\n"
            f"⚡ <b>LINK SCANNER</b>\n"
            f"• /scan &lt;tiktok_link&gt; — Scrape Locket links from comments\n\n"
            f"{E_TIP} Gold dropped after long use? Open the web store and tap <b>Free re-activation</b>."
        ),
        "admin_help": (
            f"\n\n⚙️ <b>ADMIN</b>\n"
            f"• /genkey &lt;spins&gt; [1m|1y] — Generate keys\n"
            f"• /set &lt;source_link&gt; — View/add a source\n"
            f"• /checksources [quick] — Validate &amp; clean the source pool (deep probe by default)\n"
            f"• /stats — System statistics\n"
            f"• /noti &lt;msg&gt; — Broadcast to all users\n"
            f"• /setdonate — Set success photo (reply to a photo)\n"
            f"• /setvideo — Set guide video (reply to a video)"
        ),
        "resolving": f"{E_LOADING} <b>Resolving UID...</b>",
        "not_found": f"{E_ERROR} User not found.",
        "checking_status": f"{E_LOADING} <b>Checking Gold...</b>",
        "free_status": "No Gold",
        "gold_active": f"{E_SUCCESS} <b>Gold active</b> (expires: {{}})",
        "user_info_title": f"{E_USER} <b>Account information</b>",
        "btn_upgrade": "🎟️ Redeem Key",
        "admin_noti_sent": f"{E_SUCCESS} Notification sent to all users.",
        "admin_only": f"{E_ERROR} You don't have permission.",
        "success_title": f"{E_SUCCESS} <b>ACTIVATION SUCCESSFUL</b>",
        "fail_title": f"{E_ERROR} <b>Activation failed</b>",

        "buy_key_title": "🛒 <b>BUY KEY — PERMANENT PLAN</b>\n\nChoose how many keys you need:",
        "buy_key_config_error": f"{E_ERROR} Payments are temporarily unavailable because configuration is incomplete.",
        "buy_key_not_found": f"{E_ERROR} This order does not exist, belongs to another user, or was already processed.",
        "buy_key_waiting": "⏳ No matching payment yet. Please try again in a few minutes.",
        "buy_key_paid": (
            f"{E_SUCCESS} <b>PAYMENT CONFIRMED</b>\n\n"
            f"{E_KEY} Your key:\n<pre>{{codes}}</pre>\n"
            f"Plan: <b>{{plan}}</b>\n\n"
            f"👉 Activate: <code>/redeem {{code}} &lt;locket_link&gt;</code>\n"
            f"Example: <code>/redeem {{code}} https://locket.cam/username</code>"
        ),
        "buy_key_prompt_qty": f"{E_LOADING} Enter the <b>number of keys</b> to buy (1-5):",
        "buy_key_qty_invalid": f"{E_ERROR} Invalid quantity. Enter a number from 1-5.",
        "buy_key_order_created": (
            f"{E_MONEY} <b>ORDER CREATED</b>\n\n"
            f"Plan: <b>{{plan}}</b>\n"
            f"Quantity: <b>{{qty}}</b> key(s)\n"
            f"Amount: <b>{{amount}} VND</b>\n"
            f"Transfer note: <code>{{content}}</code> (keep unchanged)\n\n"
            f"Scan the VietQR above. Keys are delivered automatically in 3-10 seconds."
        ),

        "redeem_usage": (
            f"{E_KEY} <b>REDEEM GOLD WITH A KEY</b>\n\n"
            f"Syntax: <code>/redeem &lt;key&gt; &lt;locket_link&gt;</code>\n"
            f"Example: <code>/redeem LOCK-XXXX-XXXX-XXXX https://locket.cam/username</code>\n\n"
            f"{E_TIP} No key yet? Use /nap to buy one via VietQR."
        ),
        "redeem_checking": f"{E_LOADING} Validating your key and preparing activation...",
        "redeem_invalid": f"{E_ERROR} Key does not exist or is invalid!",
        "redeem_exhausted": f"{E_ERROR} This key has no spins left!",
        "redeem_already_gold": (
            f"{E_LIMIT} <b>{{user}}</b> already has Gold ({{days}} days left, expires {{expires}}).\n"
            f"Your key spin has been refunded.\n"
            f"{E_TIP} Overwrite is only allowed with a 1-Year key or when under 7 days remain."
        ),
        "redeem_no_source": (
            f"{E_ERROR} <b>Source pool is empty</b> or every source reached 5/5 spins.\n"
            f"Your key spin has been refunded. Please contact the admin."
        ),
        "redeem_alias_limit": (
            f"{E_LIMIT} Source hit the alias limit.\n"
            f"Your key spin has been refunded. Please try again shortly."
        ),
        "redeem_ip_blocked": (
            f"{E_LIMIT} Locket/RevenueCat is temporarily blocking this IP (403).\n"
            f"Your key spin has been refunded. Please try again shortly."
        ),
        "redeem_failed": (
            f"{E_ERROR} <b>Activation failed</b>\nDetail: <code>{{error}}</code>\n"
            f"Your key spin has been refunded."
        ),
        "redeem_success": (
            f"{E_SUCCESS} <b>ACTIVATION SUCCESSFUL</b>\n\n"
            f"{E_TAG} User: <code>{{user}}</code>\n"
            f"{E_ID} UID: <code>{{uid}}</code>\n"
            f"{E_CALENDAR} Gold expires: <code>{{expires}}</code>{{days}}\n"
            f"{E_KEY} Key: <code>{{key}}</code> ({{left}} spins left)\n\n"
            f"{E_TIP} Open the Locket app and check your Gold now."
        ),

        "account_info": (
            f"{E_USER} <b>YOUR ACCOUNT</b>\n\n"
            f"ID: <code>{{user_id}}</code>{{admin_tag}}\n"
            f"{E_KEY} Unused keys: <b>{{unused}}</b> (1m: {{unused_1m}} | 1y: {{unused_1y}})\n"
            f"🧾 Orders: <b>{{orders}}</b> — <b>{{spent}} VND</b>\n"
            f"✅ Activations: <b>{{redeemed}}</b>\n\n"
            f"{E_TIP} Use /nap to buy keys and /redeem to activate."
        ),
        "account_history": "<b>Recent history:</b>",
        "account_history_line": "• {{time}} — {{target}} ({{plan}})",
        "account_no_history": "No activation history yet.",

        "check_usage": (
            f"🔍 <b>CHECK GOLD</b>\n\n"
            f"Syntax: <code>/check &lt;username_or_link&gt;</code>\n"
            f"Example: <code>/check pdlinhh</code>"
        ),
        "check_result": (
            f"🔍 <b>CHECK RESULT</b>\n\n"
            f"{E_TAG} User: <code>{{user}}</code>\n"
            f"{E_STAT} Status: <b>{{status}}</b>\n"
            f"{E_CALENDAR} Expires: <code>{{expires}}</code>{{days}}"
        ),
        "check_inactive": "No Gold",
        "check_source_added": f"🗂️ {E_SUCCESS} Added this account to the source pool (long-lived Gold).",
        "check_source_exists": "🗂️ This account is already in the source pool.",

        "chk_usage": (
            f"📄 <b>BULK CHECK</b>\n\n"
            f"Send a <b>.txt</b> file (one username/link per line) with the /chk caption,\n"
            f"or reply /chk to an existing file."
        ),
        "chk_not_txt": f"{E_ERROR} Please send a .txt file!",
        "chk_empty": f"{E_ERROR} The file is empty!",
        "chk_too_many": f"{E_ERROR} File is too large (max {{max}} lines).",
        "chk_downloading": f"{E_LOADING} Downloading and reading the list...",
        "chk_progress": "⏳ Progress: {done}/{total} | 👑 Eligible: {eligible} | ❌ Other: {other}",
        "chk_result": (
            f"📄 <b>BULK CHECK RESULT</b>\n\n"
            f"File: <code>{{file}}</code>\n"
            f"Checked: <b>{{total}}</b>\n"
            f"👑 Eligible (Gold, ≥ {{min_days}} days): <b>{{eligible}}</b>\n"
            f"⏰ Expired / under {{min_days}} days: <b>{{expired}}</b>\n"
            f"⚪ No Gold: <b>{{no_gold}}</b>\n"
            f"❌ Not found: <b>{{not_found}}</b>\n\n"
            f"{{added_note}}"
        ),
        "chk_added_note": f"{E_SUCCESS} Added <b>{{n}}</b> eligible accounts to the source pool.",
        "chk_ip_blocked": f"{E_LIMIT} IP is blocked while checking. Stopped to avoid a longer ban.",

        "scan_usage": (
            f"⚡ <b>SCRAPE LOCKET LINKS FROM TIKTOK / THREADS</b>\n\n"
            f"Syntax: <code>/scan &lt;video_link&gt;</code>\n"
            f"Example: <code>/scan https://www.tiktok.com/@user/video/1234567890</code>"
        ),
        "scan_running": f"{E_LOADING} Scraping all comments, please wait...",
        "scan_no_links": "No Locket link found in the comments.",
        "scan_result": (
            f"⚡ <b>SCAN &amp; CHECK RESULT</b>\n\n"
            f"Source: <code>{{source}}</code>\n"
            f"Links found: <b>{{total}}</b>\n"
            f"👑 Eligible: <b>{{eligible}}</b>\n"
            f"⏰ Expired / under {{min_days}} days: <b>{{expired}}</b>\n"
            f"⚪ No Gold: <b>{{no_gold}}</b>\n"
            f"❌ Not found: <b>{{not_found}}</b>\n\n"
            f"{{added_note}}"
        ),

        "genkey_usage": (
            f"{E_KEY} <b>GENERATE KEY</b>\n\n"
            f"Syntax: <code>/genkey &lt;spins&gt; [1m|1y]</code>\n"
            f"Example: <code>/genkey 5 1y</code>"
        ),
        "genkey_invalid": f"{E_ERROR} Invalid spins (1-500).",
        "genkey_done": (
            f"{E_SUCCESS} <b>KEY CREATED</b>\n\n"
            f"Plan: <b>{{plan}}</b> — Spins: <b>{{spins}}</b>\n"
            f"<pre>{{codes}}</pre>\n"
            f"👉 Customer uses: <code>/redeem {{code}} &lt;locket_link&gt;</code>"
        ),
        "cdk_qty_prompt": f"{E_LOADING} Enter the <b>number of keys</b> to generate (max 500):",
        "cdk_qty_invalid": f"{E_ERROR} Invalid quantity. Enter a number from 1-500.",
        "cdk_done_header": f"{E_SUCCESS} <b>Generated {{n}} keys</b> — tap each code to copy:",
        "cdk_btn_copy": "📋 Copy Codes",

        "set_usage": (
            f"⚙️ <b>SOURCE POOL</b>\n\n"
            f"Total: <b>{{total}}</b> sources — usable: <b>{{usable}}</b>\n\n"
            f"Syntax: <code>/set &lt;source_link&gt;</code> to add a source."
        ),
        "set_checking": f"{E_LOADING} Checking the source...",
        "set_not_gold": f"{E_ERROR} This account has no Gold or too few days left.",
        "set_done": (
            f"{E_SUCCESS} <b>SOURCE ADDED</b>\n\n"
            f"User: <code>@{{user}}</code>\n"
            f"Expires: <code>{{expires}}</code> ({{days}} days left)\n"
            f"Used: {{count}}/5 spins"
        ),
        "set_invalid": f"{E_ERROR} Invalid link/username.",
        "checksources_running": f"{E_LOADING} Validating the source pool...",
        "checksources_progress": "⏳ Checked {done}/{total} | ✅ Usable: {usable} | ⚠️ Removed: {removed}",
        "checksources_report": (
            f"{E_STAT} <b>SOURCE POOL REPORT</b>\n\n"
            f"Checked: <b>{{total}}</b>\n"
            f"✅ Usable: <b>{{usable}}</b>\n"
            f"⚠️ Alias limit (removed): <b>{{limit}}</b>\n"
            f"⏰ Expired / too short: <b>{{expiring}}</b>\n"
            f"⚪ Lost Gold: <b>{{no_gold}}</b>\n"
            f"❌ Network/IP errors: <b>{{error}}</b>\n\n"
            f"Cleaned <b>{{removed}}</b> unusable sources."
        ),

        "guide_msg": (
            f"{E_MENU} <b>USAGE GUIDE</b>\n\n"
            f"1️⃣ Buy a key: /nap (1 month) or /nap 1y (1 year).\n"
            f"2️⃣ The key is delivered automatically after payment.\n"
            f"3️⃣ Activate: /redeem &lt;key&gt; &lt;locket_link&gt;.\n"
            f"4️⃣ Verify: /check &lt;username&gt;.\n\n"
            f"{E_TIP} Watch the guide video below if needed!"
        ),
        "admin_reset": f"{E_SUCCESS} Usage reset for user {{}}.",
        "queue_almost": "",
        "processing": "",
        "generating_dns": "",
    }
}


def T(key, lang=None):
    if not lang:
        lang = DEFAULT_LANG
    return TEXTS.get(lang, TEXTS["VI"]).get(key, key)


def plan_label(plan, lang=None):
    lang = lang if lang in PLAN_LABELS else DEFAULT_LANG
    return PLAN_LABELS[lang].get("1y" if (plan or "").lower() == "1y" else "1m", plan)
