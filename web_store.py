#!/usr/bin/env python3
"""Locket Gold — Web Store + Admin Panel.

A self-contained aiohttp web app that reuses the bot's database and SePay
payment stack to sell Gold keys directly on the web:

  * Storefront (/): product page, plan picker, buy flow with VietQR, direct
    key redemption (paste key + Locket link) and key verification.
  * Order page (/order/<id>): VietQR, live payment polling, key delivery and
    a built-in redeem form.
  * Verify page (/verify): check whether a key is valid / used / unknown.
  * Admin panel (/admin): revenue, orders, key generation and the shared Gold
    source pool used by the alias activation engine.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time

from aiohttp import web

from app import database as db
from app.config import (
    ADMIN_ID,
    BANK_ACCOUNT,
    BANK_BIN,
    BANK_NAME,
    BANK_OWNER,
    CDK_SECRET,
    CDK_UNIT_PRICE,
    CDK_UNIT_PRICE_1Y,
    FREE_REACTIVATE_COOLDOWN_MINUTES,
    FREE_REACTIVATE_DAILY_MAX,
    CDK_ORDER_TIMEOUT_MINUTES,
    GOLD_MIN_SOURCE_DAYS,
    SEPAY_API_TOKEN,
    SEPAY_API_URL,
    SEPAY_POLL_INTERVAL_SECONDS,
    WEB_ADMIN_PASSWORD,
    WEB_ADMIN_PASSWORD_HASH,
    WEB_ADMIN_USER,
    WEB_HOST,
    WEB_PORT,
    WEB_SESSION_SECRET,
    payment_config_errors,
    plan_label,
)
from app.services import activation
from app.services import locket
from app.services import sepay

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot_data.db")
MAX_QUANTITY = 5
SESSION_COOKIE = "locket_store_admin"
CSRF_COOKIE = "locket_store_csrf"
VISITOR_COOKIE = "locket_store_vid"
SESSION_TTL_SECONDS = 8 * 3600
LOGIN_MAX_ATTEMPTS = 6
LOGIN_WINDOW_SECONDS = 900

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "web_store.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("web_store")

_payment_checks = {}
_login_attempts = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_vnd(amount):
    return f"{int(amount):,}".replace(",", ".") + "đ"


def _now_ts():
    return int(time.time())


def _fmt_dt(ts):
    if not ts:
        return "—"
    try:
        return time.strftime("%d/%m/%Y %H:%M", time.localtime(int(ts)))
    except (ValueError, OSError, TypeError):
        return str(ts)


def _gen_payment_content():
    return "LK" + secrets.token_hex(8).upper()


def _visitor_id(request):
    cookie = request.cookies.get(VISITOR_COOKIE, "")
    try:
        value = int(cookie)
        if -10**12 < value < 0:
            return value
    except (TypeError, ValueError):
        pass
    value = -secrets.randbelow(10**10) - 1
    request["visitor_id"] = value
    return value


def _qr_url(order):
    return sepay.build_vietqr_url(
        BANK_BIN,
        BANK_ACCOUNT,
        order["total_price"],
        order["payment_content"],
        account_name=BANK_OWNER,
    )


def _build_qr(order):
    try:
        return {"ok": True, "url": _qr_url(order)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


def _price_for_plan(plan):
    """Plan price from this module's globals so deploys/tests can override."""
    return CDK_UNIT_PRICE_1Y if (plan or "").lower() == "1y" else CDK_UNIT_PRICE


# ---------------------------------------------------------------------------
# SePay payment completion (web orders only)
# ---------------------------------------------------------------------------

async def _complete_web_order(order_id):
    """One-shot: find a matching SePay transaction and complete the order."""
    if order_id in _payment_checks:
        return None
    _payment_checks[order_id] = _now_ts()
    try:
        order = db.get_cdk_order(id=order_id)
        if not order or order["status"] != "pending":
            return None
        if order["expires_at"] and order["expires_at"] <= _now_ts():
            return None
        async with sepay.SePayClient(SEPAY_API_TOKEN, base_url=SEPAY_API_URL) as client:
            transaction = await client.find_matching_transaction(
                order["payment_content"],
                order["total_price"],
                account_number=BANK_ACCOUNT,
            )
        if not transaction:
            return None
        return db.complete_cdk_order(
            order_id=order_id,
            transaction_id=transaction["id"],
            matched_amount=transaction["amount_in"],
            secret=CDK_SECRET,
        )
    except sepay.SePayError as exc:
        logger.warning("SePay check failed for order %s: %s", order_id, exc)
        return None
    except Exception as exc:
        logger.error("Complete web order %s failed: %s", order_id, exc)
        return None
    finally:
        _payment_checks.pop(order_id, None)


