import logging
import time
import re
import os
import threading
from collections import defaultdict
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)
from telegram.constants import ParseMode

import asyncio
import io
from app.services import locket
from app.config import token as TOKEN_LIST
import scan_locket

from telegram.request import HTTPXRequest

import user_db
import sepay_config

BOT_TOKEN = "8697282663:AAHOoqv8Zs1A5ly5dt41iI30Bi8aurcg9V0"

# Danh sách ID Telegram được phép sử dụng bot (để trống [] là ai cũng dùng được)
# Bạn có thể điền ID của bạn vào đây, ví dụ: ADMIN_IDS = [123456789]
ADMIN_IDS = [965108311]
# ADMIN_IDS = [7625406656]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
# Tắt log request liên tục (getUpdates 200 OK) của httpx và httpcore
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

MAX_CONCURRENT_UPDATES = 8
MAX_CONCURRENT_CHECKS = 4
MAX_CONCURRENT_SCANS = 2


def extract_username(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    # Nếu là dynamic link: locket.camera/links/... hoặc locket.cam/links/...
    if "links/" in text.lower():
        code = text.split("links/")[-1].split("?")[0].strip("/")
        return f"https://locket.camera/links/{code}"

    # Nếu là link invites hoặc profile link
    if "locket.camera/invites/" in text:
        text = text.split("locket.camera/invites/")[-1].split("?")[0].strip("/")
    elif "locket.cam/invites/" in text:
        text = text.split("locket.cam/invites/")[-1].split("?")[0].strip("/")
    elif "locket.cam/" in text:
        text = text.split("locket.cam/")[-1].split("?")[0].strip("/")
    elif "locket.camera/" in text:
        text = text.split("locket.camera/")[-1].split("?")[0].strip("/")

    text = text.lstrip("@").split("?")[0].strip("/")
    return text


def _parse_source_expiry(expires_str: str):
    if not expires_str:
        return None
    match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', expires_str)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _normalize_sources(sources):
    """Gộp username trùng và sắp xếp theo ngày hết hạn từ lớn xuống bé."""
    unique = {}

    for item in sources:
        username = extract_username(str(item.get("username", ""))).strip().lstrip("@")
        if not username:
            continue

        try:
            count = max(0, int(item.get("count", 0)))
        except (TypeError, ValueError):
            count = 0

        expires = str(item.get("expires", "")).strip()
        candidate = {
            "stt": 0,
            "username": username,
            "count": count,
            "expires": expires,
        }
        key = username.casefold()
        existing = unique.get(key)

        if existing is None:
            unique[key] = candidate
            continue

        existing["count"] = max(existing["count"], count)
        existing_expiry = _parse_source_expiry(existing["expires"])
        candidate_expiry = _parse_source_expiry(expires)
        if candidate_expiry and (not existing_expiry or candidate_expiry > existing_expiry):
            existing["expires"] = expires
        elif not existing["expires"] and expires:
            existing["expires"] = expires

    normalized = list(unique.values())
    normalized.sort(
        key=lambda source: (
            _parse_source_expiry(source.get("expires", "")) or datetime.min,
            get_days_left_from_expires(source.get("expires", "")),
        ),
        reverse=True,
    )

    for index, item in enumerate(normalized, 1):
        item["stt"] = index
    return normalized


def esc(s: str) -> str:
    """Escape MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, f"\\{ch}")
    return s


def is_admin(user_id: int) -> bool:
    """Kiểm tra quyền Admin (quyền quản lý nguồn, cấu hình)."""
    return user_id in ADMIN_IDS


HELP_TEXT_USER = (
    "*\\[ LOCKET GOLD & SERVICES BOT \\]*\n\n"
    "💰 *1\\. NẠP TIỀN & NHẬN KEY:*\n"
    "• `/nap` : Tạo VietQR lấy Key gói *1 Tháng* \\(ưu tiên nguồn 25\\-30 ngày\\)\\.\n"
    "• `/nap 1y` : Tạo VietQR lấy Key gói *1 Năm* \\(ưu tiên nguồn 200\\-360 ngày\\)\\.\n"
    "• `/sodu` : Kiểm tra số dư và lịch sử nạp của bạn\\.\n"
    "• `/redeem <mã_key> <link_locket>` : Kích hoạt Gold bằng mã Key\\.\n"
    "  _Ví dụ:_ `/redeem LK-GOLD-89ABCX https://locket.cam/username`\n\n"
    "🔍 *2\\. LỆNH KIỂM TRA LOCKET GOLD:*\n"
    "• `/check <user_hoặc_link>` : Kiểm tra 1 nick có Gold hay không, ngày hết hạn\\.\n"
    "• `/chk` \\(kèm file \\.txt\\) : Gửi file danh sách link để kiểm tra hàng loạt\\.\n\n"
    "⚡ *3\\. QUÉT LINK TỪ TIKTOK / THREADS:*\n"
    "• `/scan <link>` : Quét tự động toàn bộ bình luận lấy link Locket\\."
)

HELP_TEXT_ADMIN = (
    "\n\n⚙️ *4\\. LỆNH QUẢN LÝ QUẢN TRỊ VIÊN \\(ADMIN\\):*\n"
    "• `/genkey <số_lượt> [1m|1y]` : Tạo thủ công mã Key kích hoạt Gold\\.\n"
    "  _Ví dụ:_ `/genkey 5 1y` \\(tạo key 5 lượt gói 1 năm\\)\n"
    "• `/set <link_nguồn>` : Xem thông tin hoặc ghim nguồn nạp mới vào đầu kho\\.\n"
    "• `/checksources` : Tự động quét kiểm tra và loại bỏ các nguồn chạm trần Limit \\(50/50\\) trong kho\\."
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    user_id = update.effective_user.id if update.effective_user else 0
    text = HELP_TEXT_USER
    if is_admin(user_id):
        text += HELP_TEXT_ADMIN
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


# ──────────────────────────────────────────────────────────────────────────────

SOURCE_FILE = "current_source.txt"

# Khóa đồng bộ đa luồng cho kho nguồn và người dùng
_source_file_lock = threading.RLock()
_source_reserve_lock = asyncio.Lock()
_in_flight_sources = defaultdict(int)
_user_locks = defaultdict(asyncio.Lock)
_check_slots = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
_bulk_check_lock = asyncio.Lock()
_scan_slots = asyncio.Semaphore(MAX_CONCURRENT_SCANS)


def clean_and_update_sources(sources=None):
    """
    Tự động tính lại số ngày còn lại (real-time countdown) và tự động XÓA
    các tài khoản đã hết hạn (ngày hết hạn <= hiện tại hoặc còn 0 ngày).
    """
    with _source_file_lock:
        if sources is None:
            sources = load_sources(raw=True)

        original_sources = [dict(item) for item in sources]
        sources = _normalize_sources(sources)
        now = datetime.now()
        valid_sources = []
        has_changes = sources != original_sources

        for item in sources:
            exp_text = item.get("expires", "").strip()
            if not exp_text:
                valid_sources.append(item)
                continue

            # Tìm định dạng ngày: YYYY-MM-DD HH:MM:SS
            date_match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', exp_text)
            if date_match:
                date_str = date_match.group(1)
                try:
                    exp_dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                    # Đếm lùi số ngày theo thời gian thực tế
                    days_left = (exp_dt - now).days
                    if exp_dt <= now or days_left < 10:
                        # ĐÃ HẾT HẠN HOẶC DƯỚI 10 NGÀY -> TỰ ĐỘNG XÓA
                        has_changes = True
                        logger.info(f"Tự động xóa nguồn hết hạn / dưới 10 ngày: @{item['username']} ({date_str}, còn {days_left} ngày)")
                        continue
                    else:
                        new_exp_text = f"expires: {date_str} (còn {days_left} ngày)"
                        if new_exp_text != exp_text:
                            has_changes = True
                            item["expires"] = new_exp_text
                        valid_sources.append(item)
                except Exception:
                    valid_sources.append(item)
            else:
                valid_sources.append(item)

        valid_sources = _normalize_sources(valid_sources)

        if has_changes or valid_sources != original_sources:
            save_sources(valid_sources)
        return valid_sources


def load_sources(raw: bool = False):
    """
    Đọc danh sách nguồn từ current_source.txt an toàn đa luồng.
    Nếu raw=False (mặc định), sẽ tự động chạy clean_and_update_sources() để đếm lùi và xóa nick hết hạn.
    """
    with _source_file_lock:
        sources = []
        if not os.path.exists(SOURCE_FILE):
            return sources

        with open(SOURCE_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]

        stt_counter = 1
        for line in lines:
            if line.startswith("#") or line.startswith("="):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                try:
                    stt = int(parts[0])
                except Exception:
                    stt = stt_counter
                u = extract_username(parts[1])
                try:
                    cnt = int(parts[2])
                except Exception:
                    cnt = 0
                exp = parts[3].strip() if len(parts) >= 4 else ""
                if u and not u.startswith("=") and len(u) >= 3:
                    sources.append({"stt": stt, "username": u, "count": cnt, "expires": exp})
                    stt_counter = max(stt_counter, stt + 1)
            elif len(parts) == 1:
                u = extract_username(parts[0])
                if u and not u.startswith("=") and len(u) >= 3:
                    sources.append({"stt": stt_counter, "username": u, "count": 0, "expires": ""})
                    stt_counter += 1

        if not raw:
            return clean_and_update_sources(sources)
        return sources


def save_sources(sources):
    """
    Ghi danh sách nguồn vào current_source.txt an toàn đa luồng.
    STT | USERNAME | SỐ LẦN ĐÃ KÍCH | EXPIRES
    """
    with _source_file_lock:
        sources = _normalize_sources(sources)
        with open(SOURCE_FILE, "w", encoding="utf-8") as f:
            f.write("# DANH SÁCH NGUỒN LOCKET GOLD\n")
            f.write("# FORMAT: STT | USERNAME | SỐ LẦN ĐÃ KÍCH (TỐI ĐA 5 LẦN) | EXPIRES\n")
            f.write("=" * 65 + "\n")
            for idx, item in enumerate(sources, 1):
                item['stt'] = idx
                exp_text = item.get('expires', '').strip()
                if exp_text:
                    f.write(f"{idx} | {item['username']} | {item['count']} | {exp_text}\n")
                else:
                    f.write(f"{idx} | {item['username']} | {item['count']}\n")


def get_days_left_from_expires(expires_str: str) -> int:
    """Trích xuất số ngày còn lại từ chuỗi expires."""
    if not expires_str:
        return 0
    m = re.search(r'còn\s+(\d+)\s+ngày', expires_str)
    if m:
        return int(m.group(1))
    date_match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', expires_str)
    if date_match:
        try:
            exp_dt = datetime.strptime(date_match.group(1), "%Y-%m-%d %H:%M:%S")
            return max(0, (exp_dt - datetime.now()).days)
        except Exception:
            pass
    return 0


def get_current_source_info(plan: str = "1m"):
    """
    Lấy username nguồn còn lượt (< 5 lần) được ưu tiên tối ưu theo gói đăng ký.
    Trả về: (username, count) hoặc ("", 0)
    """
    sources = load_sources()
    available = [item for item in sources if item['count'] < 5]
    if not available:
        return "", 0

    plan = (plan or "1m").lower()

    if plan == "1y":
        tier1 = [s for s in available if 200 <= get_days_left_from_expires(s.get('expires', '')) <= 360]
        if tier1:
            return tier1[0]['username'], tier1[0]['count']
        tier2 = [s for s in available if 190 <= get_days_left_from_expires(s.get('expires', '')) <= 370]
        if tier2:
            return tier2[0]['username'], tier2[0]['count']
        sorted_by_days = sorted(available, key=lambda s: get_days_left_from_expires(s.get('expires', '')), reverse=True)
        return sorted_by_days[0]['username'], sorted_by_days[0]['count']
    elif plan == "all":
        return available[0]['username'], available[0]['count']
    else:
        # Gói 1 Tháng ('1m')
        tier1 = [s for s in available if 25 <= get_days_left_from_expires(s.get('expires', '')) <= 30]
        if tier1:
            return tier1[0]['username'], tier1[0]['count']
        tier2 = [s for s in available if 22 <= get_days_left_from_expires(s.get('expires', '')) <= 33]
        if tier2:
            return tier2[0]['username'], tier2[0]['count']
        tier3 = [s for s in available if 15 <= get_days_left_from_expires(s.get('expires', '')) <= 60]
        if tier3:
            return tier3[0]['username'], tier3[0]['count']
        return available[0]['username'], available[0]['count']


async def reserve_source(plan: str = "1m"):
    """
    Tìm và tạm giữ (reserve) 1 slot trên tài khoản nguồn phù hợp nhất.
    Đảm bảo an toàn tuyệt đối khi nhiều người dùng kích hoạt cùng 1 lúc (multi-threading safe).
    Tổng số lần đã kích + số lần đang chờ xử lý không bao giờ vượt quá 5.
    Trả về: (username, current_count) hoặc ("", 0) nếu hết nguồn.
    """
    async with _source_reserve_lock:
        sources = load_sources()
        available = [
            item for item in sources
            if (item['count'] + _in_flight_sources[item['username'].lower()]) < 5
        ]
        if not available:
            return "", 0

        plan = (plan or "1m").lower()
        selected = None

        if plan == "1y":
            tier1 = [s for s in available if 200 <= get_days_left_from_expires(s.get('expires', '')) <= 360]
            if tier1:
                selected = tier1[0]
            else:
                tier2 = [s for s in available if 190 <= get_days_left_from_expires(s.get('expires', '')) <= 370]
                if tier2:
                    selected = tier2[0]
                else:
                    sorted_by_days = sorted(available, key=lambda s: get_days_left_from_expires(s.get('expires', '')), reverse=True)
                    selected = sorted_by_days[0]
        elif plan == "all":
            selected = available[0]
        else:
            tier1 = [s for s in available if 25 <= get_days_left_from_expires(s.get('expires', '')) <= 30]
            if tier1:
                selected = tier1[0]
            else:
                tier2 = [s for s in available if 22 <= get_days_left_from_expires(s.get('expires', '')) <= 33]
                if tier2:
                    selected = tier2[0]
                else:
                    tier3 = [s for s in available if 15 <= get_days_left_from_expires(s.get('expires', '')) <= 60]
                    if tier3:
                        selected = tier3[0]
                    else:
                        selected = available[0]

        if selected:
            u = selected['username']
            _in_flight_sources[u.lower()] += 1
            return u, selected['count']
        return "", 0


async def release_source(username: str, success: bool = False, exhausted: bool = False):
    """
    Giải phóng slot reserve sau khi quy trình kích hoạt hoàn tất.
    - success=True: tăng count thêm 1 trong current_source.txt.
    - exhausted=True: đánh dấu tài khoản nguồn đã đạt tối đa 5 lần kích (chạm trần alias RevenueCat).
    """
    async with _source_reserve_lock:
        u_clean = username.lower()
        if _in_flight_sources[u_clean] > 0:
            _in_flight_sources[u_clean] -= 1
        new_count = 0
        if success or exhausted:
            sources = load_sources()
            for item in sources:
                if item['username'].lower() == u_clean:
                    if exhausted:
                        item['count'] = 5
                    elif success:
                        item['count'] += 1
                    new_count = item['count']
                    break
            save_sources(sources)
        return new_count


def increment_current_source_count(username):
    """
    Tăng số lần đã kích hoạt của username nguồn thêm 1 (dành cho các tác vụ đồng bộ).
    """
    with _source_file_lock:
        sources = load_sources()
        updated = False
        new_count = 0
        for item in sources:
            if item['username'].lower() == username.lower():
                item['count'] += 1
                new_count = item['count']
                updated = True
                break
        if updated:
            save_sources(sources)
        return new_count


def add_eligible_sources(eligible_list):
    """
    Thêm các username đủ điều kiện vào current_source.txt nếu chưa có (thread-safe).
    """
    with _source_file_lock:
        sources = load_sources()
        existing = {item['username'].lower() for item in sources}
        added_count = 0
        next_stt = len(sources) + 1

        for item in eligible_list:
            if isinstance(item, (tuple, list)):
                u = item[0]
                exp = item[1] if len(item) > 1 else ""
                days_left = item[2] if len(item) > 2 else None
                exp_str = f"expires: {exp}"
                if days_left is not None:
                    exp_str += f" (còn {days_left} ngày)"
            else:
                u = item
                exp_str = ""
                days_left = None

            clean_u = extract_username(u)
            if days_left is not None and days_left < 10:
                continue
            if clean_u and clean_u.lower() not in existing:
                sources.append({"stt": next_stt, "username": clean_u, "count": 0, "expires": exp_str})
                existing.add(clean_u.lower())
                next_stt += 1
                added_count += 1

        if added_count > 0 or not os.path.exists(SOURCE_FILE):
            save_sources(sources)
        return added_count


def set_current_source(username, expires_text=""):
    """
    Thêm hoặc cập nhật nguồn, giữ nguyên số lượt đã ghi (thread-safe).
    """
    with _source_file_lock:
        sources = load_sources()
        clean_u = extract_username(username).strip().lstrip("@")
        count = max((item["count"] for item in sources
                     if item["username"].casefold() == clean_u.casefold()), default=0)
        sources = [item for item in sources if item['username'].casefold() != clean_u.casefold()]
        sources.append({"stt": 1, "username": clean_u, "count": count, "expires": expires_text})
        save_sources(sources)
        return count


async def cmd_setsource(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_admin(user_id):
        await update.message.reply_text("`⚠️ Chỉ Admin mới có quyền thiết lập nguồn!`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if not ctx.args:
        curr_user, curr_count = get_current_source_info()
        sources = load_sources()
        total_sources = len(sources)
        available = sum(1 for s in sources if s['count'] < 5)

        msg = (
            "*\\[ SYSTEM // UPDATE SOURCE \\]*\n"
            "\\-\n"
            "\\> `lệnh: /set <link_nguồn>`\n"
            "\\-\n"
            f"nguồn hiện tại: `@{esc(curr_user or 'Chưa có')}` \\(đã kích: `{curr_count}/5`\\)\n"
            f"tổng kho nguồn: `{total_sources}` nick \\(còn khả dụng: `{available}` nick\\)\n"
            "\\-\n"
            "\\> `ngưỡng cục bộ: 5 lượt; không phải xác nhận giới hạn từ dịch vụ.`"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
        return
    
    raw = ctx.args[0]
    username = extract_username(raw)
    if not username:
        await update.message.reply_text("`invalid input`", parse_mode=ParseMode.MARKDOWN_V2)
        return
        
    status_msg = await update.message.reply_text("`đang kiểm tra tài khoản...`", parse_mode=ParseMode.MARKDOWN_V2)
    
    uid = await locket.resolve_uid(username)
    if uid in ("IP_BLOCKED", "PROXY_ERROR"):
        await status_msg.edit_text("Chưa thể xác minh UID: kết nối bị lỗi hoặc truy cập bị từ chối.")
        return
    if not uid:
        await status_msg.edit_text("`không tìm thấy tài khoản.`", parse_mode=ParseMode.MARKDOWN_V2)
        return
        
    status = await locket.check_status(uid)
    if status.get("error"):
        await status_msg.edit_text("Chưa thể xác minh trạng thái: dịch vụ kiểm tra đang lỗi.")
        return
    expiry = _parse_source_expiry(status.get("expires", ""))
    has_gold = status.get("active") and expiry and expiry > datetime.now() + timedelta(days=30)
    
    if not has_gold:
        await status_msg.edit_text("`tài khoản này không có gold hoặc hạn còn quá ngắn (dưới 1 tháng).`", parse_mode=ParseMode.MARKDOWN_V2)
        return
        
    expires = status.get('expires', '')
    days_left = 0
    try:
        exp_dt = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S")
        days_left = (exp_dt - datetime.now()).days
    except Exception:
        pass
    exp_str = f"expires: {expires} (còn {days_left} ngày)" if expires else ""

    current_count = set_current_source(username, exp_str)
    msg = (
        "*\\[ SYSTEM // SOURCE UPDATED \\]*\n"
        "\\-\n"
        f"new source: `@{esc(username)}` \\(đã kích: `{current_count}/5`\\)\n"
        "\\-\n"
        "\\> `đã cập nhật current_source.txt và sắp xếp theo hạn.`\n"
        "\\> `Gold còn hạn; chưa xác nhận khả năng kích hoạt tiếp.`"
    )
    await status_msg.edit_text(msg, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_checksources(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    [Admin] Quét kiểm tra toàn bộ kho nguồn hiện tại trong current_source.txt.
    Tự động đánh dấu 5/5 và loại bỏ các nguồn bị 'Alias limit reached' khỏi kho.
    """
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_admin(user_id):
        await update.message.reply_text("`⚠️ Chỉ Admin mới có quyền thực hiện lệnh này!`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    status_msg = await update.message.reply_text(
        "`⏳ Bắt đầu quét kiểm tra giới hạn (Alias limit) toàn bộ kho nguồn...`",
        parse_mode=ParseMode.MARKDOWN_V2
    )

    sources = load_sources()
    total = len(sources)
    if total == 0:
        await status_msg.edit_text("`Kho nguồn hiện đang trống!`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    active_count = 0
    limit_count = 0
    error_count = 0
    last_ui_update = time.time()

    for idx, item in enumerate(sources, 1):
        u = item['username']
        cnt = item.get('count', 0)
        if cnt >= 5:
            limit_count += 1
            continue

        uid = await locket.resolve_uid(u)
        if not uid or uid in ("IP_BLOCKED", "PROXY_ERROR"):
            error_count += 1
            continue

        # Thử alias vào một probe id ngẫu nhiên để xác định limit
        probe_target = f"probe_{int(time.time())}_{idx}"
        ok, msg = await locket.aliasSubscriber(uid, probe_target)
        if ok:
            active_count += 1
        elif "alias limit" in (msg or "").lower():
            limit_count += 1
            item['count'] = 5
        else:
            error_count += 1

        if time.time() - last_ui_update > 3.0 or idx == total:
            last_ui_update = time.time()
            try:
                await status_msg.edit_text(
                    f"`⏳ Tiến độ check kho: {idx}/{total}`\n"
                    f"\\- ✅ `Khả dụng: {active_count}`\n"
                    f"\\- ⚠️ `Bị Limit (đã loại): {limit_count}`\n"
                    f"\\- ❌ `Lỗi mạng/IP: {error_count}`",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception:
                pass

        # Giữ khoảng nghỉ nhẹ để tránh bị RevenueCat chặn IP
        await asyncio.sleep(1.0)

    # Lưu lại danh sách đã làm sạch vào file
    save_sources(sources)

    report = (
        "*\\[ BÁO CÁO DỌN DẸP KHO NGUỒN \\]*\n"
        "\\-\n"
        f"• Tổng số nguồn đã kiểm tra: `{total}`\n"
        f"• ✅ Còn khả dụng \\(sống\\): `{active_count}`\n"
        f"• ⚠️ Bị chạm trần Limit \\(đã đánh dấu 5/5\\): `{limit_count}`\n"
        f"• ❌ Lỗi mạng / IP: `{error_count}`\n"
        "\\-\n"
        "\\> `Đã tự động cập nhật và loại bỏ các nick hết lượt khỏi current_source.txt!`"
    )
    await status_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_sodu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Kiểm tra số dư tài khoản của người dùng."""
    user_id = update.effective_user.id if update.effective_user else 0
    balance = user_db.get_user_balance(user_id)
    user_info = user_db.get_user_info(user_id)
    purchased = user_info.get("purchased_count", 0)

    admin_tag = " \\(Admin\\)" if is_admin(user_id) else ""
    msg = (
        "*\\[ THÔNG TIN TÀI KHOẢN \\]*\n"
        "\\-\n"
        f"ID: `{user_id}`{admin_tag}\n"
        f"Số dư: `{balance:,} VNĐ`\n"
        f"Đã nạp Gold: `{purchased}` lần\n"
        f"Giá nạp Gold: `{sepay_config.PRICE_PER_KICK:,} VNĐ`/lần\n"
        "\\-\n"
        "\\> `Dùng lệnh /nap <số_tiền> để nạp tiền tự động qua VietQR (SePay).`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_nap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tạo mã QR chuyển khoản VietQR tự động qua SePay để nhận mã Key nạp 1 lần Gold (1 tháng hoặc 1 năm)."""
    user_id = update.effective_user.id if update.effective_user else 0
    
    plan = "1m"
    if ctx.args:
        arg = ctx.args[0].lower().strip()
        if arg in ["1y", "1nam", "year", "nam"]:
            plan = "1y"

    if plan == "1y":
        amount = sepay_config.PRICE_1Y
        plan_title = "Gói 1 Năm (200 - 360 ngày)"
        content_code = f"LK{user_id}Y"
    else:
        amount = sepay_config.PRICE_1M
        plan_title = "Gói 1 Tháng (25 - 30 ngày)"
        content_code = f"LK{user_id}"

    bank = sepay_config.SEPAY_BANK_NAME
    acc_num = sepay_config.SEPAY_ACC_NUMBER
    acc_name = sepay_config.SEPAY_ACC_NAME

    # Tạo link VietQR SePay chuẩn (quét app ngân hàng tự điền đúng số tiền và nội dung)
    qr_url = f"https://qr.sepay.vn/img?bank={bank}&acc={acc_num}&amount={amount}&des={content_code}&template=compact"

    caption = (
        "*\\[ MÃ QR NẠP LOCKET GOLD \\(SEPAY\\) \\]*\n"
        "\\-\n"
        f"• Gói đăng ký: `{plan_title}`\n"
        f"• Ngân hàng: `{bank}`\n"
        f"• Số tài khoản: `{acc_num}`\n"
        f"• Chủ tài khoản: `{acc_name}`\n"
        f"• Số tiền: `{amount:,} VNĐ` \\(1 lần kích Gold\\)\n"
        f"• Nội dung CK: `{content_code}` \\(BẮT BUỘC GIỮ NGUYÊN\\)\n"
        "\\-\n"
        "\\> `Lưu ý: Mặc định là gói 1 Tháng (/nap). Nếu muốn mua gói 1 Năm, gõ: /nap 1y`\n"
        "\\> `Sau khi chuyển khoản thành công 3 - 10 giây, bot sẽ tự gửi Mã Key cho bạn!`"
    )

    try:
        await update.message.reply_photo(photo=qr_url, caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        # Nếu gửi ảnh thất bại, gửi dạng text
        await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN_V2)


async def check_sepay_loop(app):
    """
    Task chạy ngầm định kỳ mỗi 10 giây để kiểm tra giao dịch SePay và cộng tiền tự động.
    """
    import urllib.request
    import json

    seen_tx_ids = set()

    try:
        while True:
            await asyncio.sleep(10)
            api_key = sepay_config.SEPAY_API_KEY
            if not api_key or "DIEN_API_KEY" in api_key:
                continue

            url = "https://my.sepay.vn/userapi/transactions/list?limit=20"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }

            def _fetch_tx():
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                except Exception:
                    return None

            data = await asyncio.to_thread(_fetch_tx)
            if not data or not data.get("status"):
                continue

            transactions = data.get("messages", [])
            for tx in transactions:
                tx_id = tx.get("id")
                if not tx_id or tx_id in seen_tx_ids:
                    continue

                seen_tx_ids.add(tx_id)
                # Chỉ xử lý giao dịch tiền vào (amount_in > 0)
                amount_in = float(tx.get("amount_in") or 0)
                if amount_in <= 0:
                    continue

                desc = tx.get("transaction_content") or ""
                # Tìm mã LK<user_id> hoặc LK<user_id>Y trong nội dung chuyển khoản
                match = re.search(r'LK(\d+)(Y)?', desc, re.IGNORECASE)
                if match:
                    target_uid = int(match.group(1))
                    is_yearly = bool(match.group(2)) or (amount_in >= sepay_config.PRICE_1Y)
                    plan = "1y" if is_yearly else "1m"
                    amount_int = int(amount_in)

                    # Mỗi lần chuyển khoản tạo đúng 1 Key có 1 lượt kích hoạt
                    spins = 1

                    # Tạo key Redeem tự động (1 lượt) với gói tương ứng
                    key_code = user_db.create_key(
                        spins=spins,
                        created_by=target_uid,
                        note=f"SePay Tx #{tx_id} ({amount_int:,}đ)",
                        plan=plan
                    )
                    logger.info(f"[SePay] Đã tạo Key {key_code} (1 lượt, gói {plan}) cho User {target_uid}")

                    plan_label = "1 Năm (200 - 360 ngày)" if plan == "1y" else "1 Tháng (25 - 30 ngày)"

                    # Bắn thông báo trực tiếp kèm Mã Key Redeem cho khách
                    notify_msg = (
                        "*\\[ THANH TOÁN THÀNH CÔNG \\]*\n"
                        "\\-\n"
                        f"Số tiền: `+{amount_int:,} VNĐ`\n"
                        f"Mã giao dịch: `{tx_id}`\n"
                        f"Gói Gold: `{plan_label}`\n"
                        "Số lượt nạp Gold: `1` lần\n"
                        "\\-\n"
                        f"🔑 *MÃ KEY CỦA BẠN:*\n"
                        f"`{key_code}`\n"
                        "\\-\n"
                        "👉 *Cú pháp kích hoạt Locket Gold:*\n"
                        f"`/redeem {key_code} <link_locket>`\n\n"
                        f"_Ví dụ:_ `/redeem {key_code} https://locket.cam/pdlinhh`\n"
                        "\\-\n"
                        "\\> `Bạn có thể dùng ngay hoặc gửi mã Key này cho bạn bè/khách của bạn!`"
                    )
                    try:
                        await app.bot.send_message(
                            chat_id=target_uid,
                            text=notify_msg,
                            parse_mode=ParseMode.MARKDOWN_V2
                        )
                    except Exception as e:
                        logger.error(f"Lỗi gửi thông báo SePay tới {target_uid}: {e}")
    except asyncio.CancelledError:
        logger.info("check_sepay_loop đã dừng an toàn.")
        return


async def activate_gold(update: Update, ctx: ContextTypes.DEFAULT_TYPE, plan: str = "1m"):
    user_id = update.effective_user.id if update.effective_user else 0
    username_tg = update.effective_user.username or ""
    is_adm = is_admin(user_id)

    # Khóa theo từng user (Per-user lock) để tránh cùng 1 người spam click/gửi nhiều tin trong 1 giây
    user_lock = _user_locks[user_id]
    if user_lock.locked():
        await update.message.reply_text("`⏳ Yêu cầu trước của bạn đang được xử lý, vui lòng đợi hoàn tất trước khi gửi tiếp!`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    async with user_lock:
        price = sepay_config.PRICE_1Y if plan == "1y" else sepay_config.PRICE_1M
        deducted = False

        # 1. Trừ tiền nguyên tử trước (Atomic deduct) nếu không phải Admin để chống double-spending
        if not is_adm:
            if not user_db.deduct_user_balance(user_id, price):
                balance = user_db.get_user_balance(user_id)
                plan_name = "1 Năm" if plan == "1y" else "1 Tháng"
                msg = (
                    "*\\[ SỐ DƯ KHÔNG ĐỦ \\]*\n"
                    "\\-\n"
                    f"Giá nạp Locket Gold \\({plan_name}\\): `{price:,} VNĐ`/lần\n"
                    f"Số dư hiện tại: `{balance:,} VNĐ`\n"
                    f"Còn thiếu: `{price - balance:,} VNĐ`\n"
                    "\\-\n"
                    f"\\> `Vui lòng dùng lệnh /nap để nạp tiền tự động qua SePay.`"
                )
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
                return
            deducted = True

        msg = update.message
        raw = msg.text.strip()
        dest_username = extract_username(raw)

        if not dest_username:
            if deducted:
                user_db.refund_user_balance(user_id, price)
            await msg.reply_text("`invalid input. send a valid url.`", parse_mode=ParseMode.MARKDOWN_V2)
            return

        status_msg = await msg.reply_text("`resolving uid...`", parse_mode=ParseMode.MARKDOWN_V2)

        dest_uid = await locket.resolve_uid(dest_username)
        if dest_uid == "IP_BLOCKED":
            if deducted:
                user_db.refund_user_balance(user_id, price)
            await status_msg.edit_text("`⚠️ Locket báo Forbidden (Spam IP)!`", parse_mode=ParseMode.MARKDOWN_V2)
            return

        if not dest_uid:
            if deducted:
                user_db.refund_user_balance(user_id, price)
            await status_msg.edit_text("`user not found.`", parse_mode=ParseMode.MARKDOWN_V2)
            return

        # Kiểm tra trước: Nếu tài khoản đích đã có Gold và còn hạn dài
        dest_status = await locket.check_status(dest_uid)
        if dest_status and dest_status.get("active"):
            dest_exp = dest_status.get("expires", "")
            days_left = get_days_left_from_expires(dest_exp)
            # Cho phép ghi đè nếu:
            # 1. Nâng cấp lên gói 1 Năm (plan == '1y') và hạn hiện tại còn dưới 200 ngày
            # 2. Hoặc tài khoản sắp hết hạn (còn dưới 7 ngày)
            can_overwrite = (plan == "1y" and days_left < 200) or (days_left < 7)
            if not can_overwrite:
                if deducted:
                    user_db.refund_user_balance(user_id, price)
                dest_display = dest_username if dest_username.startswith("http") else f"@{dest_username}"
                await status_msg.edit_text(
                    f"`⚠️ Tài khoản {esc(dest_display)} hiện ĐÃ CÓ Locket Gold (còn {days_left} ngày, hạn đến {esc(dest_exp)})!`\n"
                    f"\\-\n\\> `Số dư đã được hoàn lại đầy đủ vào tài khoản của bạn.`\n"
                    f"\\> `(Gợi ý: Chỉ có thể nạp đè nếu nâng cấp lên gói 1 Năm hoặc khi còn dưới 7 ngày)`",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                return

        # 2. Đặt chỗ tài khoản nguồn an toàn đa luồng & Auto-failover nếu gặp Alias limit
        max_attempts = 5
        success = False
        result_msg = ""
        source_username = ""
        source_expires = ""
        new_used = 0

        for attempt in range(max_attempts):
            source_username, current_used = await reserve_source(plan=plan)
            if not source_username:
                source_username, current_used = await reserve_source(plan="all")
            if not source_username:
                break

            source_uid = await locket.resolve_uid(source_username)
            if not source_uid or source_uid in ("IP_BLOCKED", "PROXY_ERROR"):
                await release_source(source_username, success=False)
                continue

            src_status = await locket.check_status(source_uid)
            source_expires = src_status.get('expires', '2027-08-16 10:15:31') if src_status else '2027-08-16 10:15:31'

            success, result_msg = await locket.aliasSubscriber(source_uid, dest_uid)
            if success:
                new_used = await release_source(source_username, success=True)
                break
            else:
                is_limit = "alias limit" in (result_msg or "").lower()
                await release_source(source_username, success=False, exhausted=is_limit)
                if not is_limit:
                    break

        if success:
            remaining_balance_str = ""
            if not is_adm:
                new_bal = user_db.get_user_balance(user_id)
                remaining_balance_str = f"\nsố dư còn lại: `{new_bal:,} VNĐ`"

            dest_status = await locket.check_status(dest_uid)
            valid_exp = dest_status.get('expires') if (dest_status and dest_status.get('active')) else source_expires

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            remaining_info = ""
            try:
                exp_dt = datetime.strptime(valid_exp, "%Y-%m-%d %H:%M:%S")
                days_left = (exp_dt - datetime.now()).days
                if days_left > 0:
                    remaining_info = f" \\(còn {days_left} ngày\\)"
            except Exception:
                pass

            next_note = f" \\(lần {new_used}/5\\)"
            if new_used >= 5:
                next_user, _ = get_current_source_info()
                if next_user:
                    next_note = f" \\(đủ 5/5 -> chuyển sang @{esc(next_user)}\\)"
                else:
                    next_note = " \\(đủ 5/5 -> đã hết nguồn khả dụng\\)"

            admin_source_line = f"source: `@{esc(source_username)}`{next_note}\n" if is_adm else ""
            dest_display = dest_username if dest_username.startswith("http") else f"@{dest_username}"
            res = (
                "*\\[ locket gold by kaiTonguyen \\]*\n"
                "\\-\n"
                f"action: `transfer completed`\n"
                f"{admin_source_line}"
                f"dest:   `{esc(dest_display)}`\n"
                f"valid:  `{esc(valid_exp)}`{remaining_info}\n"
                f"time:   `{esc(now_str)}`{remaining_balance_str}\n"
                "\\-\n"
                "\\> `system updated in real-time.`\n"
                "`kaiTonguyen`"
            )
        else:
            if deducted:
                user_db.refund_user_balance(user_id, price)
            if "alias limit" in (result_msg or "").lower():
                res = (
                    "`⚠️ Giới hạn liên kết (Alias limit reached - tối đa 50 lần liên kết của RevenueCat).`\n"
                    "\\-\n"
                    "\\> `Số dư đã được hoàn lại đầy đủ. Vui lòng thử lại sau giây lát hoặc đổi tài khoản khác!`"
                )
            elif not source_username:
                res = "`⚠️ Kho nguồn trống hoặc toàn bộ tài khoản đã đạt tối đa 5 lần kích!`\n`Vui lòng liên hệ Admin để cập nhật nguồn mới.`"
            else:
                res = (
                    f"`lỗi từ hệ thống / limit: {esc(result_msg)}`\n"
                    "\\-\n"
                    "\\> `Số dư đã được hoàn lại vào tài khoản. Vui lòng thử lại sau giây lát hoặc liên hệ Admin.`"
                )

        await status_msg.edit_text(res, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_redeem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Kích hoạt Locket Gold bằng mã Key: /redeem <key> <link_locket>"""
    user_id = update.effective_user.id if update.effective_user else 0

    if len(ctx.args) < 2:
        msg = (
            "*\\[ KÍCH HOẠT GOLD BẰNG MÃ KEY \\]*\n"
            "\\-\n"
            "\\> `Cú pháp: /redeem <mã_key> <link_locket>`\n"
            "\\-\n"
            "• *Ví dụ:* `/redeem LK-GOLD-89ABCX https://locket.cam/username`\n"
            "\\-\n"
            "\\> `Bạn có thể dùng lệnh /nap để chuyển khoản lấy mã Key tự động!`"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Khóa theo từng user (Per-user lock) để tránh dùng chung 1 key đồng thời
    user_lock = _user_locks[user_id]
    if user_lock.locked():
        await update.message.reply_text("`⏳ Yêu cầu trước của bạn đang được xử lý, vui lòng đợi hoàn tất trước khi redeem tiếp!`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    async with user_lock:
        key_code = ctx.args[0].strip()
        target_raw = ctx.args[1].strip()
        dest_username = extract_username(target_raw)

        if not dest_username:
            await update.message.reply_text("`Đường link hoặc username Locket không hợp lệ!`", parse_mode=ParseMode.MARKDOWN_V2)
            return

        status_msg = await update.message.reply_text("`Đang kiểm tra mã Key và chuẩn bị kích hoạt Gold...`", parse_mode=ParseMode.MARKDOWN_V2)

        # 1. Xác thực và trừ 1 lượt của mã Key nguyên tử (Thread-safe)
        valid_key, key_msg, plan = user_db.use_key(key_code, user_id, dest_username)
        if not valid_key:
            await status_msg.edit_text(f"`❌ {esc(key_msg)}`", parse_mode=ParseMode.MARKDOWN_V2)
            return

        # Kiểm tra trước: UID và trạng thái của tài khoản đích
        dest_uid = await locket.resolve_uid(dest_username)
        if dest_uid == "IP_BLOCKED":
            user_db.refund_key(key_code)
            await status_msg.edit_text("`⚠️ Locket báo Forbidden (Spam IP)! Lượt key đã được hoàn lại.`", parse_mode=ParseMode.MARKDOWN_V2)
            return

        if not dest_uid:
            user_db.refund_key(key_code)
            await status_msg.edit_text("`Không tìm thấy tài khoản Locket đích! Lượt key đã được hoàn lại.`", parse_mode=ParseMode.MARKDOWN_V2)
            return

        # Kiểm tra trước: Nếu nick đích ĐÃ CÓ Gold và còn hạn dài
        dest_status = await locket.check_status(dest_uid)
        if dest_status and dest_status.get("active"):
            dest_exp = dest_status.get("expires", "")
            days_left = get_days_left_from_expires(dest_exp)
            # Cho phép ghi đè nếu:
            # 1. Nâng cấp lên gói 1 Năm (plan == '1y') và hạn hiện tại còn dưới 200 ngày
            # 2. Hoặc tài khoản sắp hết hạn (còn dưới 7 ngày)
            can_overwrite = (plan == "1y" and days_left < 200) or (days_left < 7)
            if not can_overwrite:
                user_db.refund_key(key_code)
                dest_display = dest_username if dest_username.startswith("http") else f"@{dest_username}"
                await status_msg.edit_text(
                    f"`⚠️ Tài khoản {esc(dest_display)} hiện ĐÃ CÓ Locket Gold (còn {days_left} ngày, hạn đến {esc(dest_exp)})!`\n"
                    f"\\-\n\\> `Lượt Key chưa bị trừ và đã được hoàn lại đầy đủ.`\n"
                    f"\\> `(Gợi ý: Chỉ có thể nạp đè nếu Key là gói 1 Năm hoặc nick còn dưới 7 ngày)`",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                return

        # 2. Đặt chỗ tài khoản nguồn & Tự động thử lại (Auto-failover) nếu gặp Alias limit
        max_attempts = 5
        success = False
        result_msg = ""
        source_username = ""
        source_expires = ""
        new_used = 0

        for attempt in range(max_attempts):
            source_username, current_used = await reserve_source(plan=plan)
            if not source_username:
                source_username, current_used = await reserve_source(plan="all")
            if not source_username:
                break

            source_uid = await locket.resolve_uid(source_username)
            if not source_uid or source_uid in ("IP_BLOCKED", "PROXY_ERROR"):
                await release_source(source_username, success=False)
                continue

            src_status = await locket.check_status(source_uid)
            source_expires = src_status.get('expires', '2027-08-16 10:15:31') if src_status else '2027-08-16 10:15:31'

            success, result_msg = await locket.aliasSubscriber(source_uid, dest_uid)
            if success:
                new_used = await release_source(source_username, success=True)
                break
            else:
                is_limit = "alias limit" in (result_msg or "").lower()
                await release_source(source_username, success=False, exhausted=is_limit)
                if not is_limit:
                    break

        if success:
            dest_status = await locket.check_status(dest_uid)
            valid_exp = dest_status.get('expires') if (dest_status and dest_status.get('active')) else source_expires

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            remaining_info = ""
            try:
                exp_dt = datetime.strptime(valid_exp, "%Y-%m-%d %H:%M:%S")
                days_left = (exp_dt - datetime.now()).days
                if days_left > 0:
                    remaining_info = f" \\(còn {days_left} ngày\\)"
            except Exception:
                pass

            dest_display = dest_username if dest_username.startswith("http") else f"@{dest_username}"
            res = (
                "*\\[ LOCKET GOLD REDEEM SUCCESS \\]*\n"
                "\\-\n"
                f"key:    `{esc(key_code.upper())}`\n"
                f"dest:   `{esc(dest_display)}`\n"
                f"valid:  `{esc(valid_exp)}`{remaining_info}\n"
                f"time:   `{esc(now_str)}`\n"
                "\\-\n"
                f"\\> `{esc(key_msg)}`\n"
                "\\> `Chúc mừng bạn đã kích hoạt Locket Gold thành công!`"
            )
        else:
            user_db.refund_key(key_code)
            if "alias limit" in (result_msg or "").lower():
                res = (
                    "`⚠️ Giới hạn liên kết (Alias limit reached - tối đa 50 lần liên kết của RevenueCat).`\n"
                    "\\-\n"
                    "\\> `Lượt Key đã được hoàn lại đầy đủ. Vui lòng thử lại sau giây lát hoặc đổi tài khoản khác!`"
                )
            elif not source_username:
                res = "`⚠️ Kho nguồn trống hoặc toàn bộ tài khoản đã đạt tối đa 5 lần kích!`\n`Lượt key đã được hoàn lại. Vui lòng liên hệ Admin.`"
            else:
                res = (
                    f"`Lỗi từ hệ thống / limit: {esc(result_msg)}`\n"
                    "\\-\n"
                    "\\> `Lượt kích hoạt đã được hoàn lại vào Key của bạn. Vui lòng thử lại sau!`"
                )

        await status_msg.edit_text(res, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_genkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin tạo mã key: /genkey [số_lượt] [1m|1y]"""
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_admin(user_id):
        await update.message.reply_text("`⚠️ Chỉ Admin mới có quyền tạo Key!`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    spins = 1
    plan = "1m"
    if ctx.args:
        try:
            spins = int(ctx.args[0])
        except ValueError:
            spins = 1
        if len(ctx.args) >= 2:
            arg2 = ctx.args[1].lower().strip()
            if arg2 in ["1y", "1nam", "year", "nam"]:
                plan = "1y"
            else:
                plan = "1m"

    key_code = user_db.create_key(spins=spins, created_by=user_id, note="Admin manual gen", plan=plan)
    plan_display = "1 Năm (Ưu tiên 200 - 360 ngày)" if plan == "1y" else "1 Tháng (Ưu tiên 25 - 30 ngày)"
    msg = (
        "*\\[ ADMIN // TẠO KEY THÀNH CÔNG \\]*\n"
        "\\-\n"
        f"Mã Key: `{key_code}`\n"
        f"Gói hạn: `{esc(plan_display)}`\n"
        f"Số lượt dùng: `{spins}` lần\n"
        "\\-\n"
        f"\\> `Cú pháp khách dùng: /redeem {key_code} <link_locket>`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Kiểm tra trạng thái Locket Gold của một tài khoản (Dành cho tất cả mọi người)."""
    if not ctx.args:
        msg = (
            "*\\[ system // check status \\]*\n"
            "\\-\n"
            "\\> `cú pháp: /check <link_hoặc_username>`\n"
            "\\-\n"
            "ví dụ: `/check pdlinhh`"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
        return

    raw = ctx.args[0]
    username = extract_username(raw)
    if not username:
        await update.message.reply_text("`invalid input`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    status_msg = await update.message.reply_text("`đang kiểm tra tài khoản...`", parse_mode=ParseMode.MARKDOWN_V2)

    uid = await locket.resolve_uid(username)
    if uid == "IP_BLOCKED":
        await status_msg.edit_text("`⚠️ Locket báo Forbidden (Spam IP)!`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if not uid:
        await status_msg.edit_text("`user not found.`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    status = await locket.check_status(uid)
    if status.get("error"):
        await status_msg.edit_text("Chưa thể kiểm tra: dịch vụ bị lỗi hoặc đang tạm dừng sau khi bị từ chối. Vui lòng thử lại sau.")
        return
    if status.get("active"):
        expires = status.get("expires", "Unknown")
        remaining_days_text = ""
        try:
            if expires not in ("Unknown", "N/A", ""):
                exp_dt = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S")
                days_left = (exp_dt - datetime.now()).days
                if days_left > 0:
                    remaining_days_text = f" \\(còn {days_left} ngày\\)"
                elif days_left == 0:
                    remaining_days_text = " \\(hết hạn hôm nay\\)"
                else:
                    remaining_days_text = f" \\(đã hết hạn {abs(days_left)} ngày trước\\)"
        except Exception:
            pass

        # Tự động thêm vào current_source.txt nếu tài khoản có Gold và còn hạn
        added_note = ""
        is_valid_date = False
        try:
            if expires not in ("Unknown", "N/A", ""):
                exp_dt = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S")
                if exp_dt > datetime.now():
                    is_valid_date = True
        except Exception:
            pass

        if is_valid_date:
            added = add_eligible_sources([(username, expires, days_left)])
            if added > 0:
                added_note = "\n\\> ``"

        user_display = username if username.startswith("http") else f"@{username}"
        res = (
            "*\\[ locket gold info \\]*\n"
            "\\-\n"
            f"user:    `{esc(user_display)}`\n"
            f"status:  `ACTIVE (Có Gold)`\n"
            f"expires: `{esc(expires)}`{remaining_days_text}\n"
            "\\-\n"
            f"\\> `tài khoản đang hoạt động bình thường.`{added_note}"
        )
    else:
        user_display = username if username.startswith("http") else f"@{username}"
        res = (
            "*\\[ locket gold info \\]*\n"
            "\\-\n"
            f"user:    `{esc(user_display)}`\n"
            f"status:  `INACTIVE (Chưa có Gold)`\n"
            "\\-\n"
            "\\> `tài khoản này hiện chưa kích hoạt Gold.`"
        )

    await status_msg.edit_text(res, parse_mode=ParseMode.MARKDOWN_V2)


async def check_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Kiểm tra trạng thái Gold hàng loạt từ file .txt (Ai cũng dùng được, nhưng chỉ Admin mới được tự động thêm vào kho nguồn)."""
    user_id = update.effective_user.id if update.effective_user else 0
    msg = update.message
    doc = None

    # Trường hợp 1: Tin nhắn hiện tại đính kèm file
    if msg.document:
        doc = msg.document
    # Trường hợp 2: Gõ lệnh /chk khi reply vào một tin nhắn có file
    elif msg.reply_to_message and msg.reply_to_message.document:
        doc = msg.reply_to_message.document

    if not doc:
        help_msg = (
            "*\\[ HƯỚNG DẪN DÙNG LỆNH /chk \\]*\n"
            "\\-\n"
            "\\> *Cách 1:* Gửi file `\\.txt` lên và nhập chú thích `/chk`\\.\n"
            "\\> *Cách 2:* Reply \\(trả lời\\) vào file `\\.txt` đã gửi và gõ `/chk`\\.\n"
            "\\> *Cách 3:* Gửi thẳng file `\\.txt` vào bot\\."
        )
        await msg.reply_text(help_msg, parse_mode=ParseMode.MARKDOWN_V2)
        return

    file_name = doc.file_name or "links.txt"
    if not file_name.lower().endswith(".txt"):
        await msg.reply_text("`⚠️ Vui lòng gửi file định dạng .txt!`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    proxy_url = os.environ.get("CHK_PROXY_URL", "").strip() or None
    if proxy_url and not locket.is_valid_proxy_url(proxy_url):
        await msg.reply_text(
            "`CHK_PROXY_URL không hợp lệ. Xóa biến này để dùng kết nối trực tiếp hoặc cấu hình proxy HTTP/HTTPS hợp lệ.`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    status_msg = await msg.reply_text("`⏳ Đang tải và đọc danh sách tài khoản từ file...`", parse_mode=ParseMode.MARKDOWN_V2)

    try:
        # Tải file từ Telegram
        tg_file = await doc.get_file()
        byte_data = await tg_file.download_as_bytearray()
        content = byte_data.decode("utf-8", errors="ignore")
    except Exception as e:
        await status_msg.edit_text(f"`Lỗi khi tải file: {esc(str(e))}`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Lọc danh sách username/link
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        await status_msg.edit_text("`File rỗng hoặc không có dòng nào!`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    total = len(lines)
    await status_msg.edit_text(f"`⏳ Tiến độ: 0/{total} | 👑 Đủ ĐK (Active còn date): 0 | ❌ Hết date/Không có: 0`", parse_mode=ParseMode.MARKDOWN_V2)

    eligible_accounts = []   # Đủ điều kiện: ACTIVE (Có Gold) VÀ CÒN HẠN DÙNG (date > now)
    expired_accounts = []    # Có gói nhưng ĐÃ HẾT HẠN (expired)
    no_gold_accounts = []    # Chưa từng có Gold
    not_found_accounts = []  # Không tìm thấy / lỗi link

    last_update_time = time.time()

    for idx, raw in enumerate(lines, 1):
        username = extract_username(raw)
        uid = await locket.resolve_uid(username, proxy_url=proxy_url)

        # Phân loại trạng thái
        if uid == "PROXY_ERROR":
            await status_msg.edit_text(
                "`⚠️ Proxy /chk không kết nối được hoặc sai thông tin xác thực. Đã dừng kiểm tra.`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        if uid == "IP_BLOCKED":
            await status_msg.edit_text(
                "`⚠️ IP proxy đang bị Locket từ chối. Đã dừng kiểm tra.`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        if not uid:
            not_found_accounts.append(username)
        else:
            status = await locket.check_status(uid, proxy_url=proxy_url)
            if status.get("error") == "PROXY_ERROR":
                await status_msg.edit_text(
                    "`⚠️ Proxy /chk bị lỗi khi kiểm tra trạng thái. Đã dừng kiểm tra.`",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                return
            if status.get("error") == "IP_BLOCKED":
                await status_msg.edit_text(
                    "`⚠️ IP proxy bị dịch vụ kiểm tra trạng thái từ chối. Đã dừng kiểm tra.`",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                return
            if status.get("active"):
                expires = status.get("expires", "Unknown")
                is_valid_date = False
                days_left = 0
                try:
                    if expires not in ("Unknown", "N/A", ""):
                        exp_dt = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S")
                        days_left = (exp_dt - datetime.now()).days
                        if exp_dt > datetime.now():
                            is_valid_date = True
                except Exception:
                    pass

                if is_valid_date and days_left >= 10:
                    # ĐỦ ĐIỀU KIỆN: ACTIVE + CÒN DATE >= 10 NGÀY
                    eligible_accounts.append((username, expires, days_left))
                else:
                    # ĐÃ HẾT HẠN hoặc DƯỚI 10 NGÀY (không đủ đk nạp kho)
                    expired_accounts.append((username, expires, days_left))
            else:
                no_gold_accounts.append(username)

        # Cập nhật tiến độ mỗi 5 tài khoản hoặc mỗi 3 giây
        if time.time() - last_update_time >= 3 or idx == total:
            try:
                not_eligible = len(expired_accounts) + len(no_gold_accounts) + len(not_found_accounts)
                await status_msg.edit_text(
                    f"`⏳ Tiến độ: {idx}/{total} | 👑 Đủ ĐK (Active còn date): {len(eligible_accounts)} | ❌ Hết date/Không có: {not_eligible}`",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                last_update_time = time.time()
            except Exception:
                pass

    # Tạo báo cáo tóm tắt
    not_eligible = len(expired_accounts) + len(no_gold_accounts) + len(not_found_accounts)
    summary = (
        "*\\[ KẾT QUẢ KIỂM TRA TÀI KHOẢN \\]*\n"
        "\\-\n"
        f"\\> `⏳ Tiến độ: {total}/{total} | 👑 Đủ ĐK (Active còn date): {len(eligible_accounts)} | ❌ Hết date/Không có: {not_eligible}`\n"
        "\\-\n"
        f"file:                 `{esc(file_name)}`\n"
        f"tổng kiểm:            `{total}` tài khoản\n"
        f"👑 đủ đk \\(còn date\\):  `{len(eligible_accounts)}` tài khoản\n"
        f"⏰ hết hạn \\(hết date\\): `{len(expired_accounts)}` tài khoản\n"
        f"⚪ chưa có gold:       `{len(no_gold_accounts)}` tài khoản\n"
        f"❌ không tìm thấy:     `{len(not_found_accounts)}` tài khoản\n"
        "\\-\n"
    )

    # Tự động nạp tài khoản ĐỦ ĐIỀU KIỆN vào current_source.txt
    added_to_sources = 0
    if eligible_accounts:
        added_to_sources = add_eligible_sources(eligible_accounts)

    if eligible_accounts:
        summary += "*DANH SÁCH TÀI KHOẢN ĐỦ ĐK (ACTIVE & CÒN DATE):*\n"
        for u, exp, days_left in eligible_accounts[:15]:
            user_display = u if u.startswith("http") else f"@{u}"
            days_str = f" \\(còn {days_left} ngày\\)" if days_left > 0 else " \\(hôm nay\\)"
            summary += f"• `{esc(user_display)}` \\- `{esc(exp)}`{days_str}\n"
        if len(eligible_accounts) > 15:
            summary += f"_và {len(eligible_accounts) - 15} tài khoản khác trong file đính kèm\\._\n"
        summary += "\\-\n"
        summary += f"\\> `✨ Đã tự động cập nhật {added_to_sources} tài khoản mới vào current_source.txt!`\n"
        summary += "\\> `hệ thống đã xuất toàn bộ tài khoản đủ điều kiện vào file đính kèm.`"
    else:
        summary += "\\> `không tìm thấy tài khoản nào đủ điều kiện (Active và còn date).`"

    try:
        await status_msg.edit_text(summary, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        pass

    # Xuất file danh sách
    import io

    # Xuất file danh sách TÀI KHOẢN ĐỦ ĐIỀU KIỆN (ACTIVE & CÒN DATE)
    if eligible_accounts:
        eligible_content = "# DANH SÁCH TÀI KHOẢN ĐỦ ĐIỀU KIỆN (ACTIVE & CÒN DATE)\n"
        eligible_content += f"# Tổng cộng: {len(eligible_accounts)} tài khoản\n"
        eligible_content += "=" * 55 + "\n\n"
        for u, exp, days_left in eligible_accounts:
            user_display = u if u.startswith("http") else f"@{u}"
            user_link = u if u.startswith("http") else f"https://locket.cam/{u}"
            eligible_content += f"user:         {user_display}\n"
            eligible_content += f"status:       ACTIVE\n"
            eligible_content += f"expires:      {exp} (còn {days_left} ngày)\n"
            eligible_content += f"link:         {user_link}\n"
            eligible_content += f"{'-' * 55}\n"

        file_bytes = io.BytesIO(eligible_content.encode("utf-8"))
        file_bytes.name = f"eligible_active_{file_name}"
        try:
            await update.message.reply_document(
                document=file_bytes,
                caption=f"👑 DANH SÁCH {len(eligible_accounts)} TÀI KHOẢN ĐỦ ĐIỀU KIỆN (ACTIVE & CÒN DATE)"
            )
        except Exception as e:
            logger.error(f"Lỗi gửi file: {e}")


# ──────────────────────────────────────────────────────────────────────────────

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Quét link Locket từ bình luận video TikTok qua Apify Cloud (Mọi người đều dùng được)."""
    user_id = update.effective_user.id if update.effective_user else 0

    if not ctx.args:
        msg = (
            "*\\[ QUÉT LINK LOCKET TỪ TIKTOK / THREADS \\]*\n"
            "\\-\n"
            "\\> `Cú pháp: /scan <link_tiktok_hoặc_threads>`\n"
            "\\-\n"
            "• *Ví dụ TikTok:* `/scan https://www.tiktok.com/@_iam.thm/video/7501540693945355537`\n"
            "• *Ví dụ Threads:* `/scan https://www.threads.com/share/D6gtqGHn1/`\n"
            "\\-\n"
            "\\> `Hệ thống sẽ tự động nhận diện và quét toàn bộ bình luận!`"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
        return

    target_url = ctx.args[0].strip()

    status_msg = await update.message.reply_text(
        "`⏳ Đang tự động quét toàn bộ bình luận của video Vui lòng chờ trong giây lát!`",
        parse_mode=ParseMode.MARKDOWN_V2
    )

    def run_scan_task():
        # Gọi scraper tự động: Direct API siêu tốc cho TikTok (0đ, không lưu file rác trên máy) & Chrome cho Threads
        return scan_locket.scrape_comments_auto(target_url, max_comments=1000, save_file=False)

    # Chạy scraper trên background thread để không chặn bot telegram
    links, err = await asyncio.to_thread(run_scan_task)

    if err:
        await status_msg.edit_text(f"`❌ Lỗi khi quét bình luận: {esc(err)}`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if not links:
        await status_msg.edit_text("`[!] Quá trình cào hoàn tất nhưng không tìm thấy link Locket nào trong bình luận.`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    target_id = scan_locket.extract_target_id(target_url)
    total = len(links)

    # TỰ ĐỘNG CHECK HÀNG LOẠT NGAY SAU KHI SCAN
    await status_msg.edit_text(
        f"`⏳ Tiến độ: 0/{total} | 👑 Đủ ĐK (Active còn date): 0 | ❌ Hết date/Không có: 0`",
        parse_mode=ParseMode.MARKDOWN_V2
    )

    eligible_accounts = []   # Đủ điều kiện: ACTIVE + CÒN DATE >= 10 NGÀY
    expired_accounts = []    # Có gói nhưng ĐÃ HẾT HẠN hoặc date < 10 ngày
    no_gold_accounts = []    # Chưa từng có Gold
    not_found_accounts = []  # Không tìm thấy / lỗi link

    last_update_time = time.time()

    for idx, raw in enumerate(links, 1):
        username = extract_username(raw)
        uid = await locket.resolve_uid(username)

        # Phân loại trạng thái
        if uid == "IP_BLOCKED":
            await status_msg.edit_text(f"Đã dừng tại {idx}/{total}: dịch vụ từ chối hoặc đang trong thời gian tạm nghỉ. Chưa thể kết luận các tài khoản còn lại.")
            return
        if not uid:
            not_found_accounts.append(username)
        else:
            status = await locket.check_status(uid)
            if status.get("error"):
                await status_msg.edit_text(f"Đã dừng tại {idx}/{total}: không thể lấy trạng thái từ dịch vụ.")
                return
            if status.get("active"):
                expires = status.get("expires", "Unknown")
                is_valid_date = False
                days_left = 0
                try:
                    if expires not in ("Unknown", "N/A", ""):
                        exp_dt = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S")
                        days_left = (exp_dt - datetime.now()).days
                        if exp_dt > datetime.now():
                            is_valid_date = True
                except Exception:
                    pass

                if is_valid_date and days_left >= 10:
                    # ĐỦ ĐIỀU KIỆN: ACTIVE + CÒN DATE >= 10 NGÀY
                    eligible_accounts.append((username, expires, days_left))
                else:
                    # ĐÃ HẾT HẠN hoặc DƯỚI 10 NGÀY
                    expired_accounts.append((username, expires, days_left))
            else:
                no_gold_accounts.append(username)

        # Cập nhật tiến độ mỗi 5 tài khoản hoặc mỗi 3 giây
        if time.time() - last_update_time >= 3 or idx == total:
            try:
                not_eligible = len(expired_accounts) + len(no_gold_accounts) + len(not_found_accounts)
                await status_msg.edit_text(
                    f"`⏳ Tiến độ: {idx}/{total} | 👑 Đủ ĐK (Active còn date): {len(eligible_accounts)} | ❌ Hết date/Không có: {not_eligible}`",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                last_update_time = time.time()
            except Exception:
                pass

    # Tự động nạp tài khoản ĐỦ ĐIỀU KIỆN vào current_source.txt
    added_to_sources = 0
    if eligible_accounts:
        added_to_sources = add_eligible_sources(eligible_accounts)

    not_eligible = len(expired_accounts) + len(no_gold_accounts) + len(not_found_accounts)
    summary = (
        "*\\[ KẾT QUẢ QUÉT & CHECK TỰ ĐỘNG \\]*\n"
        "\\-\n"
        f"\\> `⏳ Tiến độ: {total}/{total} | 👑 Đủ ĐK (Active còn date): {len(eligible_accounts)} | ❌ Hết date/Không có: {not_eligible}`\n"
        "\\-\n"
        f"nguồn quét:            `{esc(target_id)}`\n"
        f"tổng link quét được:   `{total}` tài khoản\n"
        f"👑 đủ đk \\(date \\>= 10d\\): `{len(eligible_accounts)}` tài khoản\n"
        f"⏰ hết hạn / dưới 10d:  `{len(expired_accounts)}` tài khoản\n"
        f"⚪ chưa có gold:       `{len(no_gold_accounts)}` tài khoản\n"
        f"❌ không tìm thấy:     `{len(not_found_accounts)}` tài khoản\n"
        "\\-\n"
    )

    if eligible_accounts:
        summary += "*DANH SÁCH TÀI KHOẢN ĐỦ ĐK (ACTIVE & CÒN DATE >= 10 NGÀY):*\n"
        for u, exp, days_left in eligible_accounts[:15]:
            user_display = u if u.startswith("http") else f"@{u}"
            days_str = f" \\(còn {days_left} ngày\\)" if days_left > 0 else " \\(hôm nay\\)"
            summary += f"• `{esc(user_display)}` \\- `{esc(exp)}`{days_str}\n"
        if len(eligible_accounts) > 15:
            summary += f"_và {len(eligible_accounts) - 15} tài khoản khác trong file đính kèm\\._\n"
        summary += "\\-\n"
        summary += f"\\> `✨ Đã tự động cập nhật {added_to_sources} tài khoản mới vào current_source.txt!`\n"
    else:
        summary += "\\> `không tìm thấy tài khoản nào đủ điều kiện (Active và date >= 10 ngày).`\n"
    summary += "\\-\n\\> `📁 Đã gửi 2 file: 1 file chứa link quét và 1 file KẾT QUẢ CHECK HÀNG LOẠT.`"

    try:
        await status_msg.edit_text(summary, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        pass

    # Gửi kèm 2 FILE riêng biệt:
    # FILE 1: Danh sách toàn bộ link quét được
    txt_links = "\n".join(links) + "\n"
    file_links = io.BytesIO(txt_links.encode("utf-8"))
    file_links.name = f"locket_scanned_{target_id}.txt"

    try:
        await update.message.reply_document(
            document=file_links,
            caption=f"📁 File 1: Danh sách {len(links)} link Locket quét được từ bình luận"
        )
    except Exception as e:
        logger.error(f"Lỗi gửi file scan: {e}")

    # FILE 2: KẾT QUẢ CHECK HÀNG LOẠT
    check_report = "# KẾT QUẢ CHECK HÀNG LOẠT (TỰ ĐỘNG SAU KHI SCAN)\n"
    check_report += f"# Nguồn quét: {target_id}\n"
    check_report += f"# Tổng kiểm tra: {total} tài khoản\n"
    check_report += f"# Đủ điều kiện (Active & date >= 10 ngày): {len(eligible_accounts)}\n"
    check_report += f"# Hết hạn hoặc date < 10 ngày: {len(expired_accounts)}\n"
    check_report += f"# Chưa có Gold: {len(no_gold_accounts)}\n"
    check_report += f"# Không tìm thấy: {len(not_found_accounts)}\n"
    check_report += "=" * 60 + "\n\n"
    check_report += "[1. ĐỦ ĐIỀU KIỆN (ĐÃ NẠP VÀO CURRENT_SOURCE.TXT)]:\n"
    for u, exp, days_left in eligible_accounts:
        user_display = u if u.startswith("http") else f"@{u}"
        user_link = u if u.startswith("http") else f"https://locket.cam/{u}"
        check_report += f"{user_display} | ACTIVE | expires: {exp} (còn {days_left} ngày) | {user_link}\n"
    check_report += "\n" + "=" * 60 + "\n"
    check_report += "[2. HẾT HẠN HOẶC DATE < 10 NGÀY]:\n"
    for u, exp, days_left in expired_accounts:
        user_display = u if u.startswith("http") else f"@{u}"
        check_report += f"{user_display} | EXPIRED/LOW_DATE | expires: {exp} (còn {days_left} ngày)\n"
    check_report += "\n" + "=" * 60 + "\n"
    check_report += "[3. CHƯA CÓ GOLD]:\n"
    for u in no_gold_accounts:
        user_display = u if u.startswith("http") else f"@{u}"
        check_report += f"{user_display}\n"
    check_report += "\n" + "=" * 60 + "\n"
    check_report += "[4. KHÔNG TÌM THẤY / LỖI LINK]:\n"
    for u in not_found_accounts:
        user_display = u if u.startswith("http") else f"@{u}"
        check_report += f"{user_display}\n"

    file_check = io.BytesIO(check_report.encode("utf-8"))
    file_check.name = f"ket_qua_check_hang_loat_{target_id}.txt"

    try:
        await update.message.reply_document(
            document=file_check,
            caption=f"📊 File 2: KẾT QUẢ CHECK HÀNG LOẠT ({len(eligible_accounts)} đủ ĐK / {total} tổng)"
        )
    except Exception as e:
        logger.error(f"Lỗi gửi file check: {e}")

    # Tự động dọn dẹp sạch sẽ: Xóa file rác locket_scanned_*.txt nếu có trên ổ cứng
    for f_name in [f"locket_scanned_{target_id}.txt", f"ket_qua_check_hang_loat_{target_id}.txt"]:
        if os.path.exists(f_name):
            try:
                os.remove(f_name)
            except Exception:
                pass


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Bất kỳ ai gửi link/user nếu có số dư (hoặc là Admin) sẽ được tự động nạp Gold."""
    await activate_gold(update, ctx)


async def limited_cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cho phép nhiều người /check đồng thời trong giới hạn an toàn."""
    async with _check_slots:
        await cmd_check(update, ctx)


async def serialized_check_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Chỉ xử lý một file /chk tại một thời điểm."""
    async with _bulk_check_lock:
        await check_file(update, ctx)


async def limited_cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Giới hạn số tác vụ quét nặng nhưng vẫn cho phép chúng chạy đồng thời."""
    if not ctx.args:
        await cmd_scan(update, ctx)
        return

    if _scan_slots.locked():
        await update.message.reply_text(
            "`⏳ Máy quét đang bận, yêu cầu của bạn đã được xếp hàng...`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    async with _scan_slots:
        await cmd_scan(update, ctx)


# ──────────────────────────────────────────────────────────────────────────────

async def post_init(application):
    """Khởi chạy background task SePay polling khi bot vừa start."""
    asyncio.create_task(check_sepay_loop(application))


def main():
    # Tăng request timeout để không bao giờ bị ReadTimeout khi tải/gửi file
    t_request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(t_request)
        .post_init(post_init)
        .concurrent_updates(MAX_CONCURRENT_UPDATES)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("nap", cmd_nap))
    app.add_handler(CommandHandler("sodu", cmd_sodu))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("check", limited_cmd_check))
    app.add_handler(CommandHandler("chk", serialized_check_file))
    app.add_handler(CommandHandler("scan", limited_cmd_scan))
    app.add_handler(CommandHandler("set", cmd_setsource))
    app.add_handler(CommandHandler("setsource", cmd_setsource))
    app.add_handler(CommandHandler("checksources", cmd_checksources))
    app.add_handler(CommandHandler("cleansources", cmd_checksources))
    app.add_handler(MessageHandler(filters.Document.ALL, serialized_check_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("=" * 52)
    print("  Locket Bot (Auto SePay & Gold)  --  dang chay...")
    print(
        f"  Concurrent updates: {MAX_CONCURRENT_UPDATES} | "
        f"check slots: {MAX_CONCURRENT_CHECKS} | scan slots: {MAX_CONCURRENT_SCANS}"
    )
    print("  Nhan Ctrl+C de dung.")
    print("=" * 52)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
