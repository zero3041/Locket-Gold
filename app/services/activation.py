"""Alias-based Gold activation.

Flow for one activation:
  1. Resolve the destination Locket account (UID).
  2. Refuse when the destination already has a long-lived Gold (unless it is a
     1-year key and the remaining term is short, or the account is expiring).
  3. Reserve the best source slot for the plan (1m prefers 25-30 days left,
     1y prefers 200-360 days left).
  4. Alias the source subscriber onto the destination UID.
  5. On success bump the source counter; on "alias limit" retire the source and
     retry with the next one; on IP block stop immediately.
"""

import time

from app import database as db
from app.services import locket

MAX_ATTEMPTS = 5

# Overwrite rules: a 1-year key may overwrite a term shorter than this, and any
# key may overwrite an account that expires within this many days.
YEAR_OVERWRITE_DAYS = 200
EXPIRING_OVERWRITE_DAYS = 7


def _result(ok, code, message, **extra):
    payload = {"ok": ok, "code": code, "message": message}
    payload.update(extra)
    return payload


def _days_left(expires_text):
    return locket.plan_days_left(expires_text)


async def _notify(log, message):
    if not log:
        return
    try:
        result = log(message)
        if hasattr(result, "__await__"):
            await result
    except Exception:
        pass


async def activate(dest_input, plan="1m", proxy_url=None, log=None):
    """Activate Gold for `dest_input` using the source pool.

    Returns a dict with ok/code/message and, on success, uid/username/expires/
    days_left/source/source_used.
    """
    plan = "1y" if (plan or "").lower() == "1y" else "1m"

    dest_uid = await locket.resolve_uid(dest_input, proxy_url=proxy_url)
    if dest_uid == "IP_BLOCKED":
        return _result(False, "ip_blocked", "Locket đang tạm chặn IP (403). Vui lòng thử lại sau ít phút.")
    if dest_uid == "PROXY_ERROR":
        return _result(False, "proxy_error", "Không kết nối được qua proxy.")
    if not dest_uid:
        return _result(False, "not_found", "Không tìm thấy tài khoản Locket.")

    await _notify(log, "Đã xác minh tài khoản đích, đang kiểm tra trạng thái Gold...")
    dest_status = await locket.check_status(dest_uid, proxy_url=proxy_url)
    if dest_status.get("error") in ("IP_BLOCKED",):
        return _result(False, "ip_blocked", "Dịch vụ kiểm tra đang tạm chặn IP (403). Vui lòng thử lại sau.")
    if dest_status.get("active"):
        expires = dest_status.get("expires", "")
        days_left = _days_left(expires)
        can_overwrite = (plan == "1y" and days_left < YEAR_OVERWRITE_DAYS) or (days_left < EXPIRING_OVERWRITE_DAYS)
        if not can_overwrite:
            return _result(
                False,
                "already_gold",
                f"Tài khoản đã có Gold (còn {days_left} ngày, hạn {expires}).",
                uid=dest_uid,
                expires=expires,
                days_left=days_left,
            )

    source_username = None
    source_expires = ""
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        source = db.reserve_gold_source(plan=plan)
        if not source:
            break
        source_username = source["username"]

        await _notify(log, f"Đang dùng nguồn @{source_username} (lần {attempt}/{MAX_ATTEMPTS})...")
        source_uid = await locket.resolve_uid(source_username, proxy_url=proxy_url)
        if not source_uid or source_uid in ("IP_BLOCKED", "PROXY_ERROR"):
            db.release_gold_source(source["id"], success=False)
            source_username = None
            if source_uid == "IP_BLOCKED":
                return _result(False, "ip_blocked", "Nguồn bị chặn IP khi phân giải. Vui lòng thử lại sau.")
            continue

        source_status = await locket.check_status(source_uid, proxy_url=proxy_url)
        source_expires = (source_status or {}).get("expires", "") or ""

        ok, message = await locket.alias_subscriber(source_uid, dest_uid, proxy_url=proxy_url)
        if ok:
            released = db.release_gold_source(source["id"], success=True)
            used = (released or {}).get("count", 0)
            await _notify(log, "Đã chuyển Gold, đang xác minh...")
            final_status = await locket.check_status(dest_uid, proxy_url=proxy_url)
            valid_exp = final_status.get("expires") if final_status.get("active") else source_expires
            days_left = _days_left(valid_exp)
            return _result(
                True,
                "ok",
                "Kích hoạt thành công.",
                uid=dest_uid,
                username=dest_input,
                expires=valid_exp or "Unknown",
                days_left=days_left,
                source=source_username,
                source_used=used,
                source_slots_left=max(0, db.MAX_SOURCE_SPINS - int(used or 0)),
            )

        if message == "IP_BLOCKED":
            db.release_gold_source(source["id"], success=False)
            return _result(False, "ip_blocked", "Locket tạm chặn IP (403) khi gửi yêu cầu. Vui lòng thử lại sau.")
        if message == "NO_TOKEN":
            db.release_gold_source(source["id"], success=False)
            return _result(False, "no_token", "Thiếu cấu hình REVENUECAT_APP_KEY.")

        is_limit = "alias limit" in (message or "").lower()
        db.release_gold_source(source["id"], success=False, exhausted=is_limit)
        last_error = message or "unknown"
        source_username = None
        if not is_limit:
            break

    if not source_username:
        if last_error:
            return _result(False, "error", f"Lỗi từ hệ thống: {last_error}")
        return _result(False, "no_source", "Kho nguồn trống hoặc tất cả nguồn đã đạt giới hạn 5 lần.")
    return _result(False, "error", f"Lỗi từ hệ thống: {last_error or 'unknown'}")


async def check_source(source, probe=False, proxy_url=None):
    """Validate one source row.

    probe=False: only checks the source still has an active Gold entitlement.
    probe=True : additionally aliases to a throwaway UID to detect RevenueCat's
                 50-alias ceiling (this consumes one alias slot on the source).
    """
    username = source["username"]
    uid = await locket.resolve_uid(username, proxy_url=proxy_url)
    if uid == "IP_BLOCKED":
        return {"username": username, "status": "ip_blocked"}
    if not uid:
        return {"username": username, "status": "not_found"}
    status = await locket.check_status(uid, proxy_url=proxy_url)
    if status.get("error") == "IP_BLOCKED":
        return {"username": username, "status": "ip_blocked"}
    if not status.get("active"):
        return {"username": username, "status": "no_gold", "uid": uid}
    expires = status.get("expires", "")
    days_left = _days_left(expires)
    if days_left < 10:
        return {"username": username, "status": "expiring", "uid": uid, "days_left": days_left, "expires": expires}
    if probe:
        probe_target = f"probe_{int(time.time())}_{uid[:8]}"
        ok, message = await locket.alias_subscriber(uid, probe_target, proxy_url=proxy_url)
        if ok:
            return {"username": username, "status": "usable", "uid": uid, "days_left": days_left, "expires": expires}
        if "alias limit" in (message or "").lower():
            return {"username": username, "status": "alias_limit", "uid": uid, "days_left": days_left, "expires": expires}
        if message == "IP_BLOCKED":
            return {"username": username, "status": "ip_blocked"}
        return {"username": username, "status": "error", "uid": uid, "detail": message}
    return {"username": username, "status": "usable", "uid": uid, "days_left": days_left, "expires": expires}