async def web_payment_poller():
    logger.info("Web payment poller started (interval=%ss)", SEPAY_POLL_INTERVAL_SECONDS)
    while True:
        try:
            db.expire_cdk_orders()
            for order in db.get_pending_cdk_orders():
                if order.get("chat_id") is not None:
                    continue  # Telegram orders are handled by the bot.
                codes = await _complete_web_order(order["id"])
                if codes:
                    logger.info("Web order #%s completed (%s keys)", order["id"], len(codes))
        except Exception as exc:
            logger.error("Web payment poller error: %s", exc)
        await asyncio.sleep(SEPAY_POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Admin session auth
# ---------------------------------------------------------------------------

def _sign(data: bytes) -> bytes:
    return hmac.new(WEB_SESSION_SECRET.encode("utf-8"), data, hashlib.sha256).digest()


def _make_session_token(username: str) -> str:
    payload = base64.urlsafe_b64encode(
        f"{username}:{_now_ts() + SESSION_TTL_SECONDS}".encode("utf-8")
    ).rstrip(b"=")
    sig = base64.urlsafe_b64encode(_sign(payload)).rstrip(b"=")
    return f"{payload.decode('ascii')}.{sig.decode('ascii')}"


def _read_session(request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token or "." not in token:
        return None
    payload_b64, sig_b64 = token.split(".", 1)
    try:
        payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(sig, _sign(payload_b64.encode("ascii"))):
        return None
    try:
        username, expires = payload.decode("utf-8").rsplit(":", 1)
        if int(expires) < _now_ts():
            return None
    except (ValueError, UnicodeDecodeError):
        return None
    return username


def _is_admin(request):
    return _read_session(request) == WEB_ADMIN_USER


def _login_allowed(ip):
    now = _now_ts()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    _login_attempts[ip] = attempts
    return len(attempts) < LOGIN_MAX_ATTEMPTS


def _record_login_fail(ip):
    _login_attempts.setdefault(ip, []).append(_now_ts())


def _client_ip(request):
    """Real client IP — works behind a reverse proxy that sets X-Forwarded-For."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.remote or "?"


def _verify_admin_password(password):
    """Verify against PBKDF2-SHA256 hash (preferred) or legacy plaintext compare."""
    if WEB_ADMIN_PASSWORD_HASH:
        try:
            scheme, iterations, salt_b64, hash_b64 = WEB_ADMIN_PASSWORD_HASH.split("$", 3)
            if scheme != "pbkdf2":
                return False
            iterations = int(iterations)
            salt = base64.urlsafe_b64decode(salt_b64 + "=" * (-len(salt_b64) % 4))
            expected = base64.urlsafe_b64decode(hash_b64 + "=" * (-len(hash_b64) % 4))
            derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
            return hmac.compare_digest(derived, expected)
        except (ValueError, TypeError, base64.binascii.Error):
            return False
    return bool(WEB_ADMIN_PASSWORD) and hmac.compare_digest(password, WEB_ADMIN_PASSWORD)


def _csrf_token(request):
    """Per-session CSRF token bound to the session secret (double-submit cookie)."""
    nonce = request.cookies.get(CSRF_COOKIE, "")
    if not nonce or len(nonce) != 32:
        return None
    return hmac.new(WEB_SESSION_SECRET.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _check_csrf(request, posted_token):
    expected = _csrf_token(request)
    return bool(expected) and bool(posted_token) and hmac.compare_digest(expected, posted_token)


def _secure_cookie(request):
    """Mark cookies Secure when the request arrives over HTTPS (incl. behind proxy)."""
    if request.headers.get("X-Forwarded-Proto", "").lower() == "https":
        return True
    return request.scheme == "https"


def _require_admin(handler):
    async def wrapper(request):
        if not _is_admin(request):
            raise web.HTTPFound("/admin/login")
        return await handler(request)
    return wrapper


# ---------------------------------------------------------------------------
# Templates / assets
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#08090e;--panel:#11141d;--panel2:#161a26;--line:rgba(255,255,255,.08);
--gold1:#f6d98a;--gold2:#e6b94f;--gold3:#b8860b;--text:#eef0f6;--muted:#98a0b3;
--green:#3ddc97;--red:#ff6b6b;--blue:#6ea8fe;--radius:18px;--shadow:0 20px 60px rgba(0,0,0,.45)}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:'Be Vietnam Pro',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;line-height:1.6;overflow-x:hidden}
a{color:inherit;text-decoration:none}
.container{max-width:1120px;margin:0 auto;padding:0 22px}
.gold-text{background:linear-gradient(120deg,var(--gold1),var(--gold2) 45%,var(--gold3));-webkit-background-clip:text;background-clip:text;color:transparent}
.btn{display:inline-flex;align-items:center;gap:10px;border:none;cursor:pointer;font-weight:700;border-radius:14px;padding:15px 30px;font-size:16px;transition:.25s;font-family:inherit}
.btn-gold{background:linear-gradient(135deg,var(--gold1),var(--gold2));color:#201503;box-shadow:0 8px 30px rgba(230,185,79,.35)}
.btn-gold:hover{transform:translateY(-2px);box-shadow:0 12px 40px rgba(230,185,79,.5)}
.btn-ghost{background:rgba(255,255,255,.06);color:var(--text);border:1px solid var(--line)}
.btn-ghost:hover{background:rgba(255,255,255,.1)}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none!important}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
.pill{display:inline-block;padding:4px 14px;border-radius:99px;font-size:13px;font-weight:600;background:rgba(230,185,79,.12);color:var(--gold1);border:1px solid rgba(230,185,79,.3)}
.hero{position:relative;padding:110px 0 80px;text-align:center;overflow:hidden}
.hero::before{content:'';position:absolute;top:-260px;left:50%;transform:translateX(-50%);width:900px;height:900px;border-radius:50%;background:radial-gradient(circle,rgba(230,185,79,.16),transparent 60%);pointer-events:none}
.hero::after{content:'';position:absolute;inset:0;background:
radial-gradient(600px circle at 20% 10%,rgba(230,185,79,.05),transparent 50%),
radial-gradient(700px circle at 85% 30%,rgba(110,168,254,.06),transparent 50%);pointer-events:none}
.hero h1{font-size:clamp(34px,6vw,64px);font-weight:800;letter-spacing:-1px;line-height:1.15;position:relative;z-index:1}
.hero .sub{max-width:640px;margin:22px auto 34px;color:var(--muted);font-size:clamp(15px,2vw,18px);position:relative;z-index:1}
.hero-badges{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-top:34px;position:relative;z-index:1}
.badge{display:flex;align-items:center;gap:8px;padding:9px 18px;border-radius:99px;background:var(--panel);border:1px solid var(--line);font-size:13.5px;color:var(--text)}
.badge .dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green)}
.nav{position:sticky;top:0;z-index:50;backdrop-filter:blur(14px);background:rgba(8,9,14,.82);border-bottom:1px solid var(--line)}
.nav-inner{display:flex;align-items:center;justify-content:space-between;height:64px}
.logo{display:flex;align-items:center;gap:10px;font-weight:800;font-size:18px;letter-spacing:.3px}
.logo .mark{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,var(--gold1),var(--gold3));display:flex;align-items:center;justify-content:center;font-size:18px;color:#201503;box-shadow:0 4px 14px rgba(230,185,79,.4)}
.nav-links{display:flex;align-items:center;gap:26px;font-size:14.5px;color:var(--muted)}
.nav-links a:hover{color:var(--gold1)}
.section{padding:72px 0}
.section-tag{color:var(--gold2);font-weight:700;letter-spacing:2px;text-transform:uppercase;font-size:12.5px}
.section h2{font-size:clamp(26px,4vw,38px);font-weight:800;margin:10px 0 14px;letter-spacing:-.5px}
.section .lead{color:var(--muted);max-width:600px;margin-bottom:40px}
.grid{display:grid;gap:18px}
.features{grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
.feature{padding:26px;transition:.25s}
.feature:hover{transform:translateY(-4px);border-color:rgba(230,185,79,.35)}
.feature .icon{width:46px;height:46px;border-radius:12px;background:rgba(230,185,79,.12);border:1px solid rgba(230,185,79,.25);display:flex;align-items:center;justify-content:center;font-size:22px;margin-bottom:16px}
.feature h3{font-size:16.5px;margin-bottom:8px}
.feature p{font-size:14px;color:var(--muted)}
.plan-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px}
.plan-card{padding:30px;text-align:center;transition:.25s}
.plan-card:hover{transform:translateY(-4px);border-color:rgba(230,185,79,.45)}
.plan-card .p-name{font-size:17px;font-weight:800}
.plan-card .p-price{font-size:36px;font-weight:800;margin:12px 0 2px}
.plan-card .p-price small{font-size:14px;color:var(--muted);font-weight:600}
.plan-card .p-desc{color:var(--muted);font-size:13.5px;margin:12px 0 20px;min-height:42px}
.qty-row{display:flex;align-items:center;justify-content:center;gap:14px;margin-bottom:20px}
.qty-btn{width:46px;height:46px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);color:var(--text);font-size:22px;font-weight:700;cursor:pointer;transition:.2s;font-family:inherit}
.qty-btn:hover{border-color:var(--gold2);color:var(--gold1)}
.qty-val{font-size:24px;font-weight:800;min-width:44px;text-align:center}
.total-line{display:flex;justify-content:space-between;align-items:baseline;padding:16px 0;border-top:1px dashed var(--line);border-bottom:1px dashed var(--line);margin-bottom:22px}
.total-line .lbl{color:var(--muted);font-size:14px}
.total-line .amt{font-size:28px;font-weight:800}
.bank-note{font-size:12.5px;color:var(--muted);margin-top:16px;display:flex;gap:8px;align-items:flex-start}
.steps{grid-template-columns:repeat(auto-fit,minmax(240px,1fr));counter-reset:step}
.step{padding:28px;position:relative}
.step .num{font-size:38px;font-weight:800;color:rgba(230,185,79,.35);line-height:1}
.step h3{margin:12px 0 8px;font-size:16px}
.step p{font-size:14px;color:var(--muted)}
.verify-box{max-width:640px;margin:0 auto;text-align:center;padding:40px}
.verify-input{width:100%;max-width:440px;padding:16px 20px;border-radius:14px;border:1px solid var(--line);background:var(--panel2);color:var(--text);font-size:16px;letter-spacing:1.5px;text-transform:uppercase;font-family:inherit;text-align:center;margin:22px 0 14px}
.verify-input:focus{outline:none;border-color:var(--gold2)}
#verify-result{margin-top:20px;display:none}
.result-card{padding:22px;border-radius:14px;font-size:15px}
.result-card.ok{background:rgba(61,220,151,.08);border:1px solid rgba(61,220,151,.35)}
.result-card.bad{background:rgba(255,107,107,.08);border:1px solid rgba(255,107,107,.35)}
.result-card.info{background:rgba(110,168,254,.08);border:1px solid rgba(110,168,254,.35)}
.result-card .big{font-size:18px;font-weight:800;margin-bottom:6px}
.result-card .meta{font-size:13.5px;color:var(--muted)}
.order-wrap{max-width:760px;margin:0 auto}
.order-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap;gap:10px}
.qr-card{display:flex;flex-direction:column;align-items:center;text-align:center;padding:34px;margin-bottom:20px}
.qr-card img{width:280px;height:280px;border-radius:16px;background:#fff;padding:12px;margin-bottom:18px}
.qr-amount{font-size:34px;font-weight:800;margin-bottom:4px}
.qr-content{font-family:ui-monospace,Menlo,Consolas,monospace;background:var(--panel2);border:1px solid var(--line);padding:8px 16px;border-radius:10px;font-size:15px;letter-spacing:1px;margin-top:10px;user-select:all}
.bank-card{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px}
@media(max-width:560px){.bank-card{grid-template-columns:1fr}}
.bank-item{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.bank-item .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}
.bank-item .v{font-size:16px;font-weight:700}
.countdown{display:flex;align-items:center;gap:10px;background:rgba(255,107,107,.07);border:1px solid rgba(255,107,107,.3);border-radius:14px;padding:14px 18px;color:var(--red);font-weight:700;font-size:15px;margin-bottom:20px;justify-content:center}
.countdown.ok{background:rgba(61,220,151,.08);border-color:rgba(61,220,151,.35);color:var(--green)}
.status-tip{text-align:center;color:var(--muted);font-size:13.5px;margin-top:14px}
.codes-box{display:none;padding:34px;margin-top:20px;border-color:rgba(61,220,151,.35)}
.codes-box h3{color:var(--green);margin-bottom:14px;font-size:19px}
.code-line{display:flex;justify-content:space-between;align-items:center;gap:12px;background:var(--panel2);border:1px solid var(--line);padding:13px 16px;border-radius:12px;margin-bottom:10px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:14.5px;letter-spacing:.5px}
.code-line button{background:rgba(230,185,79,.14);border:1px solid rgba(230,185,79,.35);color:var(--gold1);border-radius:9px;padding:7px 14px;font-size:12.5px;cursor:pointer;font-family:inherit;font-weight:600}
.code-line button:hover{background:rgba(230,185,79,.25)}
.auth-card{max-width:420px;margin:90px auto;padding:40px;text-align:center}
.auth-card .logo{margin:0 auto 22px;justify-content:center}
.auth-card form{display:flex;flex-direction:column;gap:14px;margin-top:22px}
.field{position:relative;text-align:left}
.field label{display:block;font-size:13px;color:var(--muted);margin-bottom:7px}
.field input,.field select{width:100%;padding:14px 16px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);color:var(--text);font-size:15px;font-family:inherit}
.field input:focus,.field select:focus{outline:none;border-color:var(--gold2)}
.auth-error{background:rgba(255,107,107,.1);border:1px solid rgba(255,107,107,.4);color:var(--red);border-radius:12px;padding:12px 16px;font-size:14px}
.admin-layout{display:grid;grid-template-columns:230px 1fr;min-height:calc(100vh - 64px);gap:0}
@media(max-width:860px){.admin-layout{grid-template-columns:1fr}}
.admin-side{border-right:1px solid var(--line);padding:26px 18px;display:flex;flex-direction:column;gap:6px}
.admin-side a{padding:12px 16px;border-radius:12px;color:var(--muted);font-size:14.5px;transition:.2s}
.admin-side a:hover,.admin-side a.active{background:rgba(230,185,79,.1);color:var(--gold1)}
.admin-main{padding:30px}
.admin-main h1{font-size:24px;margin-bottom:6px}
.admin-main .sub{color:var(--muted);font-size:13.5px;margin-bottom:26px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:30px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px 20px}
.stat .v{font-size:26px;font-weight:800}
.stat .k{font-size:12.5px;color:var(--muted);margin-top:2px}
.table{width:100%;border-collapse:collapse;font-size:13.5px}
.table th{color:var(--muted);text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);font-weight:600;white-space:nowrap}
.table td{padding:10px 12px;border-bottom:1px solid rgba(255,255,255,.04)}
.table tr:hover td{background:rgba(255,255,255,.02)}
.tag{display:inline-block;padding:3px 11px;border-radius:99px;font-size:11.5px;font-weight:700}
.tag.pending{background:rgba(246,217,138,.12);color:var(--gold1)}
.tag.completed{background:rgba(61,220,151,.12);color:var(--green)}
.tag.expired,.tag.canceled{background:rgba(255,107,107,.12);color:var(--red)}
.tag.valid{background:rgba(61,220,151,.12);color:var(--green)}
.tag.used{background:rgba(255,107,107,.12);color:var(--red)}
.tag.reserved{background:rgba(110,168,254,.12);color:var(--blue)}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
.admin-form{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:22px;margin-bottom:24px}
.admin-form .field{flex:1;min-width:180px}
.gen-result{background:var(--panel2);border:1px solid rgba(61,220,151,.3);border-radius:12px;padding:16px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;white-space:pre-wrap;margin-bottom:24px;display:none}
.alert{background:rgba(255,107,107,.08);border:1px solid rgba(255,107,107,.35);color:var(--red);border-radius:12px;padding:12px 16px;font-size:14px;margin-bottom:20px}
.btn-sm{padding:7px 13px;border-radius:10px;font-size:12.5px;font-weight:700;border:1px solid var(--line);background:var(--panel2);color:var(--text);cursor:pointer;font-family:inherit}
.btn-sm.danger{color:var(--red);border-color:rgba(255,107,107,.4)}
.btn-sm:hover{border-color:var(--gold2);color:var(--gold1)}
footer{border-top:1px solid var(--line);padding:36px 0;text-align:center;color:var(--muted);font-size:13.5px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
.pulse{animation:pulse 1.6s infinite}
.check-card{max-width:560px;margin:26px auto 0;background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:10px;display:flex;gap:10px;position:relative;z-index:1}
.check-card input{flex:1;min-width:0;background:transparent;border:none;outline:none;color:var(--text);font-size:15.5px;padding:8px 14px;font-family:inherit}
.check-card input::placeholder{color:var(--muted)}
.check-card .btn{padding:12px 22px}
#check-result{max-width:640px;margin:16px auto 0;text-align:left;position:relative;z-index:1}
.check-user{display:flex;align-items:center;gap:16px;padding:20px 22px;border-radius:16px;background:var(--panel);border:1px solid var(--line);margin-bottom:14px}
.check-user img{width:64px;height:64px;border-radius:50%;object-fit:cover;border:2px solid var(--gold2);flex-shrink:0;background:var(--panel2)}
.check-user .nm{font-weight:800;font-size:17px}
.check-user .uid{font-size:12.5px;color:var(--muted);font-family:ui-monospace,Menlo,Consolas,monospace}
.check-status{display:inline-block;padding:4px 14px;border-radius:99px;font-size:13px;font-weight:700;margin-top:8px}
.check-status.gold{background:rgba(61,220,151,.12);color:var(--green)}
.check-status.free{background:rgba(255,107,107,.12);color:var(--red)}
.redeem-box{max-width:680px;margin:0 auto;padding:36px}
.redeem-row{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
.redeem-row input{flex:1;min-width:200px;padding:14px 16px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);color:var(--text);font-size:15px;font-family:inherit}
.redeem-row input:focus{outline:none;border-color:var(--gold2)}
"""


def page(title, body, *, css=CSS, extra_head=""):
    return f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>{css}</style>{extra_head}</head><body>
{body}
<script>
document.querySelectorAll('[data-copy]').forEach(function(btn){{
  btn.addEventListener('click',function(){{
    var t=btn.getAttribute('data-copy');
    if(navigator.clipboard){{navigator.clipboard.writeText(t).then(function(){{btn.textContent='✓ Copied';setTimeout(function(){{btn.textContent='Copy'}},1500);}});}}
  }});
}});
</script>
</body></html>"""


def nav_bar(active=""):
    return f"""<nav class="nav"><div class="container nav-inner">
<a class="logo" href="/"><span class="mark">👑</span>Locket <span class="gold-text">Gold</span></a>
<div class="nav-links">
<a href="/#products" {'class="active"' if active=="shop" else ""}>Sản phẩm</a>
<a href="/#redeem">Kích hoạt Key</a>
<a href="/verify">Kiểm tra Key</a>
</div></div></nav>"""


def _product_card(disabled=False):
    price = _price_for_plan("1m")
    button = (
        '<button class="btn btn-gold" style="width:100%;justify-content:center" disabled>Cửa hàng tạm đóng</button>'
        if disabled
        else '<button class="btn btn-gold" style="width:100%;justify-content:center" onclick="buyPlan(this)">💳 Mua Gói Vĩnh Viễn</button>'
    )
    return f"""<div class="card plan-card">
<div class="p-name">👑 Gói Vĩnh Viễn</div>
<div class="p-price">{format_vnd(price)} <small>/ key</small></div>
<div class="p-desc">1 key kích hoạt Gold cho 1 tài khoản Locket.<br>
💡 Nếu Gold rớt sau một thời gian dài sử dụng, vui lòng vào web bấm <b>Kích hoạt lại miễn phí</b>.</div>
{button}
<p class="bank-note" style="justify-content:center">Mua 1 lần — kích hoạt lại miễn phí trên web khi Gold rớt.</p>
</div>"""


def landing_page():
    errors = payment_config_errors()
    shop_disabled = bool(errors) or _price_for_plan("1m") <= 0
    body = f"""{nav_bar('shop')}
<section class="hero"><div class="container">
<span class="pill">✨ Mua key &amp; kích hoạt tự động</span>
<h1 style="margin-top:20px">Bật <span class="gold-text">Locket Gold</span><br>chỉ trong vài phút</h1>
<p class="sub">Mua key bằng VietQR, nhận key tự động, rồi kích hoạt Gold cho tài khoản Locket của bạn — ngay trên web hoặc qua bot Telegram với <b>/redeem</b>.</p>

<div class="check-card">
  <input id="check-username" placeholder="Username Locket hoặc link locket.cam/..." autocomplete="off" spellcheck="false">
  <button class="btn btn-gold" id="check-btn" onclick="checkGold()">🔍 Kiểm tra</button>
</div>
<div id="check-result"></div>

<div class="hero-badges">
<span class="badge"><span class="dot"></span>Nhận key tự động 24/7</span>
<span class="badge"><span class="dot"></span>Thanh toán VietQR</span>
<span class="badge"><span class="dot"></span>Kích hoạt tức thì</span>
</div></div></section>

<section class="section" id="products"><div class="container">
<div class="section-tag">Sản phẩm</div>
<h2>Gói <span class="gold-text">Vĩnh Viễn</span></h2>
<p class="lead">Mỗi key kích hoạt Gold cho 1 tài khoản Locket. Key hiển thị ngay sau khi ngân hàng xác nhận chuyển khoản. Nếu Gold rớt sau thời gian dài sử dụng, vào web kích hoạt lại — <b>miễn phí</b> cho tài khoản đã từng kích hoạt.</p>
<div class="plan-grid" id="plan-cards">
{_product_card(shop_disabled)}
</div>
<div class="card redeem-box" id="redeem" style="margin-top:26px">
  <div class="section-tag">Đã có key?</div>
  <h2 style="font-size:26px;margin-top:8px">Kích hoạt Gold ngay</h2>
  <p class="lead" style="margin:8px 0 0">Dán key và username Locket của bạn. Hệ thống kích hoạt trực tiếp, kết quả hiển thị ngay bên dưới.</p>
  <div class="redeem-row">
    <input id="redeem-code" placeholder="Key: LK-GOLD-XXXXXX hoặc LOCK-..." autocomplete="off" spellcheck="false">
    <input id="redeem-username" placeholder="Username hoặc link locket.cam/..." autocomplete="off" spellcheck="false">
    <button class="btn btn-gold" id="redeem-btn" onclick="redeemKey()">🚀 Kích hoạt</button>
  </div>
  <div id="redeem-result" style="margin-top:16px"></div>
</div>
</div></section>

<section class="section" style="padding-top:0"><div class="container">
<div class="section-tag">Vì sao chọn chúng tôi</div>
<h2>Tại sao <span class="gold-text">Locket Gold</span>?</h2>
<div class="grid features">
  <div class="card feature"><div class="icon">⚡</div><h3>Kích hoạt tức thì</h3><p>Kích hoạt ngay sau khi thanh toán hoặc bằng key có sẵn — không cần chờ đợi.</p></div>
  <div class="card feature"><div class="icon">🏦</div><h3>VietQR chuẩn</h3><p>Chuyển khoản qua mã QR ngân hàng, hệ thống đối soát tự động theo nội dung và số tiền.</p></div>
  <div class="card feature"><div class="icon">🎟️</div><h3>Key đa nền tảng</h3><p>Dùng key trên web hoặc gửi cho khách/bạn bè kích hoạt qua bot Telegram bằng /redeem.</p></div>
  <div class="card feature"><div class="icon">🛡️</div><h3>Nguồn chăm sóc tự động</h3><p>Kho nguồn được kiểm tra định kỳ, tự loại nguồn hết hạn hoặc chạm giới hạn.</p></div>
  <div class="card feature"><div class="icon">💬</div><h3>Hỗ trợ 24/7</h3><p>Kênh Telegram luôn sẵn sàng giải đáp mọi thắc mắc sau khi mua hàng.</p></div>
  <div class="card feature"><div class="icon">🤝</div><h3>Hoàn key khi lỗi</h3><p>Nếu kích hoạt thất bại, lượt key được hoàn lại tự động — bạn không mất gì.</p></div>
</div></div></section>

<section class="section" style="padding-top:0"><div class="container">
<div class="section-tag">Hướng dẫn</div>
<h2>Chỉ 3 bước <span class="gold-text">đơn giản</span></h2>
<div class="grid steps">
  <div class="card step"><div class="num">01</div><h3>Mua key</h3><p>Chọn gói 1 tháng hoặc 1 năm và chuyển khoản theo mã VietQR.</p></div>
  <div class="card step"><div class="num">02</div><h3>Nhận key</h3><p>Key hiện ngay trên trang đơn hàng sau khi ngân hàng xác nhận (3-10 giây).</p></div>
  <div class="card step"><div class="num">03</div><h3>Kích hoạt</h3><p>Dán key + username Locket và bấm kích hoạt — hoặc dùng <b>/redeem key link</b> trên bot.</p></div>
</div></div></section>

<section class="section" style="padding-top:0"><div class="container">
<div class="card verify-box">
<div class="section-tag">Kiểm tra mã</div>
<h2 style="font-size:26px">Xác minh key đã mua</h2>
<p class="lead" style="margin:8px auto 0">Dán key vào đây để kiểm tra trạng thái và số lượt còn lại.</p>
<input class="verify-input" id="verify-code" placeholder="LK-GOLD-XXXXXX" autocomplete="off" spellcheck="false">
<button class="btn btn-gold" onclick="verify()" style="justify-content:center">🔍 Kiểm tra</button>
<div id="verify-result"></div>
</div></div></section>

<footer><div class="container">© 2026 Locket Gold — Kích hoạt Gold Locket tự động. Mọi thắc mắc liên hệ kênh Telegram chính thức.</div></footer>

<script>
const PRICES={{'1m':{_price_for_plan('1m')},'1y':{_price_for_plan('1y')}}};
function esc(s){{return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}
function vnd(n){{return Number(n).toLocaleString('vi-VN').replace(/,/g,'.')+'đ';}}
let currentUser=null;

async function checkGold(){{
  const input=document.getElementById('check-username');
  const btn=document.getElementById('check-btn');
  const box=document.getElementById('check-result');
  const username=input.value.trim();
  if(!username){{alert('Nhập username Locket của bạn');return;}}
  btn.disabled=true;btn.textContent='⏳ Đang kiểm tra...';
  box.style.display='block';
  box.innerHTML='<div class="result-card info"><div class="big">⏳ Đang kiểm tra tình trạng Gold...</div></div>';
  try{{
    const r=await fetch('/api/check',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{username:username}})}});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Lỗi hệ thống');
    currentUser=d;
    const avatar=d.avatar?'<img src="'+esc(d.avatar)+'" alt="avatar" onerror="this.style.display=\\'none\\'">':'<div style="width:64px;height:64px;border-radius:50%;background:var(--panel2);display:flex;align-items:center;justify-content:center;font-size:26px">👤</div>';
    const status=d.gold_active
      ? '<div class="check-status gold">✅ Đã có Gold'+(d.expires?' — hết hạn: '+esc(d.expires):'')+'</div>'
      : '<div class="check-status free">⏳ Chưa có Gold</div>';
    let action='';
    const reactivateBtn=d.can_reactivate
      ? '<button class="btn btn-gold" style="width:100%;justify-content:center;margin-top:14px" data-name="'+esc(d.username)+'" onclick="reactivate(this)">⚡ Kích hoạt lại miễn phí</button>'
      : '';
    if(d.gold_active){{
      action='<div class="result-card ok"><div class="big">🎉 Tài khoản này đã có Gold!</div><div class="meta">Bạn chưa cần mua key. Nếu Gold rớt sau thời gian dài sử dụng, quay lại đây bấm "Kích hoạt lại miễn phí".</div></div>'+reactivateBtn;
    }}else if(d.can_reactivate){{
      action='<div class="result-card info"><div class="big">🔄 Tài khoản đã từng kích hoạt</div><div class="meta">Bạn được kích hoạt lại <b>miễn phí</b> (không cần mua key mới).</div></div>'+reactivateBtn;
    }}else{{
      action='<div class="result-card info"><div class="meta">Tài khoản chưa có Gold. Chọn gói bên dưới hoặc dán key có sẵn vào ô "Đã có key?".</div></div>';
    }}
    box.innerHTML='<div class="check-user">'+avatar+'<div><div class="nm">'+esc(d.username)+'</div><div class="uid">'+esc(d.uid)+'</div>'+status+'</div></div>'+action;
    const ru=document.getElementById('redeem-username');
    if(ru&&!ru.value)ru.value=username;
  }}catch(e){{
    box.innerHTML='<div class="result-card bad"><div class="big">⚠️ Không tìm thấy tài khoản</div><div class="meta">'+esc(e.message)+' — Kiểm tra lại username hoặc link hồ sơ Locket.</div></div>';
  }}
  btn.disabled=false;btn.textContent='🔍 Kiểm tra';
}}

async function buyPlan(btn){{
  const qty=parseInt(prompt('Số lượng key (1-5):','1')||'0',10);
  if(!qty||qty<1||qty>5){{if(qty!==0)alert('Số lượng 1-5');return;}}
  btn.disabled=true;const old=btn.textContent;btn.textContent='⏳ Đang tạo đơn...';
  try{{
    const r=await fetch('/api/order',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{plan:'1m',quantity:qty}})}});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Lỗi hệ thống');
    location.href='/order/'+d.order_id;
  }}catch(e){{alert(e.message);btn.disabled=false;btn.textContent=old;}}
}}

async function reactivate(btn){{
  const username=btn.getAttribute('data-name');
  const box=document.getElementById('check-result');
  if(!username)return;
  btn.disabled=true;const old=btn.textContent;btn.textContent='⏳ Đang kích hoạt lại...';
  box.innerHTML+='<div class="result-card info" id="reactivate-result"><div class="big">⏳ Đang kích hoạt lại Gold...</div><div class="meta">Quá trình có thể mất 10-40 giây.</div></div>';
  try{{
    const r=await fetch('/api/reactivate',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{username:username}})}});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Lỗi hệ thống');
    document.getElementById('reactivate-result').className='result-card ok';
    document.getElementById('reactivate-result').innerHTML='<div class="big">🎉 KÍCH HOẠT LẠI THÀNH CÔNG</div>'
      +'<div>User: <b>'+esc(d.username)+'</b><br>Hạn Gold: <b>'+esc(d.expires||'—')+'</b>'+(d.days_left?' (còn '+d.days_left+' ngày)':'')+'</div>'
      +'<div class="meta" style="margin-top:8px">Kích hoạt lại miễn phí cho tài khoản đã từng mua. Nếu Gold rớt tiếp, quay lại đây bấm lại.</div>';
  }}catch(e){{
    document.getElementById('reactivate-result').className='result-card bad';
    document.getElementById('reactivate-result').innerHTML='<div class="big">❌ Kích hoạt lại thất bại</div><div class="meta">'+esc(e.message)+'</div>';
  }}
  btn.disabled=false;btn.textContent=old;
}}

async function redeemKey(){{
  const code=document.getElementById('redeem-code').value.trim();
  const username=document.getElementById('redeem-username').value.trim();
  const btn=document.getElementById('redeem-btn');
  const box=document.getElementById('redeem-result');
  if(!code||!username){{alert('Nhập key và username Locket');return;}}
  btn.disabled=true;btn.textContent='⏳ Đang kích hoạt...';
  box.innerHTML='<div class="result-card info"><div class="big">⏳ Đang kích hoạt Gold, vui lòng chờ...</div><div class="meta">Quá trình có thể mất 10-40 giây.</div></div>';
  try{{
    const r=await fetch('/api/redeem',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{code:code,username:username}})}});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Lỗi hệ thống');
    box.innerHTML='<div class="result-card ok"><div class="big">🎉 KÍCH HOẠT THÀNH CÔNG</div>'
      +'<div>User: <b>'+esc(d.username)+'</b><br>UID: <span class="mono">'+esc(d.uid)+'</span><br>'
      +'Hạn Gold: <b>'+esc(d.expires||'—')+'</b>'+(d.days_left?' (còn '+d.days_left+' ngày)':'')+'<br>'
      +'Key còn <b>'+d.spins_left+'</b> lượt.</div></div>';
  }}catch(e){{
    box.innerHTML='<div class="result-card bad"><div class="big">❌ Kích hoạt thất bại</div><div class="meta">'+esc(e.message)+'</div></div>';
  }}
  btn.disabled=false;btn.textContent='🚀 Kích hoạt';
}}

async function verify(){{
  const code=document.getElementById('verify-code').value.trim();
  const box=document.getElementById('verify-result');
  if(!code)return;
  box.style.display='block';box.innerHTML='<div class="result-card info"><div class="big">⏳ Đang kiểm tra...</div></div>';
  try{{
    const r=await fetch('/api/verify',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{code:code}})}});
    const d=await r.json();
    const planName=d.plan==='1y'?'1 Năm':'1 Tháng';
    if(d.status==='valid')box.innerHTML='<div class="result-card ok"><div class="big">✅ Key hợp lệ</div><div>Mã <b>'+esc(d.code)+'</b> — gói <b>'+planName+'</b>, còn <b>'+d.spins_left+'/'+d.spins+'</b> lượt.<br>Dán key vào ô "Đã có key?" để kích hoạt.</div></div>';
    else if(d.status==='used')box.innerHTML='<div class="result-card bad"><div class="big">❌ Key đã dùng hết lượt</div><div class="meta">Dùng lần cuối: '+esc(d.used_at||'—')+'</div></div>';
    else if(d.status==='reserved')box.innerHTML='<div class="result-card info"><div class="big">⏳ Key đang được giữ</div><div class="meta">Đơn hàng đang xử lý. Thử lại sau ít phút.</div></div>';
    else box.innerHTML='<div class="result-card bad"><div class="big">⚠️ Không tìm thấy key</div><div class="meta">Mã không tồn tại hoặc sai định dạng.</div></div>';
  }}catch(e){{box.innerHTML='<div class="result-card bad"><div class="big">⚠️ Lỗi kiểm tra</div><div class="meta">'+esc(e.message)+'</div></div>';}}
}}
</script>"""
    return page("Locket Gold — Mua key & kích hoạt Gold tự động", body)


def order_page(order):
    qr = _build_qr(order)
    expires_ms = (order["expires_at"] or 0) * 1000
    plan = order.get("plan") or "1m"
    bank = [
        ("Ngân hàng", BANK_NAME),
        ("Chủ tài khoản", BANK_OWNER),
        ("Số tài khoản", BANK_ACCOUNT),
        ("Nội dung", order["payment_content"]),
    ]
    bank_html = "".join(
        f'<div class="bank-item"><div class="k">{html.escape(k)}</div>'
        f'<div class="v mono" style="user-select:all">{html.escape(str(v))}</div></div>'
        for k, v in bank
    )
    qr_html = (
        f'<img src="{html.escape(qr["url"])}" alt="VietQR" loading="lazy">'
        if qr["ok"]
        else f'<div class="result-card bad" style="width:100%">⚠️ {html.escape(qr["error"])}</div>'
    )
    body = f"""{nav_bar()}
<section class="section"><div class="container order-wrap">
<div class="order-head">
  <div><div class="section-tag">Đơn hàng #{order['id']}</div>
  <h2 style="margin-top:6px">Thanh toán <span class="gold-text">Key {plan_label(plan, 'VI')}</span></h2></div>
  <span class="pill">Giao key tự động</span>
</div>

<div class="card qr-card">
  <div class="qr-amount">{format_vnd(order['total_price'])}</div>
  <div style="color:var(--muted);font-size:14px">Gói {plan_label(plan, 'VI')} — {order['quantity']} key</div>
  {qr_html}
  <div class="qr-content">{html.escape(order['payment_content'])}</div>
</div>

<div class="bank-card">{bank_html}</div>

<div id="countdown" class="countdown">⏳ Đang kiểm tra thanh toán…</div>
<div class="codes-box card" id="codes-box">
  <h3>✅ THANH TOÁN THÀNH CÔNG — KEY CỦA BẠN:</h3>
  <div id="codes-list"></div>
  <p class="bank-note">💡 Kích hoạt ngay bên dưới, hoặc gửi key cho khách và dùng lệnh <b>/redeem &lt;key&gt; &lt;link_locket&gt;</b> trên bot Telegram.<br>Nếu Gold rớt sau thời gian dài sử dụng, vào lại web và bấm <b>Kích hoạt lại miễn phí</b>.</p>
  <div class="redeem-row">
    <input id="order-redeem-username" placeholder="Username hoặc link Locket cần kích hoạt" autocomplete="off" spellcheck="false">
    <button class="btn btn-gold" id="order-redeem-btn" onclick="redeemFromOrder()">🚀 Kích hoạt ngay</button>
  </div>
  <div id="order-redeem-result" style="margin-top:14px"></div>
</div>
<div class="status-tip" id="status-tip">Chuyển khoản đúng số tiền và nội dung ở trên. Key được giao tự động khi ngân hàng xác nhận.</div>
</div></section>
<footer><div class="container">© 2026 Locket Gold — Đơn #{order['id']}</div></footer>

<script>
const ORDER_ID={order['id']}, EXPIRES={expires_ms};
let FIRST_CODE=null;
function esc(s){{return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}
const countdown=document.getElementById('countdown'), codesBox=document.getElementById('codes-box');
function tick(){{
  const left=EXPIRES-Date.now();
  if(left<=0){{countdown.innerHTML='⌛ Đơn đã hết hạn. Tạo đơn mới nếu chưa thanh toán.';return true;}}
  const m=Math.floor(left/60000),s=Math.floor(left%60000/1000);
  countdown.innerHTML='⏳ Đơn hết hạn sau <b>'+m+'</b> phút <b>'+String(s).padStart(2,'0')+'</b> giây';
  return false;}}
function renderCodes(codes){{
  if(!codes||!codes.length)return;
  FIRST_CODE=FIRST_CODE||codes[0];
  document.getElementById('codes-list').innerHTML=codes.map(function(c){{
    return '<div class="code-line"><span>'+esc(c)+'</span><button data-copy="'+esc(c)+'">Copy</button></div>';
  }}).join('');
  codesBox.style.display='block';
  countdown.className='countdown ok';countdown.innerHTML='✅ Thanh toán thành công!';
  document.getElementById('status-tip').style.display='none';
}}
async function redeemFromOrder(){{
  const username=document.getElementById('order-redeem-username').value.trim();
  const btn=document.getElementById('order-redeem-btn');
  const box=document.getElementById('order-redeem-result');
  if(!FIRST_CODE){{box.innerHTML='<div class="result-card bad"><div class="big">⚠️ Chưa có key</div></div>';return;}}
  if(!username){{alert('Nhập username hoặc link Locket');return;}}
  btn.disabled=true;btn.textContent='⏳ Đang kích hoạt...';
  box.innerHTML='<div class="result-card info"><div class="big">⏳ Đang kích hoạt Gold...</div></div>';
  try{{
    const r=await fetch('/api/redeem',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{code:FIRST_CODE,username:username}})}});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Lỗi hệ thống');
    box.innerHTML='<div class="result-card ok"><div class="big">🎉 KÍCH HOẠT THÀNH CÔNG</div><div>User: <b>'+esc(d.username)+'</b><br>Hạn Gold: <b>'+esc(d.expires||'—')+'</b>'+(d.days_left?' (còn '+d.days_left+' ngày)':'')+'</div></div>';
  }}catch(e){{box.innerHTML='<div class="result-card bad"><div class="big">❌ Thất bại</div><div class="meta">'+esc(e.message)+'</div></div>';}}
  btn.disabled=false;btn.textContent='🚀 Kích hoạt ngay';
}}
async function poll(){{
  if(tick()&&!window.__done)return;
  try{{
    const r=await fetch('/api/order/'+ORDER_ID);const d=await r.json();
    if(d.status==='completed'){{renderCodes(d.codes);window.__done=true;clearInterval(window.__timer);return;}}
    if(d.status==='expired'){{countdown.innerHTML='⌛ Đơn đã hết hạn.';window.__done=true;clearInterval(window.__timer);}}
  }}catch(e){{}}
}}
window.__timer=setInterval(poll,4000);poll();
</script>"""
    return page(f"Đơn #{order['id']} — Locket Gold", body)


def verify_page():
    body = f"""{nav_bar()}
<section class="section"><div class="container">
<div class="card redeem-box" style="text-align:center">
<div class="section-tag">Kiểm tra key</div>
<h2 style="font-size:26px;margin-top:8px">Xác minh <span class="gold-text">key</span></h2>
<p class="lead" style="margin:8px auto 0">Dán key để xem trạng thái, gói và số lượt còn lại.</p>
<input class="verify-input" id="verify-code" placeholder="LK-GOLD-XXXXXX" autocomplete="off" spellcheck="false">
<button class="btn btn-gold" onclick="verify()" style="justify-content:center">🔍 Kiểm tra</button>
<div id="verify-result"></div>

<div style="margin-top:34px;border-top:1px dashed var(--line);padding-top:26px">
  <div class="section-tag">Kích hoạt luôn</div>
  <h3 style="margin:10px 0 0;font-size:19px">Kích hoạt Gold bằng key</h3>
  <div class="redeem-row">
    <input id="rw-code" placeholder="Key của bạn" autocomplete="off" spellcheck="false">
    <input id="rw-username" placeholder="Username hoặc link locket.cam/..." autocomplete="off" spellcheck="false">
    <button class="btn btn-gold" id="rw-btn" onclick="redeemKey()">🚀 Kích hoạt</button>
  </div>
  <div id="rw-result" style="margin-top:14px"></div>
</div>
</div></div></section>
<footer><div class="container">© 2026 Locket Gold — Kiểm tra &amp; kích hoạt key.</div></footer>

<script>
function esc(s){{return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}
async function verify(){{
  const code=document.getElementById('verify-code').value.trim();
  const box=document.getElementById('verify-result');
  if(!code)return;
  box.style.display='block';box.innerHTML='<div class="result-card info"><div class="big">⏳ Đang kiểm tra...</div></div>';
  try{{
    const r=await fetch('/api/verify',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{code:code}})}});
    const d=await r.json();
    const planName=d.plan==='1y'?'1 Năm':'1 Tháng';
    if(d.status==='valid'){{box.innerHTML='<div class="result-card ok"><div class="big">✅ Key hợp lệ</div><div>Mã <b>'+esc(d.code)+'</b> — gói <b>'+planName+'</b>, còn <b>'+d.spins_left+'/'+d.spins+'</b> lượt.</div></div>';document.getElementById('rw-code').value=d.code||code;}}
    else if(d.status==='used')box.innerHTML='<div class="result-card bad"><div class="big">❌ Key đã dùng hết lượt</div><div class="meta">Dùng lần cuối: '+esc(d.used_at||'—')+'</div></div>';
    else if(d.status==='reserved')box.innerHTML='<div class="result-card info"><div class="big">⏳ Key đang được giữ</div><div class="meta">Thử lại sau ít phút.</div></div>';
    else box.innerHTML='<div class="result-card bad"><div class="big">⚠️ Không tìm thấy key</div><div class="meta">Mã không tồn tại hoặc sai định dạng.</div></div>';
  }}catch(e){{box.innerHTML='<div class="result-card bad"><div class="big">⚠️ Lỗi kiểm tra</div><div class="meta">'+esc(e.message)+'</div></div>';}}
}}
async function redeemKey(){{
  const code=document.getElementById('rw-code').value.trim();
  const username=document.getElementById('rw-username').value.trim();
  const btn=document.getElementById('rw-btn');
  const box=document.getElementById('rw-result');
  if(!code||!username){{alert('Nhập key và username Locket');return;}}
  btn.disabled=true;btn.textContent='⏳ Đang kích hoạt...';
  box.innerHTML='<div class="result-card info"><div class="big">⏳ Đang kích hoạt Gold...</div></div>';
  try{{
    const r=await fetch('/api/redeem',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{code:code,username:username}})}});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Lỗi hệ thống');
    box.innerHTML='<div class="result-card ok"><div class="big">🎉 KÍCH HOẠT THÀNH CÔNG</div><div>User: <b>'+esc(d.username)+'</b><br>Hạn Gold: <b>'+esc(d.expires||'—')+'</b>'+(d.days_left?' (còn '+d.days_left+' ngày)':'')+'<br>Key còn <b>'+d.spins_left+'</b> lượt.</div></div>';
  }}catch(e){{box.innerHTML='<div class="result-card bad"><div class="big">❌ Thất bại</div><div class="meta">'+esc(e.message)+'</div></div>';}}
  btn.disabled=false;btn.textContent='🚀 Kích hoạt';
}}
</script>"""
    return page("Kiểm tra & kích hoạt key — Locket Gold", body)


# ---------------------------------------------------------------------------
# Admin pages
# ---------------------------------------------------------------------------

ADMIN_SHELL = """<nav class="nav"><div class="container nav-inner">
<a class="logo" href="/"><span class="mark">👑</span>Locket <span class="gold-text">Gold</span></a>
<div class="nav-links"><a href="/admin/logout">Đăng xuất</a></div></div></nav>"""


def admin_page(title, body, active=""):
    links = [
        ("/admin", "Tổng quan", "dash"),
        ("/admin/orders", "Đơn hàng", "orders"),
        ("/admin/keys", "Kho key", "keys"),
        ("/admin/sources", "Nguồn Gold", "sources"),
    ]
    side = "".join(
        f'<a href="{href}" {"class=\'active\'" if active==key else ""}>{label}</a>'
        for href, label, key in links
    )
    return page(title, f"""{ADMIN_SHELL}
<div class="admin-layout">
<div class="admin-side">{side}</div>
<div class="admin-main">{body}</div>
</div>""")


def login_page(error=None, csrf=""):
    error_html = f'<div class="auth-error">{html.escape(error)}</div>' if error else ""
    return page("Đăng nhập quản trị — Locket Gold", f"""{ADMIN_SHELL}
<div class="card auth-card">
<div class="logo"><span class="mark">👑</span>Quản trị</div>
{error_html}
<form method="post" action="/admin/login">
<input type="hidden" name="csrf" value="{html.escape(csrf)}">
<div class="field"><label>Tên đăng nhập</label><input name="username" autocomplete="username" required></div>
<div class="field"><label>Mật khẩu</label><input type="password" name="password" autocomplete="current-password" required></div>
<button class="btn btn-gold" type="submit" style="justify-content:center">🔐 Đăng nhập</button>
</form></div>""")


def _status_tag(status):
    return f'<span class="tag {html.escape(status)}">{html.escape(status)}</span>'


def admin_dashboard():
    order_stats = db.cdk_order_stats()
    key_stats = db.cdk_stats()
    source_stats = db.gold_source_stats()
    recent = db.list_cdk_orders(limit=8)
    rows = "".join(
        f"<tr><td>#{row['id']}</td><td>{row['user_id']}</td>"
        f"<td>{plan_label(row.get('plan'), 'VI')}</td><td>{row['quantity']}</td>"
        f"<td>{format_vnd(row['total_price'] or 0)}</td><td>{_status_tag(row['status'])}</td>"
        f"<td>{_fmt_dt(row['created_at'])}</td></tr>"
        for row in recent
    )
    body = f"""
<h1>Tổng quan</h1>
<div class="sub">Doanh thu, key và kho nguồn Gold dùng chung với bot.</div>
<div class="stats">
  <div class="stat"><div class="v gold-text">{format_vnd(order_stats['revenue'])}</div><div class="k">Doanh thu</div></div>
  <div class="stat"><div class="v">{order_stats['completed']}</div><div class="k">Đơn hoàn tất</div></div>
  <div class="stat"><div class="v">{order_stats['pending']}</div><div class="k">Đơn chờ</div></div>
  <div class="stat"><div class="v">{key_stats['unused']}/{key_stats['total']}</div><div class="k">Key chưa dùng</div></div>
  <div class="stat"><div class="v">{source_stats['usable']}/{source_stats['total']}</div><div class="k">Nguồn khả dụng</div></div>
</div>
<h1 style="font-size:19px">Đơn gần đây</h1>
<div class="sub"></div>
<div class="card" style="padding:10px 14px"><table class="table">
<tr><th>ID</th><th>User</th><th>Gói</th><th>SL</th><th>Tiền</th><th>Trạng thái</th><th>Tạo lúc</th></tr>
{rows or '<tr><td colspan="7">Chưa có đơn nào.</td></tr>'}
</table></div>"""
    return admin_page("Tổng quan — Admin", body, active="dash")


def admin_orders():
    orders = db.list_cdk_orders(limit=200)
    rows = "".join(
        f"<tr><td>#{row['id']}</td><td>{row['user_id']}</td>"
        f"<td>{plan_label(row.get('plan'), 'VI')}</td><td>{row['quantity']}</td>"
        f"<td>{format_vnd(row['total_price'] or 0)}</td><td>{_status_tag(row['status'])}</td>"
        f"<td class='mono'>{html.escape(str(row['payment_content'] or ''))}</td>"
        f"<td>{_fmt_dt(row['created_at'])}</td><td>{_fmt_dt(row['completed_at'])}</td></tr>"
        for row in orders
    )
    body = f"""
<h1>Đơn hàng</h1>
<div class="sub">Toàn bộ đơn mua key từ web và bot.</div>
<div class="card" style="padding:10px 14px"><table class="table">
<tr><th>ID</th><th>User</th><th>Gói</th><th>SL</th><th>Tiền</th><th>Trạng thái</th><th>Nội dung</th><th>Tạo</th><th>Hoàn tất</th></tr>
{rows or '<tr><td colspan="9">Chưa có đơn nào.</td></tr>'}
</table></div>"""
    return admin_page("Đơn hàng — Admin", body, active="orders")


def admin_keys(csrf=""):
    keys = db.list_cdk_codes(limit=200, secret=CDK_SECRET)
    rows = "".join(
        f"<tr><td class='mono'>{html.escape(row.get('code') or row['code_hash'][:16])}</td>"
        f"<td>{plan_label(row.get('plan'), 'VI')}</td>"
        f"<td>{row.get('spins_left', 0)}/{row.get('spins', 1)}</td>"
        f"<td>{_status_tag('valid' if (row.get('spins_left') or 0) > 0 else 'used')}</td>"
        f"<td>{row['used_by'] if row['used_by'] is not None else '—'}</td>"
        f"<td>{html.escape(str(row.get('source') or 'admin'))}</td>"
        f"<td>{_fmt_dt(row.get('created_ts'))}</td></tr>"
        for row in keys
    )
    body = f"""
<h1>Kho key</h1>
<div class="sub">Tạo key thủ công (dùng được trên web và bot).</div>
<div class="admin-form">
  <div class="field"><label>Số lượng key</label><input id="gen-count" type="number" min="1" max="500" value="1"></div>
  <div class="field"><label>Số lượt / key</label><input id="gen-spins" type="number" min="1" max="500" value="1"></div>
  <div class="field"><label>Gói</label><select id="gen-plan"><option value="1m">1 Tháng</option><option value="1y">1 Năm</option></select></div>
  <button class="btn btn-gold" id="gen-btn" onclick="genKeys()">🎟️ Tạo key</button>
</div>
<div class="gen-result" id="gen-result"></div>
<div class="card" style="padding:10px 14px"><table class="table">
<tr><th>Mã</th><th>Gói</th><th>Lượt</th><th>Trạng thái</th><th>Dùng bởi</th><th>Nguồn</th><th>Tạo lúc</th></tr>
{rows or '<tr><td colspan="7">Chưa có key nào.</td></tr>'}
</table></div>
<script>
const CSRF={json.dumps(csrf)};
function esc(s){{return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
async function genKeys(){{
  const count=parseInt(document.getElementById('gen-count').value||'1',10);
  const spins=parseInt(document.getElementById('gen-spins').value||'1',10);
  const plan=document.getElementById('gen-plan').value;
  const btn=document.getElementById('gen-btn'),box=document.getElementById('gen-result');
  btn.disabled=true;btn.textContent='⏳ Đang tạo...';
  try{{
    const r=await fetch('/admin/keys/generate',{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF}},body:JSON.stringify({{count:count,spins:spins,plan:plan}})}});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Lỗi hệ thống');
    box.style.display='block';box.textContent=d.codes.join('\\n');
  }}catch(e){{box.style.display='block';box.textContent='❌ '+e.message;}}
  btn.disabled=false;btn.textContent='🎟️ Tạo key';
}}
</script>"""
    return admin_page("Kho key — Admin", body, active="keys")


def admin_sources(csrf=""):
    sources = db.list_gold_sources()
    rows = "".join(
        f"<tr><td class='mono'>{html.escape(row['username'])}</td>"
        f"<td>{row['count']}/5</td>"
        f"<td>{row['in_flight']}</td>"
        f"<td>{row['slots_left']}</td>"
        f"<td>{row['days_left']}</td>"
        f"<td>{html.escape(str(row['expires_at'] or '—'))}</td>"
        f"<td><button class='btn-sm danger' onclick=\"removeSource('{html.escape(row['username'])}')\">Xóa</button></td></tr>"
        for row in sources
    )
    body = f"""
<h1>Nguồn Gold</h1>
<div class="sub">Kho nguồn dùng chung với bot. Nguồn dưới 10 ngày hoặc chạm 5/5 lượt sẽ bị loại tự động.</div>
<div class="admin-form">
  <div class="field"><label>Thêm nguồn (username hoặc link Locket)</label><input id="src-name" placeholder="https://locket.cam/username"></div>
  <button class="btn btn-gold" id="src-btn" onclick="addSource()">➕ Kiểm tra &amp; thêm</button>
  <button class="btn btn-ghost" id="clean-btn" onclick="cleanSources()">🧹 Dọn nguồn hết hạn</button>
</div>
<div class="gen-result" id="src-result"></div>
<div class="card" style="padding:10px 14px"><table class="table">
<tr><th>Username</th><th>Đã dùng</th><th>Đang xử lý</th><th>Còn slot</th><th>Còn ngày</th><th>Hết hạn</th><th></th></tr>
{rows or '<tr><td colspan="7">Kho nguồn đang trống.</td></tr>'}
</table></div>
<script>
const CSRF={json.dumps(csrf)};
function esc(s){{return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
function show(msg){{const box=document.getElementById('src-result');box.style.display='block';box.textContent=msg;}}
async function addSource(){{
  const name=document.getElementById('src-name').value.trim();
  const btn=document.getElementById('src-btn');
  if(!name)return;
  btn.disabled=true;btn.textContent='⏳ Đang kiểm tra...';
  try{{
    const r=await fetch('/admin/sources/add',{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF}},body:JSON.stringify({{username:name}})}});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Lỗi hệ thống');
    show('✅ Đã thêm @'+d.username+' — Gold còn '+d.days_left+' ngày (hạn '+d.expires+')');
    setTimeout(function(){{location.reload();}},1200);
  }}catch(e){{show('❌ '+e.message);}}
  btn.disabled=false;btn.textContent='➕ Kiểm tra & thêm';
}}
async function removeSource(name){{
  if(!confirm('Xóa nguồn @'+name+' khỏi kho?'))return;
  try{{
    const r=await fetch('/admin/sources/remove',{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF}},body:JSON.stringify({{username:name}})}});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Lỗi hệ thống');
    location.reload();
  }}catch(e){{show('❌ '+e.message);}}
}}
async function cleanSources(){{
  const btn=document.getElementById('clean-btn');
  btn.disabled=true;btn.textContent='⏳ Đang dọn...';
  try{{
    const r=await fetch('/admin/sources/cleanup',{{method:'POST',headers:{{'X-CSRF-Token':CSRF}}}});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Lỗi hệ thống');
    show('🧹 Đã dọn '+d.removed+' nguồn không còn dùng được.');
    setTimeout(function(){{location.reload();}},1200);
  }}catch(e){{show('❌ '+e.message);}}
  btn.disabled=false;btn.textContent='🧹 Dọn nguồn hết hạn';
}}
</script>"""
    return admin_page("Nguồn Gold — Admin", body, active="sources")


# ---------------------------------------------------------------------------
# Handlers: storefront
# ---------------------------------------------------------------------------

async def index(request):
    return web.Response(text=landing_page(), content_type="text/html")


async def verify_page_handler(request):
    return web.Response(text=verify_page(), content_type="text/html")


async def api_create_order(request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "Yêu cầu không hợp lệ."}, status=400)
    if payment_config_errors():
        return web.json_response({"ok": False, "error": "Cửa hàng tạm đóng. Liên hệ admin."}, status=503)

    plan = str(data.get("plan", "1m")).lower()
    if plan not in ("1m", "1y"):
        return web.json_response({"ok": False, "error": "Gói không hợp lệ."}, status=400)
    try:
        quantity = int(data.get("quantity", 1))
    except (TypeError, ValueError):
        quantity = 1
    if quantity not in range(1, MAX_QUANTITY + 1):
        return web.json_response({"ok": False, "error": f"Số lượng 1-{MAX_QUANTITY}."}, status=400)

    price = _price_for_plan(plan)
    if price <= 0:
        return web.json_response({"ok": False, "error": "Gói này chưa được cấu hình giá."}, status=503)

    visitor = _visitor_id(request)
    order = db.create_cdk_order(
        user_id=visitor,
        chat_id=None,
        quantity=quantity,
        total_price=price * quantity,
        payment_content=_gen_payment_content(),
        expires_at=_now_ts() + CDK_ORDER_TIMEOUT_MINUTES * 60,
        plan=plan,
    )
    asyncio.create_task(_complete_web_order(order["id"]))
    response = web.json_response({"ok": True, "order_id": order["id"]})
    response.set_cookie(VISITOR_COOKIE, str(visitor), max_age=365 * 24 * 3600, httponly=True, samesite="lax")
    return response


async def order_page_handler(request):
    try:
        order_id = int(request.match_info["order_id"])
    except ValueError:
        raise web.HTTPNotFound()
    order = db.get_cdk_order(id=order_id)
    if not order:
        raise web.HTTPNotFound()
    return web.Response(text=order_page(order), content_type="text/html")


async def api_order_status(request):
    try:
        order_id = int(request.match_info["order_id"])
    except ValueError:
        return web.json_response({"status": "not_found"})
    order = db.get_cdk_order(id=order_id)
    if not order:
        return web.json_response({"status": "not_found"})
    codes = None
    if order["status"] == "completed":
        codes = db.complete_cdk_order(
            order_id=order_id,
            transaction_id=order["transaction_id"],
            matched_amount=order["matched_amount"],
            secret=CDK_SECRET,
        ) or []
    return web.json_response({
        "status": order["status"],
        "codes": codes,
        "plan": order.get("plan") or "1m",
        "quantity": order["quantity"],
        "total_price": order["total_price"],
        "expires_at": order["expires_at"],
    })


async def api_verify(request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "Yêu cầu không hợp lệ."}, status=400)
    code = str(data.get("code", "")).strip().upper()
    if not code:
        return web.json_response({"ok": False, "error": "Nhập mã key."}, status=400)
    detail = db.get_cdk_detail(code, secret=CDK_SECRET)
    detail["ok"] = True
    return web.json_response(detail)


def _parse_locket_username(text):
    raw = (text or "").strip()
    if not raw:
        return ""
    if "links/" in raw.lower():
        return raw
    for marker in ("locket.camera/invites/", "locket.cam/invites/", "locket.camera/", "locket.cam/"):
        if marker in raw:
            raw = raw.split(marker, 1)[1]
            break
    return raw.split("?", 1)[0].strip().strip("/").lstrip("@")[:60]


def _normalize_avatar(url):
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    return url


async def api_check(request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "Yêu cầu không hợp lệ."}, status=400)
    username = _parse_locket_username(str(data.get("username", "")))
    if not username:
        return web.json_response({"ok": False, "error": "Nhập username Locket của bạn."}, status=400)
    profile = await locket.resolve_profile(username)
    if not profile:
        return web.json_response({"ok": False, "error": "Không tìm thấy tài khoản Locket."}, status=404)
    uid = profile["uid"]
    status = await locket.check_status(uid)
    gold_active = bool(status and status.get("active"))
    activation = db.get_uid_activation(uid)
    return web.json_response({
        "ok": True,
        "uid": uid,
        "username": username,
        "avatar": _normalize_avatar(profile.get("avatar")),
        "gold_active": gold_active,
        "expires": (status or {}).get("expires") if gold_active else None,
        "can_reactivate": activation is not None,
        "activations": (activation or {}).get("activations", 0),
    })


async def api_reactivate(request):
    """Free re-activation for UIDs this shop has activated before."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "Yêu cầu không hợp lệ."}, status=400)
    username = _parse_locket_username(str(data.get("username", "")))
    if not username:
        return web.json_response({"ok": False, "error": "Nhập username hoặc link Locket."}, status=400)

    uid = await locket.resolve_uid(username)
    if uid == "IP_BLOCKED":
        return web.json_response({"ok": False, "error": "Dịch vụ đang tạm chặn IP, thử lại sau."}, status=503)
    if not uid:
        return web.json_response({"ok": False, "error": "Không tìm thấy tài khoản Locket."}, status=404)

    record = db.get_uid_activation(uid)
    if not record:
        return web.json_response({
            "ok": False,
            "error": "Tài khoản này chưa từng kích hoạt tại shop — vui lòng mua key để kích hoạt.",
        }, status=403)

    now = _now_ts()
    cooldown = FREE_REACTIVATE_COOLDOWN_MINUTES * 60
    last_at = int(record.get("last_at") or 0)
    if cooldown and now - last_at < cooldown:
        remaining = max(1, (cooldown - (now - last_at)) // 60)
        return web.json_response({
            "ok": False,
            "error": f"Tài khoản vừa được kích hoạt. Vui lòng thử lại sau ~{remaining} phút.",
        }, status=429)

    visitor = _visitor_id(request)
    used_today = db.count_free_reactivations(visitor, now - 86400)
    if used_today >= FREE_REACTIVATE_DAILY_MAX:
        return web.json_response({
            "ok": False,
            "error": "Bạn đã dùng hết lượt kích hoạt lại miễn phí hôm nay. Vui lòng thử lại sau.",
        }, status=429)

    result = await activation.activate(username, plan="1m")
    if not result["ok"]:
        messages = {
            "not_found": "Không tìm thấy tài khoản Locket đích.",
            "already_gold": result.get("message") or "Tài khoản đã có Gold.",
            "no_source": "Kho nguồn đang trống, vui lòng thử lại sau.",
            "ip_blocked": "Dịch vụ đang tạm chặn IP, thử lại sau.",
            "proxy_error": "Lỗi kết nối proxy.",
        }
        return web.json_response(
            {"ok": False, "error": messages.get(result["code"], result.get("message") or "Kích hoạt lại thất bại.")},
            status=400,
        )

    db.mark_uid_activated(uid)
    db.log_key_redemption("FREE-REACTIVATE", visitor, username, uid, "1m", status="success",
                          detail=f"source={result.get('source')}")
    response = web.json_response({
        "ok": True,
        "uid": uid,
        "username": username,
        "plan": "1m",
        "expires": result.get("expires"),
        "days_left": result.get("days_left", 0),
        "free": True,
    })
    response.set_cookie(VISITOR_COOKIE, str(visitor), max_age=365 * 24 * 3600, httponly=True, samesite="lax")
    return response


async def api_redeem(request):
    """Redeem a key on the web: consume one spin and run the alias activation."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "Yêu cầu không hợp lệ."}, status=400)
    code = str(data.get("code", "")).strip().upper()
    username = _parse_locket_username(str(data.get("username", "")))
    if not code:
        return web.json_response({"ok": False, "error": "Nhập key của bạn."}, status=400)
    if not username:
        return web.json_response({"ok": False, "error": "Nhập username hoặc link Locket."}, status=400)

    visitor = _visitor_id(request)
    ok, reason, plan, left, _source = db.consume_key(code, visitor, secret=CDK_SECRET)
    if not ok:
        message = "Key không tồn tại hoặc không hợp lệ!" if reason == "not_found" else "Key này đã hết lượt sử dụng!"
        response = web.json_response({"ok": False, "error": message}, status=400)
        return response

    result = await activation.activate(username, plan=plan)
    if not result["ok"]:
        db.refund_key_spin(code, secret=CDK_SECRET)
        messages = {
            "not_found": "Không tìm thấy tài khoản Locket đích.",
            "already_gold": result.get("message") or "Tài khoản đã có Gold.",
            "no_source": "Kho nguồn đang trống. Lượt key đã được hoàn lại.",
            "ip_blocked": "Dịch vụ đang tạm chặn IP. Lượt key đã được hoàn lại, vui lòng thử lại sau.",
            "proxy_error": "Lỗi kết nối proxy. Lượt key đã được hoàn lại.",
        }
        message = messages.get(result["code"], result.get("message") or "Kích hoạt thất bại.")
        response = web.json_response({"ok": False, "error": message, "refunded": True}, status=400)
        response.set_cookie(VISITOR_COOKIE, str(visitor), max_age=365 * 24 * 3600, httponly=True, samesite="lax")
        return response

    db.save_activation(visitor, result["uid"], username)
    db.mark_uid_activated(result["uid"])
    db.log_key_redemption(code, visitor, username, result["uid"], plan, status="success",
                          detail=f"source={result.get('source')}")
    response = web.json_response({
        "ok": True,
        "uid": result["uid"],
        "username": username,
        "plan": plan,
        "expires": result.get("expires"),
        "days_left": result.get("days_left", 0),
        "spins_left": left,
    })
    response.set_cookie(VISITOR_COOKIE, str(visitor), max_age=365 * 24 * 3600, httponly=True, samesite="lax")
    return response


# ---------------------------------------------------------------------------
# Handlers: admin
# ---------------------------------------------------------------------------

async def admin_login_page(request):
    if _is_admin(request):
        raise web.HTTPFound("/admin")
    nonce = secrets.token_hex(16)
    token = hmac.new(WEB_SESSION_SECRET.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()
    response = web.Response(text=login_page(csrf=token), content_type="text/html")
    response.set_cookie(
        CSRF_COOKIE, nonce, max_age=SESSION_TTL_SECONDS, httponly=True,
        samesite="lax", secure=_secure_cookie(request),
    )
    return response


async def admin_login(request):
    if _is_admin(request):
        raise web.HTTPFound("/admin")
    ip = _client_ip(request)
    if not _login_allowed(ip):
        return web.Response(text=login_page("Quá nhiều lần thử. Vui lòng đợi 15 phút."), content_type="text/html", status=429)
    try:
        data = await request.post()
    except Exception:
        data = {}
    if not _check_csrf(request, data.get("csrf", "")):
        return web.Response(text=login_page("Phiên đăng nhập không hợp lệ. Tải lại trang và thử lại."), content_type="text/html", status=400)
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if username != WEB_ADMIN_USER or not _verify_admin_password(password):
        _record_login_fail(ip)
        return web.Response(text=login_page("Sai tên đăng nhập hoặc mật khẩu."), content_type="text/html", status=401)
    response = web.HTTPFound("/admin")
    response.set_cookie(
        SESSION_COOKIE, _make_session_token(username), max_age=SESSION_TTL_SECONDS,
        httponly=True, samesite="lax", secure=_secure_cookie(request),
    )
    return response


async def admin_logout(request):
    response = web.HTTPFound("/admin/login")
    response.del_cookie(SESSION_COOKIE)
    return response


@_require_admin
async def admin_dashboard_handler(request):
    return web.Response(text=admin_dashboard(), content_type="text/html")


@_require_admin
async def admin_orders_handler(request):
    return web.Response(text=admin_orders(), content_type="text/html")


@_require_admin
async def admin_keys_handler(request):
    return web.Response(text=admin_keys(csrf=_csrf_token(request) or ""), content_type="text/html")


@_require_admin
async def admin_sources_handler(request):
    return web.Response(text=admin_sources(csrf=_csrf_token(request) or ""), content_type="text/html")


async def _json_body(request):
    try:
        return await request.json()
    except json.JSONDecodeError:
        return {}


@_require_admin
async def admin_generate_keys(request):
    if not _check_csrf(request, request.headers.get("X-CSRF-Token", "")):
        return web.json_response({"ok": False, "error": "CSRF"}, status=403)
    data = await _json_body(request)
    try:
        count = int(data.get("count", 1))
        spins = int(data.get("spins", 1))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Số lượng không hợp lệ."}, status=400)
    plan = str(data.get("plan", "1m")).lower()
    if plan not in ("1m", "1y"):
        return web.json_response({"ok": False, "error": "Gói không hợp lệ."}, status=400)
    if not (1 <= count <= 500 and 1 <= spins <= 500):
        return web.json_response({"ok": False, "error": "Số lượng 1-500."}, status=400)
    if len(CDK_SECRET) < 32:
        return web.json_response({"ok": False, "error": "Chưa cấu hình CDK_SECRET hợp lệ."}, status=503)
    codes = db.gen_cdk(count, ADMIN_ID, cdk_secret=CDK_SECRET, source="admin", plan=plan, spins=spins)
    if not codes:
        return web.json_response({"ok": False, "error": "Không tạo được key."}, status=500)
    return web.json_response({"ok": True, "codes": codes})


@_require_admin
async def admin_source_add(request):
    if not _check_csrf(request, request.headers.get("X-CSRF-Token", "")):
        return web.json_response({"ok": False, "error": "CSRF"}, status=403)
    data = await _json_body(request)
    username = str(data.get("username", "")).strip()
    if not username:
        return web.json_response({"ok": False, "error": "Nhập username hoặc link Locket."}, status=400)
    outcome = await activation.check_source({"username": username, "uid": None, "count": 0}, probe=False)
    if outcome.get("status") != "usable":
        errors = {
            "not_found": "Không tìm thấy tài khoản Locket.",
            "no_gold": "Tài khoản chưa có Gold.",
            "expiring": "Gold còn quá ít ngày (dưới 10 ngày).",
            "ip_blocked": "Đang bị chặn IP, thử lại sau.",
        }
        return web.json_response({"ok": False, "error": errors.get(outcome.get("status"), "Nguồn không hợp lệ.")}, status=400)
    expires = outcome.get("expires") or ""
    days_left = outcome.get("days_left", 0)
    expires_text = f"expires: {expires} (còn {days_left} ngày)" if expires else ""
    added = db.add_gold_source(
        username, uid=outcome.get("uid"), expires=expires_text, min_days=GOLD_MIN_SOURCE_DAYS,
    )
    if not added:
        return web.json_response({"ok": False, "error": "Không thêm được nguồn."}, status=400)
    normalized = db.normalize_source_username(username)
    return web.json_response({
        "ok": True,
        "username": normalized,
        "expires": expires,
        "days_left": days_left,
    })


@_require_admin
async def admin_source_remove(request):
    if not _check_csrf(request, request.headers.get("X-CSRF-Token", "")):
        return web.json_response({"ok": False, "error": "CSRF"}, status=403)
    data = await _json_body(request)
    username = str(data.get("username", "")).strip()
    if not username or not db.remove_gold_source(username):
        return web.json_response({"ok": False, "error": "Không tìm thấy nguồn."}, status=404)
    return web.json_response({"ok": True})


@_require_admin
async def admin_source_cleanup(request):
    if not _check_csrf(request, request.headers.get("X-CSRF-Token", "")):
        return web.json_response({"ok": False, "error": "CSRF"}, status=403)
    removed = db.cleanup_gold_sources(min_days=GOLD_MIN_SOURCE_DAYS)
    return web.json_response({"ok": True, "removed": removed})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def build_app():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/verify", verify_page_handler)
    app.router.add_post("/api/order", api_create_order)
    app.router.add_get("/order/{order_id}", order_page_handler)
    app.router.add_get("/api/order/{order_id}", api_order_status)
    app.router.add_post("/api/verify", api_verify)
    app.router.add_post("/api/check", api_check)
    app.router.add_post("/api/redeem", api_redeem)
    app.router.add_post("/api/reactivate", api_reactivate)
    app.router.add_get("/admin/login", admin_login_page)
    app.router.add_post("/admin/login", admin_login)
    app.router.add_get("/admin/logout", admin_logout)
    app.router.add_get("/admin", admin_dashboard_handler)
    app.router.add_get("/admin/orders", admin_orders_handler)
    app.router.add_get("/admin/keys", admin_keys_handler)
    app.router.add_get("/admin/sources", admin_sources_handler)
    app.router.add_post("/admin/keys/generate", admin_generate_keys)
    app.router.add_post("/admin/cdks/generate", admin_generate_keys)
    app.router.add_post("/admin/sources/add", admin_source_add)
    app.router.add_post("/admin/sources/remove", admin_source_remove)
    app.router.add_post("/admin/sources/cleanup", admin_source_cleanup)
    return app


async def main():
    db.init_db()
    imported = db.import_sources_from_file(
        os.path.join(BASE_DIR, "current_source.txt"), min_days=GOLD_MIN_SOURCE_DAYS
    )
    if imported:
        logger.info("Imported %s sources from current_source.txt", imported)

    if not WEB_ADMIN_PASSWORD and not WEB_ADMIN_PASSWORD_HASH:
        logger.warning("WEB_ADMIN_PASSWORD is empty — admin panel disabled.")

    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    logger.info("Web store running at http://%s:%s", WEB_HOST, WEB_PORT)

    poller = asyncio.create_task(web_payment_poller())
    try:
        await asyncio.Event().wait()
    finally:
        poller.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
