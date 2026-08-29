#!/usr/bin/env python3
"""Locket Gold — Web Store + Admin Panel.

A self-contained aiohttp web app that reuses the bot's database and SePay
payment stack to sell CDKs directly on the web:

  * Storefront (/): beautiful product page, quantity selector, buy flow.
  * Order page (/order/<id>): VietQR + bank info, live payment polling,
    auto-delivery of CDK codes.
  * Verify page (/verify): paste a CDK, see if it is valid / used / reserved.
  * Admin panel (/admin): separate login, stats, orders, CDK management.

Web orders are stored in the same cdk_orders table; the bot's Telegram
poller skips them (chat_id IS NULL) and this app's own poller completes
them via the shared SePay client.
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
    CDK_ORDER_TIMEOUT_MINUTES,
    CDK_RESERVATION_TTL_SECONDS,
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
)
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
    return "CDK" + secrets.token_hex(8).upper()


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
        codes = db.complete_cdk_order(
            order_id=order_id,
            transaction_id=transaction["id"],
            matched_amount=transaction["amount_in"],
            secret=CDK_SECRET,
        )
        if codes:
            activation = db.get_web_activation(order_id=order_id)
            if activation and activation["status"] == "awaiting_payment":
                db.mark_uid_paid(activation["uid"], order_id=order_id)
                db.update_web_activation(
                    activation["id"],
                    status="paid",
                    cdk_code=codes[0],
                )
        return codes
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
                    logger.info("Web order #%s completed (%s CDK)", order["id"], len(codes))
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
    # Signature is computed over the canonical base64 payload string.
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
            derived = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt, iterations
            )
            return hmac.compare_digest(derived, expected)
        except (ValueError, TypeError, base64.binascii.Error):
            return False
    return WEB_ADMIN_PASSWORD and hmac.compare_digest(password, WEB_ADMIN_PASSWORD)


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
.nav-admin{font-size:13px;color:var(--muted);border:1px solid var(--line);padding:7px 14px;border-radius:10px}
.nav-admin:hover{color:var(--gold1);border-color:rgba(230,185,79,.4)}
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
/* product */
.product-wrap{display:grid;grid-template-columns:1.05fr .95fr;gap:40px;align-items:center}
@media(max-width:860px){.product-wrap{grid-template-columns:1fr}}
.product-visual{position:relative;border-radius:26px;padding:46px 38px;overflow:hidden;background:linear-gradient(160deg,#151a29,#0d1019);border:1px solid rgba(230,185,79,.25)}
.product-visual::before{content:'';position:absolute;top:-140px;right:-140px;width:380px;height:380px;border-radius:50%;background:radial-gradient(circle,rgba(230,185,79,.22),transparent 65%)}
.product-card-title{font-size:21px;font-weight:800;margin-bottom:6px}
.product-price{font-size:46px;font-weight:800;margin:14px 0 4px}
.product-price small{font-size:16px;color:var(--muted);font-weight:600}
.product-desc{color:var(--muted);font-size:14.5px;margin:14px 0 22px}
.buy-box{padding:34px}
.buy-box h3{font-size:19px;margin-bottom:18px}
.qty-row{display:flex;align-items:center;gap:14px;margin-bottom:20px}
.qty-btn{width:46px;height:46px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);color:var(--text);font-size:22px;font-weight:700;cursor:pointer;transition:.2s;font-family:inherit}
.qty-btn:hover{border-color:var(--gold2);color:var(--gold1)}
.qty-val{font-size:24px;font-weight:800;min-width:44px;text-align:center}
.total-line{display:flex;justify-content:space-between;align-items:baseline;padding:16px 0;border-top:1px dashed var(--line);border-bottom:1px dashed var(--line);margin-bottom:22px}
.total-line .lbl{color:var(--muted);font-size:14px}
.total-line .amt{font-size:28px;font-weight:800}
.bank-note{font-size:12.5px;color:var(--muted);margin-top:16px;display:flex;gap:8px;align-items:flex-start}
/* steps */
.steps{grid-template-columns:repeat(auto-fit,minmax(240px,1fr));counter-reset:step}
.step{padding:28px;position:relative}
.step .num{font-size:38px;font-weight:800;color:rgba(230,185,79,.35);line-height:1}
.step h3{margin:12px 0 8px;font-size:16px}
.step p{font-size:14px;color:var(--muted)}
/* verify */
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
/* order page */
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
/* admin */
.auth-card{max-width:420px;margin:90px auto;padding:40px;text-align:center}
.auth-card .logo{margin:0 auto 22px;justify-content:center}
.auth-card form{display:flex;flex-direction:column;gap:14px;margin-top:22px}
.field{position:relative;text-align:left}
.field label{display:block;font-size:13px;color:var(--muted);margin-bottom:7px}
.field input{width:100%;padding:14px 16px;border-radius:12px;border:1px solid var(--line);background:var(--panel2);color:var(--text);font-size:15px;font-family:inherit}
.field input:focus{outline:none;border-color:var(--gold2)}
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
footer{border-top:1px solid var(--line);padding:36px 0;text-align:center;color:var(--muted);font-size:13.5px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
.pulse{animation:pulse 1.6s infinite}
/* check-gold flow */
.check-card{max-width:560px;margin:26px auto 0;background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:10px;display:flex;gap:10px;position:relative;z-index:1}
.check-card input{flex:1;min-width:0;background:transparent;border:none;outline:none;color:var(--text);font-size:15.5px;padding:8px 14px;font-family:inherit}
.check-card input::placeholder{color:var(--muted)}
.check-card .btn{padding:12px 22px}
#check-result{max-width:560px;margin:16px auto 0;text-align:left;position:relative;z-index:1}
.check-user{display:flex;align-items:center;gap:16px;padding:20px 22px;border-radius:16px;background:var(--panel);border:1px solid var(--line);margin-bottom:14px}
.check-user img{width:64px;height:64px;border-radius:50%;object-fit:cover;border:2px solid var(--gold2);flex-shrink:0;background:var(--panel2)}
.check-user .nm{font-weight:800;font-size:17px}
.check-user .uid{font-size:12.5px;color:var(--muted);font-family:ui-monospace,Menlo,Consolas,monospace}
.check-status{display:inline-block;padding:4px 14px;border-radius:99px;font-size:13px;font-weight:700;margin-top:8px}
.check-status.gold{background:rgba(61,220,151,.12);color:var(--green)}
.check-status.free{background:rgba(255,107,107,.12);color:var(--red)}
.check-status.paid{background:rgba(110,168,254,.12);color:var(--blue)}
.act-console{background:#0a0c12;border:1px solid var(--line);border-radius:14px;padding:16px 18px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;white-space:pre-wrap;max-height:260px;overflow-y:auto;color:#b8c2d8;line-height:1.7;margin-top:14px}
.act-console .ok{color:var(--green)}
.act-console .err{color:var(--red)}
.act-console .ok-card{font-family:'Be Vietnam Pro',system-ui,sans-serif;background:rgba(61,220,151,.08);border:1px solid rgba(61,220,151,.3);border-radius:12px;padding:14px 16px}
.act-console .ok-card img{border:2px solid var(--green)}
.act-console .ok-line{margin-top:10px;color:var(--green);font-weight:700;font-size:14px}
.act-progress{margin-top:18px}
.act-progress h3{font-size:16px;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.act-bar{height:12px;border-radius:99px;background:var(--panel2);border:1px solid var(--line);overflow:hidden;margin:12px 0}
.act-bar>div{height:100%;width:0%;border-radius:99px;background:linear-gradient(90deg,var(--gold1),var(--gold2));transition:width .9s ease}
.act-bar.step1>div{width:33%}
.act-bar.step2>div{width:66%}
.act-bar.step3>div{width:100%}
.act-bar.done>div{width:100%;background:linear-gradient(90deg,var(--green),#7ee7a8)}
.act-bar.fail>div{width:100%;background:var(--red)}
.dns-box{margin-top:18px;padding:22px 20px;border-radius:14px;background:rgba(61,220,151,.07);border:1px solid rgba(61,220,151,.35);text-align:center}
.dns-box .link{word-break:break-all;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;margin-top:8px;user-select:all;background:var(--panel2);padding:10px 12px;border-radius:10px}
.dns-title{font-size:18px;font-weight:800;color:var(--green)}
.dns-sub{font-size:13px;color:var(--muted);margin:4px 0 14px}
 .btn-dns{display:flex;width:100%;justify-content:center;align-items:center;gap:8px;padding:16px 20px;border-radius:14px;font-size:17px;font-weight:800;background:linear-gradient(135deg,var(--green),#4fd1a0);color:#04251a;box-shadow:0 8px 28px rgba(61,220,151,.4);cursor:pointer;text-decoration:none;transition:transform .15s ease}
 .btn-dns:hover{transform:translateY(-2px);box-shadow:0 12px 36px rgba(61,220,151,.55);color:#04251a}
 .dns-guide{margin-top:20px;padding:18px;border-radius:14px;background:var(--panel);border:1px solid var(--line);text-align:left}
 .dg-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:14px}
 .dg-title{font-size:15.5px;font-weight:800;color:var(--text)}
 .dg-tabs{display:flex;gap:8px}
 .dg-tab{padding:8px 14px;border-radius:10px;border:1px solid var(--line);background:var(--panel2);color:var(--muted);font-size:13px;font-weight:700;cursor:pointer;font-family:inherit}
 .dg-tab.active{background:rgba(61,220,151,.12);border-color:rgba(61,220,151,.4);color:var(--green)}
 .dg-view{touch-action:pan-y}
 .dg-img{border-radius:14px;overflow:hidden;border:1px solid var(--line);background:#000;text-align:center}
 .dg-img img{width:auto;max-width:100%;max-height:56vh;object-fit:contain;margin:0 auto;display:block}
 .dg-caption{text-align:center;font-size:14.5px;font-weight:600;margin:12px 0 6px;color:var(--text)}
 .dg-nav{display:flex;align-items:center;justify-content:center;gap:16px}
 .dg-btn{width:38px;height:38px;border-radius:50%;border:1px solid var(--line);background:var(--panel2);color:var(--text);font-size:16px;cursor:pointer;font-family:inherit;flex:0 0 auto}
 .dg-btn:disabled{opacity:.35;cursor:not-allowed}
 .dg-dots{display:flex;gap:6px}
 .dg-dot{width:8px;height:8px;border-radius:50%;background:var(--line);cursor:pointer;padding:0;border:none}
 .dg-dot.active{background:var(--green);box-shadow:0 0 8px var(--green)}
 .dg-counter{text-align:center;font-size:12px;color:var(--muted);margin-top:6px}
 .dg-tip{margin-top:14px;padding:12px 14px;border-radius:12px;background:rgba(110,168,254,.08);border:1px solid rgba(110,168,254,.3);font-size:13px;color:var(--muted);line-height:1.55}
 .dg-line{margin:8px 0;font-size:14px;color:var(--text);line-height:1.5}
 .dg-host-row{display:flex;gap:8px;align-items:center;margin-top:12px;flex-wrap:wrap}
 .dg-host{font-family:ui-monospace,Menlo,Consolas,monospace;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:9px 12px;font-size:13.5px;flex:1;min-width:170px;color:var(--text)}
 .dg-copy{padding:9px 14px;border-radius:8px;border:1px solid var(--line);background:var(--panel2);color:var(--text);font-size:13px;cursor:pointer;font-family:inherit;font-weight:700}
 .dg-copy.ok{background:rgba(61,220,151,.12);border-color:rgba(61,220,151,.4);color:var(--green)}
 @media(max-width:480px){.dg-title{font-size:14px}.dg-tab{padding:7px 10px;font-size:12px}}
"""

DNS_GUIDE = """
<div class="dns-guide" id="dns-guide">
  <div class="dg-head">
    <div class="dg-title">📖 Hướng dẫn cài DNS — 5 bước</div>
    <div class="dg-tabs">
      <button class="dg-tab active" data-os="ios" onclick="dgTab(this)">📱 iPhone</button>
      <button class="dg-tab" data-os="android" onclick="dgTab(this)">🤖 Android</button>
    </div>
  </div>
  <div class="dg-panel" id="dg-panel-ios">
    <div class="dg-view" id="dg-view">
      <div class="dg-img"><img id="dg-photo" src="/static/dns/1.jpeg" alt="Bước 1"></div>
      <div class="dg-caption" id="dg-caption"></div>
      <div class="dg-nav">
        <button class="dg-btn" onclick="dgStep(-1)" aria-label="Bước trước">←</button>
        <div class="dg-dots" id="dg-dots"></div>
        <button class="dg-btn" onclick="dgStep(1)" aria-label="Bước sau">→</button>
      </div>
      <div class="dg-counter" id="dg-counter"></div>
    </div>
    <div class="dg-tip">💡 <b>Mẹo:</b> mở link DNS bằng <b>Safari</b> (không dùng Chrome/Zalo). Không thấy hồ sơ đã tải? Vào <b>Cài đặt → chung → VPN &amp; Quản lý thiết bị</b>.</div>
  </div>
  <div class="dg-panel" id="dg-panel-android" style="display:none">
    <div class="dg-tip" style="margin-top:0">🤖 Trên Android không cài được profile qua link — dùng <b>Private DNS</b>:</div>
    <div class="dg-line">1️⃣ Mở <b>Cài đặt → Mạng &amp; Internet</b> (một số máy là <b>Kết nối</b>)</div>
    <div class="dg-line">2️⃣ Bấm vào <b>DNS riêng tư (Private DNS)</b></div>
    <div class="dg-line">3️⃣ Chọn <b>Tên máy chủ</b> → nhập hostname bên dưới</div>
    <div class="dg-line">4️⃣ Bấm <b>Lưu</b> — xong ✅</div>
    <div class="dg-host-row">
      <input class="dg-host" id="dg-host" value="334513.dns.nextdns.io" readonly onclick="this.select()">
      <button class="dg-copy" id="dg-copy" onclick="dgCopyHost(this)">📋 Copy</button>
    </div>
    <div class="dg-tip" style="border-color:rgba(230,185,79,.35);background:rgba(230,185,79,.07)">⚠️ Nhập <b>đúng hostname</b> (có <b>.dns.nextdns.io</b> ở cuối) và chỉ cài <b>1 DNS</b> — cài nhiều sẽ bị trùng, dễ thu hồi Gold.</div>
  </div>
</div>
<script>
const DG_STEPS=[
{img:'/static/dns/1.jpeg',cap:'1️⃣ Nhập tên và bấm <b>Tải</b> — mở link bằng Safari'},
{img:'/static/dns/2.jpeg',cap:'2️⃣ Bấm <b>Cho phép</b> để tải hồ sơ về'},
{img:'/static/dns/3.png',cap:'3️⃣ Vào <b>Cài đặt</b> → bấm vào <b>Đã tải về hồ sơ</b>'},
{img:'/static/dns/4.jpeg',cap:'4️⃣ Bấm <b>Cài đặt</b> và xác nhận'},
{img:'/static/dns/5.jpeg',cap:'5️⃣ DNS hiển thị ở <b>Giới hạn</b> và <b>Proxy</b> là xong ✅'}];
let dgI=0,dgX=null;
function dgRender(){
  const p=DG_STEPS[dgI];
  const ph=document.getElementById('dg-photo');
  if(ph){ph.src=p.img;ph.alt='Bước '+(dgI+1);}
  const cap=document.getElementById('dg-caption');
  if(cap)cap.innerHTML=p.cap;
  const c=document.getElementById('dg-counter');
  if(c)c.textContent='Bước '+(dgI+1)+' / '+DG_STEPS.length;
  const dots=document.getElementById('dg-dots');
  if(dots){
    dots.innerHTML='';
    for(let i=0;i<DG_STEPS.length;i++){
      const d=document.createElement('button');
      d.className='dg-dot'+(i===dgI?' active':'');
      d.setAttribute('aria-label','Bước '+(i+1));
      d.onclick=(function(n){return function(){dgI=n;dgRender();};})(i);
      dots.appendChild(d);
    }
  }
  const btns=document.querySelectorAll('#dns-guide .dg-btn');
  if(btns.length){btns[0].disabled=(dgI===0);btns[1].disabled=(dgI===DG_STEPS.length-1);}
}
function dgStep(d){
  dgI=Math.min(DG_STEPS.length-1,Math.max(0,dgI+d));
  dgRender();
}
function dgTab(btn){
  const os=btn.getAttribute('data-os');
  const pi=document.getElementById('dg-panel-ios');
  const pa=document.getElementById('dg-panel-android');
  if(!pi||!pa)return;
  pi.style.display=(os==='ios')?'block':'none';
  pa.style.display=(os==='android')?'block':'none';
  const tabs=document.querySelectorAll('#dns-guide .dg-tab');
  for(let i=0;i<tabs.length;i++)tabs[i].className='dg-tab'+(tabs[i]===btn?' active':'');
}
function dgCopyHost(btn){
  const host=document.getElementById('dg-host');
  const done=function(){btn.textContent='✅ Copied';btn.classList.add('ok');setTimeout(function(){btn.textContent='📋 Copy';btn.classList.remove('ok');},1500);};
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(host.value).then(done,function(){done();});
  }else{
    host.select();host.setSelectionRange(0,host.value.length);
    try{document.execCommand('copy');}catch(e){}
    done();
  }
}
function dgSetLink(link){
  if(!link)return;
  const m=String(link).match(/profile=([A-Za-z0-9]+)/);
  if(!m)return;
  const h=document.getElementById('dg-host');
  if(h)h.value=m[1]+'.dns.nextdns.io';
}
(function(){
  const v=document.getElementById('dg-view');
  if(!v)return;
  v.addEventListener('touchstart',function(e){dgX=e.touches[0].clientX;},{passive:true});
  v.addEventListener('touchend',function(e){
    if(dgX===null)return;
    const dx=e.changedTouches[0].clientX-dgX;
    dgX=null;
    if(Math.abs(dx)>40)dgStep(dx<0?1:-1);
  },{passive:true});
})();
dgRender();
</script>
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
<a href="/verify">Kiểm tra CDK</a>
</div></div></nav>"""


def landing_page():
    errors = payment_config_errors()
    shop_disabled = bool(errors) or CDK_UNIT_PRICE <= 0
    price = CDK_UNIT_PRICE
    body = f"""{nav_bar()}
<section class="hero"><div class="container">
<span class="pill">✨ Kích hoạt tự động — không cần bot</span>
<h1 style="margin-top:20px">Bật <span class="gold-text">Locket Gold</span><br>chỉ trong vài phút</h1>
<p class="sub">Nhập username Locket của bạn, kiểm tra tình trạng Gold, thanh toán và hệ thống sẽ <b>tự động kích hoạt</b> — hoàn toàn không cần thao tác thêm.</p>

<div class="check-card">
  <input id="check-username" placeholder="Username Locket hoặc link locket.cam/..." autocomplete="off" spellcheck="false">
  <button class="btn btn-gold" id="check-btn" onclick="checkGold()">🔍 Kiểm tra</button>
</div>
<div id="check-result"></div>

<div class="hero-badges">
<span class="badge"><span class="dot"></span>Kích hoạt tự động 100%</span>
<span class="badge"><span class="dot"></span>Thanh toán VietQR</span>
<span class="badge"><span class="dot"></span>Bảo hành 100%</span>
<span class="badge"><span class="dot"></span>Hỗ trợ 24/7</span>
</div></div></section>

<section class="section" id="products"><div class="container">
<div class="section-tag">Sản phẩm</div>
<h2>Gói kích hoạt <span class="gold-text">Gold vĩnh viễn</span></h2>
<p class="lead">Thanh toán một lần, Gold kích hoạt vĩnh viễn cho tài khoản của bạn. Kích hoạt lại sau này hoàn toàn miễn phí.</p>
<div class="product-wrap">
  <div class="product-visual">
    <div class="product-card-title">👑 Locket Gold — <span class="gold-text">Vĩnh viễn</span></div>
    <div class="product-price">{format_vnd(price)} <small>/ 1 tài khoản</small></div>
    <div class="product-desc">
      • Kiểm tra tình trạng Gold trước khi thanh toán<br>
      • Tự động kích hoạt sau khi chuyển khoản<br>
      • Kèm hướng dẫn cài DNS chống mất Gold<br>
      • Bảo hành — lỗi 1 đổi 1 trong 24h
    </div>
    <span class="pill">⭐ 4.9/5 — hơn 1.000 lượt mua</span>
  </div>
  <div class="card buy-box">
    <h3>👑 Kích hoạt Gold của bạn</h3>
    <p class="lead" style="margin:0 0 14px;font-size:14px">Nhập username Locket ở ô trên, hệ thống sẽ kiểm tra tình trạng và đưa bạn đến bước thanh toán.</p>
    <div class="total-line"><span class="lbl">Giá kích hoạt</span><span class="amt gold-text">{format_vnd(price)}</span></div>
    <button class="btn btn-gold" style="width:100%;justify-content:center" onclick="location.href='/#check-username';document.getElementById('check-username').focus()">🚀 Bắt đầu kích hoạt</button>
    {'<p class="bank-note">⚠️ Cửa hàng tạm đóng do thiếu cấu hình thanh toán. Liên hệ admin qua Telegram.</p>' if shop_disabled else '<p class="bank-note">💡 Đã có Gold trước đây? Kích hoạt lại miễn phí, không tốn thêm chi phí.</p>'}
  </div>
</div></div></section>

<section class="section" style="padding-top:0"><div class="container">
<div class="section-tag">Vì sao chọn chúng tôi</div>
<h2>Tại sao <span class="gold-text">Locket Gold</span>?</h2>
<p class="lead">Hệ thống bán hàng tự động, chuyên nghiệp và đáng tin cậy nhất hiện nay.</p>
<div class="grid features">
  <div class="card feature"><div class="icon">⚡</div><h3>Kích hoạt tức thì</h3><p>Hệ thống tự động kích hoạt Gold ngay khi ngân hàng xác nhận giao dịch — không cần chờ admin, không cần bot.</p></div>
  <div class="card feature"><div class="icon">🏦</div><h3>VietQR chuẩn</h3><p>Chuyển khoản qua mã QR ngân hàng, hệ thống đối soát tự động theo nội dung và số tiền.</p></div>
  <div class="card feature"><div class="icon">🛡️</div><h3>Chống mất Gold</h3><p>Kèm hướng dẫn cài DNS chặn để Gold không bị thu hồi sau vài ngày.</p></div>
  <div class="card feature"><div class="icon">🔒</div><h3>Kích hoạt lại miễn phí</h3><p>Đã mua một lần, kích hoạt lại bất cứ lúc nào mà không phải trả thêm bất kỳ chi phí nào.</p></div>
  <div class="card feature"><div class="icon">💬</div><h3>Hỗ trợ 24/7</h3><p>Kênh Telegram luôn sẵn sàng giải đáp mọi thắc mắc sau khi mua hàng.</p></div>
  <div class="card feature"><div class="icon">🤝</div><h3>Bảo hành 1-đổi-1</h3><p>Sản phẩm lỗi được đổi mã mới trong vòng 24h — uy tín đặt lên hàng đầu.</p></div>
</div></div></section>

<section class="section" style="padding-top:0"><div class="container">
<div class="section-tag">Hướng dẫn</div>
<h2>Chỉ 3 bước <span class="gold-text">đơn giản</span></h2>
<p class="lead">Từ khi kiểm tra đến khi có Gold chỉ mất vài phút.</p>
<div class="grid steps">
  <div class="card step"><div class="num">01</div><h3>Nhập username</h3><p>Nhập username Locket hoặc dán link hồ sơ locket.cam để kiểm tra tình trạng Gold.</p></div>
  <div class="card step"><div class="num">02</div><h3>Chuyển khoản</h3><p>Quét mã QR hoặc chuyển khoản đúng số tiền + nội dung hiển thị.</p></div>
  <div class="card step"><div class="num">03</div><h3>Gold tự động bật</h3><p>Hệ thống tự nhận thanh toán và kích hoạt Gold — bạn chỉ cần mở lại ứng dụng Locket.</p></div>
</div></div></section>

<section class="section" style="padding-top:0"><div class="container">
<div class="section-tag">Chống mất Gold</div>
<h2>Hướng dẫn cài <span class="gold-text">DNS</span> — 5 bước</h2>
<p class="lead">Bắt buộc cài sau khi kích hoạt để Gold không bị thu hồi. Vuốt ngang để xem từng bước, chọn đúng thiết bị của bạn.</p>
{DNS_GUIDE}
</div></section>

<section class="section" style="padding-top:0"><div class="container">
<div class="card verify-box">
<div class="section-tag">Kiểm tra mã</div>
<h2 style="font-size:26px">Xác minh CDK đã mua</h2>
<p class="lead" style="margin:8px auto 0">Bạn đã mua CDK? Dán mã vào đây để kiểm tra trạng thái.</p>
<input class="verify-input" id="verify-code" placeholder="LOCK-XXXXXXXX-XXXX-XXXX" autocomplete="off" spellcheck="false">
<button class="btn btn-gold" onclick="verify()" style="justify-content:center">🔍 Kiểm tra</button>
<div id="verify-result"></div>
</div></div></section>

<footer><div class="container">© 2026 Locket Gold — Kích hoạt Gold Locket tự động. Mọi thắc mắc liên hệ kênh Telegram chính thức.</div></footer>

<script>
const PRICE={price};
function esc(s){{return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}
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
    const avatar=d.avatar?'<img src="'+d.avatar+'" alt="avatar" onerror="this.style.display=\\'none\\'">':'<div style="width:64px;height:64px;border-radius:50%;background:var(--panel2);display:flex;align-items:center;justify-content:center;font-size:26px">👤</div>';
    const saved=d.activation&&d.activation.status==='success';
    const never=(!d.activation)&&!d.paid;
    const uname=esc(d.username), uuid=esc(d.uid), uav=esc(d.avatar||'');
    const dataAttr='data-uid="'+uuid+'" data-name="'+uname+'" data-avatar="'+uav+'"';
    if(d.gold_active){{
      const glabel=d.paid?'⚡ Kích hoạt lại miễn phí':'⚡ Kích hoạt ngay — '+PRICE.toLocaleString('vi-VN').replace(/,/g,'.')+'đ';
      box.innerHTML='<div class="check-user">'+avatar+'<div><div class="nm">'+uname+'</div><div class="uid">'+uuid+'</div><div class="check-status gold">✅ Đã có Gold'+(d.expires?' — hết hạn: '+d.expires:'')+'</div></div></div>'
        +'<div class="result-card ok"><div class="big">🎉 Tài khoản của bạn đã có Gold!</div><div>Không cần mua thêm. Nếu Gold bị thu hồi, kích hoạt lại bất cứ lúc nào.</div></div>'
        +'<button class="btn btn-gold" style="width:100%;justify-content:center" '+dataAttr+' onclick="activate(this)">'+glabel+'</button>';
    }}else if(saved){{
      box.innerHTML='<div class="check-user">'+avatar+'<div><div class="nm">'+uname+'</div><div class="uid">'+uuid+'</div><div class="check-status gold">✅ Đã kích hoạt Gold thành công</div></div></div>'
        +'<div class="result-card ok"><div class="big">🎉 Tài khoản đã được kích hoạt</div>'
        +'<div class="meta">CDK đã dùng: <b>'+esc(d.activation.cdk_code)+'</b></div>'
        +(d.activation.dns_link?'<a class="btn btn-dns" style="margin-top:14px" href="'+esc(d.activation.dns_link)+'" target="_blank" rel="noopener">📲 Cài DNS chống mất Gold (mở tab mới)</a>':'')
        +'<div>Gold bị thu hồi? Kích hoạt lại miễn phí bất cứ lúc nào.</div></div>'
        +'<button class="btn btn-gold" style="width:100%;justify-content:center" '+dataAttr+' onclick="activate(this)">⚡ Kích hoạt lại miễn phí</button>';
      if(d.activation.dns_link)dgSetLink(d.activation.dns_link);
    }}else if(never){{
      box.innerHTML='<div class="check-user">'+avatar+'<div><div class="nm">'+uname+'</div><div class="uid">'+uuid+'</div><div class="check-status free">⏳ Chưa có Gold</div></div></div>'
        +'<div class="result-card info"><div class="big">👤 User này chưa từng mua và kích hoạt</div><div class="meta">Hệ thống chưa có hồ sơ kích hoạt nào cho tài khoản <b>'+uname+'</b>.</div></div>'
        +'<button class="btn btn-gold" style="width:100%;justify-content:center;margin-top:16px" '+dataAttr+' onclick="activate(this)">💳 Mua Gold — '+PRICE.toLocaleString('vi-VN').replace(/,/g,'.')+'đ</button>'
        +'<button class="btn btn-ghost" style="width:100%;justify-content:center;margin-top:10px" onclick="toggleCdk(this)">🎟️ Tôi đã có CDK — kích hoạt bằng CDK</button>'
        +'<div class="cdk-row" style="display:none;margin-top:12px"><input class="verify-input" id="cdk-code" placeholder="LOCK-XXXXXXXX-XXXX-XXXX" autocomplete="off" spellcheck="false"><button class="btn btn-gold" style="width:100%;justify-content:center;margin-top:8px" '+dataAttr+' onclick="activateCdk(this)">✅ Kích hoạt bằng CDK</button><div id="cdk-result" style="margin-top:10px"></div></div>';
    }}else{{
      const label=d.paid?'Kích hoạt lại miễn phí':'Kích hoạt ngay — '+PRICE.toLocaleString('vi-VN').replace(/,/g,'.')+'đ';
      const paidCard='<div class="check-user">'+avatar+'<div><div class="nm">'+uname+'</div><div class="uid">'+uuid+'</div><div class="check-status '+(d.paid?'paid':'free')+'">'+(d.paid?'🔄 Đã mua trước đây — kích hoạt lại miễn phí':'⏳ Chưa có Gold')+'</div></div></div>';
      box.innerHTML=paidCard
        +(d.paid?'<button class="btn btn-gold" style="width:100%;justify-content:center" '+dataAttr+' onclick="activate(this)">⚡ '+label+'</button>'
        :'<button class="btn btn-gold" style="width:100%;justify-content:center;margin-top:16px" '+dataAttr+' onclick="activate(this)">💳 Mua Gold — '+PRICE.toLocaleString('vi-VN').replace(/,/g,'.')+'đ</button>'
          +'<button class="btn btn-ghost" style="width:100%;justify-content:center;margin-top:10px" onclick="toggleCdk(this)">🎟️ Tôi đã có CDK — kích hoạt bằng CDK</button>'
          +'<div class="cdk-row" style="display:none;margin-top:12px"><input class="verify-input" id="cdk-code" placeholder="LOCK-XXXXXXXX-XXXX-XXXX" autocomplete="off" spellcheck="false"><button class="btn btn-gold" style="width:100%;justify-content:center;margin-top:8px" '+dataAttr+' onclick="activateCdk(this)">✅ Kích hoạt bằng CDK</button><div id="cdk-result" style="margin-top:10px"></div></div>');
    }}
  }}catch(e){{
    box.innerHTML='<div class="result-card bad"><div class="big">⚠️ Không tìm thấy tài khoản</div><div class="meta">'+e.message+' — Kiểm tra lại username hoặc link hồ sơ Locket của bạn.</div></div>';
  }}
  btn.disabled=false;btn.textContent='🔍 Kiểm tra';
}}
async function activate(btn){{
  const uid=btn.getAttribute('data-uid'), username=btn.getAttribute('data-name'), avatar=btn.getAttribute('data-avatar')||'';
  try{{
    const r=await fetch('/api/activate',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{uid:uid,username:username,avatar:avatar}})}});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Lỗi hệ thống');
    if(d.free)location.href='/activate/'+d.activation_id;
    else location.href='/order/'+d.order_id;
  }}catch(e){{alert(e.message);}}
}}
function toggleCdk(btn){{
  const row=btn.parentElement.querySelector('.cdk-row');
  if(row)row.style.display=row.style.display==='none'?'block':'none';
}}
async function activateCdk(btn){{
  const uid=btn.getAttribute('data-uid'), username=btn.getAttribute('data-name'), avatar=btn.getAttribute('data-avatar')||'';
  const row=btn.parentElement, input=row.querySelector('#cdk-code'), res=row.querySelector('#cdk-result');
  const code=input.value.trim();
  if(!code){{res.innerHTML='<div class="result-card bad"><div class="big">⚠️ Nhập mã CDK</div></div>';return;}}
  btn.disabled=true;btn.textContent='⏳ Đang xử lý CDK...';
  res.innerHTML='<div class="result-card info"><div class="big">⏳ Đang xác minh CDK...</div></div>';
  try{{
    const r=await fetch('/api/activate-cdk',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{uid:uid,username:username,avatar:avatar,code:code}})}});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Lỗi hệ thống');
    location.href='/activate/'+d.activation_id;
  }}catch(e){{res.innerHTML='<div class="result-card bad"><div class="big">❌ '+esc(e.message||'Lỗi hệ thống')+'</div><div class="meta">Mã sai, đã dùng, hoặc đang được người khác giữ. Kiểm tra lại mã CDK của bạn.</div></div>';btn.disabled=false;btn.textContent='✅ Kích hoạt bằng CDK';}}
}}
async function verify(){{
  const code=document.getElementById('verify-code').value.trim();
  const box=document.getElementById('verify-result');
  if(!code)return;
  box.style.display='block';box.innerHTML='<div class="result-card info"><div class="big">⏳ Đang kiểm tra...</div></div>';
  try{{
    const r=await fetch('/api/verify',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{code:code}})}});
    const d=await r.json();
    if(d.status==='valid')box.innerHTML='<div class="result-card ok"><div class="big">✅ CDK hợp lệ</div><div>Mã <b>'+d.code+'</b> còn sử dụng được. Nhập vào bot để kích hoạt Gold.</div></div>';
    else if(d.status==='used')box.innerHTML='<div class="result-card bad"><div class="big">❌ CDK đã được sử dụng</div><div class="meta">Đã kích hoạt lúc: '+d.used_at+'</div></div>';
    else if(d.status==='reserved')box.innerHTML='<div class="result-card info"><div class="big">⏳ CDK đang được giữ</div><div class="meta">Đơn hàng đang xử lý. Thử lại sau ít phút.</div></div>';
    else box.innerHTML='<div class="result-card bad"><div class="big">⚠️ Không tìm thấy CDK</div><div class="meta">Mã không tồn tại hoặc sai định dạng. Kiểm tra lại mã của bạn.</div></div>';
  }}catch(e){{box.innerHTML='<div class="result-card bad"><div class="big">⚠️ Lỗi kiểm tra</div><div class="meta">'+e.message+'</div></div>';}}
}}
</script>"""
    return page("Locket Gold — Kích hoạt Gold tự động", body)


def order_page(order):
    qr = _build_qr(order)
    expires_ms = (order["expires_at"] or 0) * 1000
    activation = db.get_web_activation(order_id=order["id"])
    is_activation = activation is not None
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
    qr_html = f'<img src="{html.escape(qr["url"])}" alt="VietQR" loading="lazy">' if qr["ok"] else \
        f'<div class="result-card bad" style="width:100%">⚠️ {html.escape(qr["error"])}</div>'
    status = order["status"]
    body = f"""{nav_bar()}
<section class="section"><div class="container order-wrap">
<div class="order-head">
  <div><div class="section-tag">Đơn hàng #{order['id']}</div>
  <h2 style="margin-top:6px">Thanh toán <span class="gold-text">{'kích hoạt Gold' if is_activation else 'CDK'}</span></h2></div>
  <span class="pill">{'Kích hoạt tự động' if is_activation else 'Giao hàng tự động'}</span>
</div>

<div class="card qr-card">
  <div class="qr-amount">{format_vnd(order['total_price'])}</div>
  <div style="color:var(--muted);font-size:14px">{'Kích hoạt vĩnh viễn 1 tài khoản Locket' if is_activation else f"Số lượng: {order['quantity']} CDK"}</div>
  {qr_html}
  <div class="qr-content">{html.escape(order['payment_content'])}</div>
</div>

<div class="bank-card">{bank_html}</div>

<div id="countdown" class="countdown">⏳ Đang kiểm tra thanh toán…</div>
<div class="codes-box card" id="codes-box">
  <h3>✅ THANH TOÁN THÀNH CÔNG — {'hệ thống đang tự động kích hoạt Gold cho bạn!' if is_activation else 'CDK của bạn:'}</h3>
  <div id="codes-list"></div>
  <p class="bank-note">💡 {'Quá trình kích hoạt mất khoảng 1-2 phút. Bạn không cần làm gì thêm — Gold sẽ tự động bật trong Locket.' if is_activation else 'Nhập từng mã vào bot Telegram để kích hoạt Gold. Mỗi mã chỉ dùng được một lần.'}</p>
</div>
<div class="act-progress" id="act-progress" style="display:none">
  <h3 id="act-title" class="pulse">⚡ Hệ thống đang kích hoạt Gold...</h3>
  <div class="act-bar" id="act-bar"><div></div></div>
  <div class="act-console" id="act-console"></div>
  <div class="dns-box" id="dns-box" style="display:none">
    <div class="dns-title">🌐 CÀI DNS CHỐNG MẤT GOLD</div>
    <div class="dns-sub">Bắt buộc — cài xong Gold sẽ không bị thu hồi.</div>
    <a class="btn btn-dns" id="dns-link" href="#" target="_blank" rel="noopener">📲 Bấm để cài DNS (mở tab mới)</a>
    {DNS_GUIDE}
  </div>
</div>
<div class="status-tip" id="status-tip">{'Chuyển khoản đúng số tiền và nội dung ở trên. Gold sẽ tự động được kích hoạt sau khi ngân hàng xác nhận.' if is_activation else 'Chuyển khoản đúng số tiền và nội dung ở trên. Đơn tự động giao CDK khi ngân hàng xác nhận.'}</div>
</div></section>
<footer><div class="container">© 2026 Locket Gold — Đơn #{order['id']}</div></footer>

<script>
const ORDER_ID={order['id']}, EXPIRES={expires_ms};
const HAS_ACTIVATION={'true' if is_activation else 'false'};
function esc(s){{return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}
const countdown=document.getElementById('countdown'), codesBox=document.getElementById('codes-box');
function tick(){{
  const left=EXPIRES-Date.now();
  if(left<=0){{countdown.innerHTML='⌛ Đơn đã hết hạn. Kiểm tra lại tài khoản để tạo đơn mới.';return true;}}
  const m=Math.floor(left/60000),s=Math.floor(left%60000/1000);
  countdown.innerHTML='⏳ Đơn hết hạn sau <b>'+m+'</b> phút <b>'+String(s).padStart(2,'0')+'</b> giây';
  return false;}}
function renderCodes(codes){{
  document.getElementById('codes-list').innerHTML=codes.map(function(c){{
    return '<div class="code-line"><span>'+c+'</span><button data-copy="'+c+'">Copy</button></div>';
  }}).join('');
codesBox.style.display='block';
  countdown.className='countdown ok';countdown.innerHTML='✅ Thanh toán thành công!';
  document.getElementById('status-tip').style.display='none';
}}
function renderActivation(a){{
  const box=document.getElementById('act-progress');
  if(!a||a.status==='not_found')return;
  if(a.status==='success'){{
    box.style.display='block';
    document.getElementById('act-bar').className='act-bar done';
    document.getElementById('act-title').innerHTML='🎉 KÍCH HOẠT THÀNH CÔNG — GOLD ĐÃ BẬT!';
    document.getElementById('act-title').className='';
    const av=a.avatar?'<img src="'+esc(a.avatar)+'" alt="" onerror="this.style.display=\\'none\\'" style="width:52px;height:52px;border-radius:50%;object-fit:cover;flex:0 0 auto">':'<div style="width:52px;height:52px;border-radius:50%;background:var(--panel2);display:flex;align-items:center;justify-content:center;font-size:22px;flex:0 0 auto">👤</div>';
    document.getElementById('act-console').innerHTML='<div class="ok-card"><div style="display:flex;gap:14px;align-items:center">'+av+'<div><div style="font-weight:700;font-size:16px">'+esc(a.username||'')+'</div><div style="font-size:12.5px;color:#8a94ad">UID: '+esc(a.uid||'')+'</div></div></div><div class="ok-line">✨ Gold vĩnh viễn đã được bật thành công trên tài khoản của bạn!</div></div>';
    if(a.dns_link){{document.getElementById('dns-link').href=a.dns_link;document.getElementById('dns-box').style.display='block';dgSetLink(a.dns_link);}}
    clearInterval(window.__timer);return;
  }}
  if(a.status==='failed'){{
    document.getElementById('act-bar').className='act-bar fail';
    document.getElementById('act-title').innerHTML='❌ Kích hoạt thất bại';
    document.getElementById('act-title').className='';
    document.getElementById('act-console').innerHTML='<span class="err">'+(a.result||'Lỗi không xác định')+'</span><br><br><a class="btn btn-gold" style="justify-content:center" href="/#check-username">🔄 Thử lại (miễn phí)</a>';
    clearInterval(window.__timer);return;
  }}
  box.style.display='block';
  const p=(a.progress||'').toLowerCase();
  document.getElementById('act-bar').className='act-bar '+(p.indexOf('thành công')>-1?'done':(p.indexOf('dns')>-1||p.indexOf('xong')>-1?'step3':(p.indexOf('đang kích')>-1||p.indexOf('exploit')>-1?'step2':'step1')));
  document.getElementById('act-console').innerHTML=(a.progress||'').replace(/</g,'&lt;').replace(/\\n/g,'<br>')||'<span class="pulse">⏳ Đang xếp hàng xử lý...</span>';
}}
async function poll(){{
  if(tick()&&!window.__done)return;
  try{{
    const r=await fetch('/api/order/'+ORDER_ID);const d=await r.json();
    if(d.status==='completed'){{
      if(d.codes)renderCodes(d.codes);
      window.__done=true;
      if(HAS_ACTIVATION==='true'&&d.activation){{
        location.href='/activate/'+d.activation.id;return;
      }}
      clearInterval(window.__timer);return;
    }}
    if(d.status==='expired'){{countdown.innerHTML='⌛ Đơn đã hết hạn. Kiểm tra lại tài khoản để tạo đơn mới.';window.__done=true;clearInterval(window.__timer);}}
    if(HAS_ACTIVATION==='true'&&d.activation&&d.activation.status!=='awaiting_payment')renderActivation(d.activation);
  }}catch(e){{}}
}}
window.__timer=setInterval(poll,4000);poll();
</script>"""
    return page(f"Đơn #{order['id']} — Locket Gold", body)


def activation_page(activation):
    uid = activation["uid"]
    username = activation["username"] or uid
    avatar = activation.get("avatar") or ""
    avatar_html = f'<img src="{html.escape(avatar)}" alt="avatar" onerror="this.style.display=\'none\'">' if avatar else \
        '<div style="width:64px;height:64px;border-radius:50%;background:var(--panel2);display:flex;align-items:center;justify-content:center;font-size:26px">👤</div>'
    body = f"""{nav_bar()}
<section class="section"><div class="container order-wrap">
<div class="order-head">
  <div><div class="section-tag">Kích hoạt Gold</div>
  <h2 style="margin-top:6px">Đang kích hoạt <span class="gold-text">Gold</span></h2></div>
  <span class="pill">{'✅ Đã thanh toán bằng CDK' if not activation.get('order_id') else '✅ Đã thanh toán'}</span>
</div>

<div class="check-user" style="justify-content:center;margin-bottom:20px">{avatar_html}<div><div class="nm">{html.escape(username)}</div><div class="uid">{html.escape(uid)}</div></div></div>

<div class="act-progress" id="act-progress">
  <h3 id="act-title" class="pulse">⏳ Đang xếp hàng xử lý...</h3>
  <div class="act-bar" id="act-bar"><div></div></div>
  <div class="act-console" id="act-console"></div>
  <div class="dns-box" id="dns-box" style="display:none">
    <div class="dns-title">🌐 CÀI DNS CHỐNG MẤT GOLD</div>
    <div class="dns-sub">Bắt buộc — cài xong Gold sẽ không bị thu hồi.</div>
    <a class="btn btn-dns" id="dns-link" href="#" target="_blank" rel="noopener">📲 Bấm để cài DNS (mở tab mới)</a>
    {DNS_GUIDE}
  </div>
</div>
</div></section>
<footer><div class="container">© 2026 Locket Gold — Kích hoạt #{activation['id']}</div></footer>

<script>
const ACT_ID={activation['id']};
const HAS_ORDER={'true' if activation.get('order_id') else 'false'};
function esc(s){{return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}
function notify(title, body){{
  try{{if('Notification' in window&&Notification.permission==='granted')new Notification(title,{{body:body,icon:'https://locket.gold/icon.png'}});}}catch(e){{}}
}}
if('Notification' in window&&Notification.permission==='default')Notification.requestPermission();
async function startIfNeeded(){{
  if(HAS_ORDER!=='true')return;
  try{{
    const r=await fetch('/api/order/{activation['order_id'] or 0}/start',{{method:'POST'}});
    const d=await r.json();
    if(d&&d.status==='queued'){{document.getElementById('act-console').innerHTML='<span class="pulse">⏳ Đang xếp hàng xử lý...</span>';}}
  }}catch(e){{}}
}}
function renderActivation(a){{
  const box=document.getElementById('act-progress');
  if(!a||a.status==='not_found')return;
  if(a.status==='success'){{
    document.getElementById('act-bar').className='act-bar done';
    document.getElementById('act-title').innerHTML='🎉 KÍCH HOẠT THÀNH CÔNG — GOLD ĐÃ BẬT!';
    document.getElementById('act-title').className='';
    const av=a.avatar?'<img src="'+esc(a.avatar)+'" alt="" onerror="this.style.display=\\'none\\'" style="width:52px;height:52px;border-radius:50%;object-fit:cover;flex:0 0 auto">':'<div style="width:52px;height:52px;border-radius:50%;background:var(--panel2);display:flex;align-items:center;justify-content:center;font-size:22px;flex:0 0 auto">👤</div>';
    document.getElementById('act-console').innerHTML='<div class="ok-card"><div style="display:flex;gap:14px;align-items:center">'+av+'<div><div style="font-weight:700;font-size:16px">'+esc(a.username||'')+'</div><div style="font-size:12.5px;color:#8a94ad">UID: '+esc(a.uid||'')+'</div></div></div><div class="ok-line">✨ Gold vĩnh viễn đã được bật thành công trên tài khoản của bạn!</div></div>';
    if(a.dns_link){{document.getElementById('dns-link').href=a.dns_link;document.getElementById('dns-box').style.display='block';dgSetLink(a.dns_link);}}
    notify('✅ Kích hoạt thành công','Gold của bạn đã được bật!');
    clearInterval(window.__timer);return;
  }}
  if(a.status==='failed'){{
    document.getElementById('act-bar').className='act-bar fail';
    document.getElementById('act-title').innerHTML='❌ Kích hoạt thất bại';
    document.getElementById('act-title').className='';
    document.getElementById('act-console').innerHTML='<span class="err">'+(a.result||'Lỗi không xác định')+'</span><br><br><a class="btn btn-gold" style="justify-content:center" href="/">🔄 Thử lại (miễn phí)</a>';
    clearInterval(window.__timer);return;
  }}
  const p=(a.progress||'').toLowerCase();
  document.getElementById('act-bar').className='act-bar '+(p.indexOf('thành công')>-1?'done':(p.indexOf('dns')>-1||p.indexOf('xong')>-1?'step3':(p.indexOf('đang kích')>-1||p.indexOf('exploit')>-1?'step2':'step1')));
  document.getElementById('act-console').innerHTML=(a.progress||'').replace(/</g,'&lt;').replace(/\\n/g,'<br>')||'<span class="pulse">⏳ Đang xếp hàng xử lý...</span>';
}}
async function poll(){{
  try{{
    const r=await fetch('/api/activation/'+ACT_ID);const d=await r.json();
    renderActivation(d);
  }}catch(e){{}}
}}
startIfNeeded();
window.__timer=setInterval(poll,4000);poll();
</script>"""
    return page("Kích hoạt Gold — Locket Gold", body)


def verify_page():
    body = f"""{nav_bar()}
<section class="section"><div class="container">
<div class="card verify-box" style="margin-top:40px">
<div class="section-tag">Xác minh</div>
<h2 style="font-size:28px">Kiểm tra <span class="gold-text">CDK</span></h2>
<p class="lead" style="margin:8px auto 0">Nhập mã CDK đã mua để xem trạng thái — hợp lệ, đã dùng hay không tồn tại.</p>
<input class="verify-input" id="verify-code" placeholder="LOCK-XXXXXXXX-XXXX-XXXX" autocomplete="off" spellcheck="false">
<button class="btn btn-gold" onclick="verify()" style="justify-content:center">🔍 Kiểm tra ngay</button>
<div id="verify-result"></div>
</div></div></section>
<footer><div class="container">© 2026 Locket Gold — Kiểm tra CDK</div></footer>
<script>
async function verify(){{
  const code=document.getElementById('verify-code').value.trim();
  const box=document.getElementById('verify-result');
  if(!code)return;
  box.style.display='block';box.innerHTML='<div class="result-card info"><div class="big">⏳ Đang kiểm tra...</div></div>';
  try{{
    const r=await fetch('/api/verify',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{code:code}})}});
    const d=await r.json();
    if(d.status==='valid')box.innerHTML='<div class="result-card ok"><div class="big">✅ CDK hợp lệ</div><div>Mã <b>'+d.code+'</b> còn sử dụng được. Nhập vào bot để kích hoạt Gold.</div><div class="meta" style="margin-top:6px">Mã cấp: '+d.created_at+' · Loại: '+d.source+'</div></div>';
    else if(d.status==='used')box.innerHTML='<div class="result-card bad"><div class="big">❌ CDK đã được sử dụng</div><div class="meta">Đã kích hoạt lúc: '+d.used_at+(d.used_by?' · Bởi user #'+d.used_by:'')+'</div></div>';
    else if(d.status==='reserved')box.innerHTML='<div class="result-card info"><div class="big">⏳ CDK đang được giữ</div><div class="meta">Đơn hàng đang xử lý. Thử lại sau ít phút.</div></div>';
    else box.innerHTML='<div class="result-card bad"><div class="big">⚠️ Không tìm thấy CDK</div><div class="meta">Mã không tồn tại hoặc sai định dạng.</div></div>';
  }}catch(e){{box.innerHTML='<div class="result-card bad"><div class="big">⚠️ Lỗi kiểm tra</div><div class="meta">'+e.message+'</div></div>';}}
}}
</script>"""
    return page("Kiểm tra CDK — Locket Gold", body)


# ---------------------------------------------------------------------------
# Admin templates
# ---------------------------------------------------------------------------

ADMIN_SHELL = """<nav class="nav"><div class="container nav-inner">
<a class="logo" href="/"><span class="mark">👑</span>Locket <span class="gold-text">Gold</span></a>
<div class="nav-links"><a href="/">← Về trang chủ</a><a href="/admin/logout">Đăng xuất</a></div></div></nav>
<div class="admin-layout"><div class="admin-side">
<a href="/admin" class="{a_dash}">📊 Tổng quan</a>
<a href="/admin/orders" class="{a_orders}">🧾 Đơn hàng</a>
<a href="/admin/cdks" class="{a_cdks}">🎟️ CDK</a>
</div><div class="admin-main">"""


def admin_page(title, body, active=""):
    shell = ADMIN_SHELL.format(
        a_dash="active" if active == "dash" else "",
        a_orders="active" if active == "orders" else "",
        a_cdks="active" if active == "cdks" else "",
    )
    return page(f"{title} — Admin", shell + body + "</div></div>")


def login_page(error=None, csrf=""):
    error_html = f'<div class="auth-error">{html.escape(error)}</div>' if error else ""
    csrf_field = f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">' if csrf else ""
    body = f"""{nav_bar()}
<div class="card auth-card">
<div class="logo"><span class="mark">🔐</span>Admin <span class="gold-text">Panel</span></div>
<p style="color:var(--muted);font-size:14px;margin-top:6px">Đăng nhập để quản lý cửa hàng</p>
{error_html}
<form method="post" action="/admin/login">
{csrf_field}
<div class="field"><label>Tên đăng nhập</label><input name="username" autocomplete="username" required></div>
<div class="field"><label>Mật khẩu</label><input type="password" name="password" autocomplete="current-password" required></div>
<button class="btn btn-gold" style="justify-content:center">Đăng nhập</button>
</form></div>"""
    return page("Admin Login — Locket Gold", body)


def _status_tag(status):
    known = {"pending", "completed", "expired", "canceled", "queued", "processing", "success", "failed", "awaiting_payment", "paid"}
    cls = status if status in known else "pending"
    labels = {
        "pending": "Chờ thanh toán", "completed": "Hoàn tất", "expired": "Hết hạn", "canceled": "Đã hủy",
        "awaiting_payment": "Chờ thanh toán", "paid": "Đã thanh toán", "queued": "Trong hàng đợi", "processing": "Đang kích hoạt",
        "success": "Thành công", "failed": "Thất bại",
    }
    return f'<span class="tag {cls}">{labels.get(status, status)}</span>'


def admin_dashboard():
    o = db.cdk_order_stats()
    c = db.cdk_stats()
    s = db.get_stats()
    w = db.web_activation_stats()
    orders = db.list_cdk_orders(limit=8)
    rows = "".join(
        f"<tr><td>#{r['id']}</td><td>{r['quantity']}</td>"
        f"<td>{format_vnd(r['total_price'])}</td>"
        f"<td>{_status_tag(r['status'])}</td>"
        f"<td>{_fmt_dt(r['created_at'])}</td>"
        f"<td>{_fmt_dt(r['completed_at'])}</td></tr>"
        for r in orders
    )
    acts = db.list_web_activations(limit=6)
    act_rows = "".join(
        f"<tr><td>#{a['id']}</td><td class='mono'>{html.escape(a['username'] or '—')}</td>"
        f"<td class='mono'>{html.escape(str(a['uid'] or '—'))}</td>"
        f"<td>{_status_tag('completed' if a['status'] == 'success' else a['status'])}</td>"
        f"<td>{_fmt_dt(a['created_at'])}</td></tr>"
        for a in acts
    )
    body = f"""<h1>Tổng quan</h1><div class="sub">Cửa hàng Locket Gold — {time.strftime('%d/%m/%Y %H:%M')}</div>
<div class="stats">
<div class="stat"><div class="v gold-text">{format_vnd(o['revenue'])}</div><div class="k">Doanh thu</div></div>
<div class="stat"><div class="v">{o['completed']}</div><div class="k">Đơn hoàn tất</div></div>
<div class="stat"><div class="v">{o['pending']}</div><div class="k">Đơn chờ</div></div>
<div class="stat"><div class="v">{w['success']}</div><div class="k">Kích hoạt web OK</div></div>
<div class="stat"><div class="v">{w['failed']}</div><div class="k">Kích hoạt web lỗi</div></div>
<div class="stat"><div class="v">{c['unused']}</div><div class="k">CDK còn lại</div></div>
<div class="stat"><div class="v">{c['used']}/{c['total']}</div><div class="k">CDK đã dùng</div></div>
<div class="stat"><div class="v">{s['unique_users']}</div><div class="k">User Telegram</div></div>
</div>
<h2 style="font-size:17px;margin-bottom:12px">Kích hoạt web gần đây</h2>
<div class="card" style="padding:6px;overflow-x:auto;margin-bottom:22px">
<table class="table"><tr><th>ID</th><th>Username</th><th>UID</th><th>Trạng thái</th><th>Tạo lúc</th></tr>{act_rows}</table>
</div>
<h2 style="font-size:17px;margin-bottom:12px">Đơn gần đây</h2>
<div class="card" style="padding:6px;overflow-x:auto">
<table class="table"><tr><th>ID</th><th>SL</th><th>Tiền</th><th>Trạng thái</th><th>Tạo lúc</th><th>Hoàn tất</th></tr>{rows}</table>
</div>"""
    return admin_page("Tổng quan", body, "dash")


def admin_orders():
    orders = db.list_cdk_orders(limit=100)
    rows = "".join(
        f"<tr><td>#{r['id']}</td><td class='mono'>{html.escape(str(r['payment_content'] or '—'))}</td>"
        f"<td>{r['quantity']}</td><td>{format_vnd(r['total_price'])}</td>"
        f"<td>{_status_tag(r['status'])}</td><td>{_fmt_dt(r['created_at'])}</td>"
        f"<td>{_fmt_dt(r['completed_at'])}</td><td class='mono'>{html.escape(str(r['transaction_id'] or '—'))}</td></tr>"
        for r in orders
    )
    body = f"""<h1>Đơn hàng</h1><div class="sub">100 đơn mới nhất (bao gồm cả đơn từ bot Telegram)</div>
<div class="card" style="padding:6px;overflow-x:auto">
<table class="table"><tr><th>ID</th><th>Nội dung CK</th><th>SL</th><th>Tiền</th><th>Trạng thái</th><th>Tạo lúc</th><th>Hoàn tất</th><th>Giao dịch</th></tr>{rows}</table>
</div>"""
    return admin_page("Đơn hàng", body, "orders")


def admin_cdks(csrf=""):
    codes = db.list_cdk_codes(limit=100, secret=CDK_SECRET)
    rows = "".join(
        f"<tr><td class='mono'>{html.escape(str(r.get('code') or '—'))}</td>"
        f"<td>{'<span class=\'tag valid\'>Chưa dùng</span>' if not r['used'] else '<span class=\'tag used\'>Đã dùng</span>'}</td>"
        f"<td>{r['source'] or 'admin'}</td><td>{r['used_by'] if r['used_by'] is not None else '—'}</td>"
        f"<td>{_fmt_dt(r['created_at'] if r.get('created_at') else None)}</td>"
        f"<td>{html.escape(str(r['used_at'] or '—'))}</td>"
        f"<td>{('#' + str(r['order_id'])) if r['order_id'] else '—'}</td></tr>"
        for r in codes
    )
    body = f"""<h1>CDK</h1><div class="sub">Tạo mã mới hoặc xem 100 mã gần nhất (đã giải mã)</div>
<form class="admin-form" id="gen-form">
<div class="field"><label>Số lượng CDK (1–500)</label>
<input type="number" name="count" min="1" max="500" value="10" required></div>
<button class="btn btn-gold" type="submit">🎟️ Tạo CDK</button>
</form>
<div class="gen-result" id="gen-result"></div>
<div class="card" style="padding:6px;overflow-x:auto">
<table class="table"><tr><th>Mã CDK</th><th>Trạng thái</th><th>Nguồn</th><th>Dùng bởi</th><th>Tạo lúc</th><th>Dùng lúc</th><th>Đơn</th></tr>{rows}</table>
</div>
<script>
const CSRF_TOKEN='{csrf}';
document.getElementById('gen-form').addEventListener('submit',async function(e){{
  e.preventDefault();
  const count=this.elements['count'].value;
  const r=await fetch('/admin/cdks/generate',{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF_TOKEN}},body:JSON.stringify({{count:parseInt(count)}})}});
  const d=await r.json();
  const box=document.getElementById('gen-result');
  if(!r.ok||!d.ok){{box.style.display='block';box.style.borderColor='rgba(255,107,107,.4)';box.textContent='⚠️ '+(d.error||'Lỗi');return;}}
  box.style.display='block';box.style.borderColor='rgba(61,220,151,.3)';
  box.innerHTML='✅ Đã tạo '+d.codes.length+' CDK:<br>'+d.codes.join('\\n');
  setTimeout(function(){{location.reload();}},2500);
}});
</script>"""
    return admin_page("CDK", body, "cdks")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def index(request):
    return web.Response(text=landing_page(), content_type="text/html")


async def verify_page_handler(request):
    return web.Response(text=verify_page(), content_type="text/html")


async def api_create_order(request):
    if payment_config_errors() or CDK_UNIT_PRICE <= 0:
        return web.json_response({"ok": False, "error": "Cửa hàng tạm đóng. Liên hệ admin."}, status=503)
    try:
        data = await request.json()
        quantity = int(data.get("quantity", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({"ok": False, "error": "Số lượng không hợp lệ."}, status=400)
    if not 1 <= quantity <= MAX_QUANTITY:
        return web.json_response({"ok": False, "error": f"Chọn số lượng từ 1-{MAX_QUANTITY}."}, status=400)

    visitor = _visitor_id(request)
    total = CDK_UNIT_PRICE * quantity
    content = _gen_payment_content()
    expires_at = _now_ts() + CDK_ORDER_TIMEOUT_MINUTES * 60
    try:
        order = db.create_cdk_order(
            user_id=visitor,
            chat_id=None,
            quantity=quantity,
            total_price=total,
            payment_content=content,
            expires_at=expires_at,
        )
    except Exception as exc:
        logger.error("Create web order failed: %s", exc)
        return web.json_response({"ok": False, "error": "Không tạo được đơn. Thử lại sau."}, status=500)

    # Trigger an immediate (idempotent) payment check so a previously paid
    # order that the poller has not seen yet is completed instantly.
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
    status = order["status"]
    payload = {"status": status, "id": order["id"], "quantity": order["quantity"]}
    activation = db.get_web_activation(order_id=order_id)
    if activation:
        payload["activation"] = {
            "id": activation["id"],
            "status": activation["status"],
            "progress": activation["progress"],
            "result": activation["result"],
            "dns_link": activation["dns_link"],
            "username": activation["username"],
            "uid": activation["uid"],
            "avatar": activation["avatar"],
        }
    if status == "pending":
        if order["expires_at"] and order["expires_at"] <= _now_ts():
            db.expire_cdk_orders()
            return web.json_response({"status": "expired", "id": order["id"]})
        # Idempotent on-demand check (rate-limited per order).
        last = _payment_checks.get(order_id)
        if last is None or _now_ts() - last >= SEPAY_POLL_INTERVAL_SECONDS:
            codes = await _complete_web_order(order_id)
            if codes:
                status = "completed"
                payload["codes"] = codes
    elif status == "completed":
        codes = db.complete_cdk_order(order_id=order_id, secret=CDK_SECRET)
        payload["codes"] = codes or []
        activation = db.get_web_activation(order_id=order_id)
        if activation:
            payload["activation"] = {
                "id": activation["id"],
                "status": activation["status"],
                "progress": activation["progress"],
                "result": activation["result"],
                "dns_link": activation["dns_link"],
                "username": activation["username"],
                "uid": activation["uid"],
                "avatar": activation["avatar"],
            }
    payload["status"] = status
    return web.json_response(payload)


async def api_verify(request):
    try:
        data = await request.json()
        code = str(data.get("code", "")).strip()
    except (ValueError, TypeError, json.JSONDecodeError):
        code = ""
    if not code:
        return web.json_response({"status": "empty"})
    detail = db.get_cdk_detail(code, secret=CDK_SECRET)
    return web.json_response(detail)


def _parse_locket_username(text):
    """Accept a bare username or a full locket.cam link."""
    text = text.strip()
    if "locket.cam/" in text:
        return text.split("locket.cam/")[-1].split("?")[0]
    return text


def _normalize_avatar(url):
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://locket.cam" + url
    return url


async def api_check(request):
    """Resolve username/link → uid, avatar, Gold status, paid-once flag."""
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
    activation = db.get_web_activation(uid=uid)
    return web.json_response({
        "ok": True,
        "uid": uid,
        "username": username,
        "avatar": _normalize_avatar(profile.get("avatar")),
        "gold_active": gold_active,
        "expires": (status or {}).get("expires"),
        "paid": db.is_uid_paid(uid),
        "activation": {
            "id": activation["id"],
            "status": activation["status"],
            "cdk_code": activation["cdk_code"],
            "dns_link": activation["dns_link"],
            "result": activation["result"],
        } if activation else None,
    })


async def api_activate(request):
    """Start activation: free re-activation for paid UIDs, else a paid order."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "Yêu cầu không hợp lệ."}, status=400)
    uid = str(data.get("uid", "")).strip()
    username = str(data.get("username", "")).strip() or uid
    avatar = str(data.get("avatar", "")).strip() or None
    if not uid:
        return web.json_response({"ok": False, "error": "Thiếu UID tài khoản."}, status=400)

    visitor = _visitor_id(request)

    if db.is_uid_paid(uid):
        # Already bought once — re-activation is free, no new order.
        activation_id = db.create_web_activation(
            None, visitor, uid, username, avatar=avatar, status="queued",
        )
        response = web.json_response({"ok": True, "free": True, "activation_id": activation_id})
        response.set_cookie(VISITOR_COOKIE, str(visitor), max_age=365 * 24 * 3600, httponly=True, samesite="lax")
        return response

    if payment_config_errors() or CDK_UNIT_PRICE <= 0:
        return web.json_response({"ok": False, "error": "Cửa hàng tạm đóng. Liên hệ admin."}, status=503)

    order = db.create_cdk_order(
        user_id=visitor,
        chat_id=None,
        quantity=1,
        total_price=CDK_UNIT_PRICE,
        payment_content=_gen_payment_content(),
        expires_at=_now_ts() + CDK_ORDER_TIMEOUT_MINUTES * 60,
    )
    activation = db.get_web_activation(order_id=order["id"])
    if activation:
        activation_id = activation["id"]
    else:
        activation_id = db.create_web_activation(
            order["id"], visitor, uid, username, avatar=avatar, status="awaiting_payment",
        )

    # Trigger an immediate (idempotent) payment check.
    asyncio.create_task(_complete_web_order(order["id"]))

    response = web.json_response({"ok": True, "free": False, "order_id": order["id"], "activation_id": activation_id})
    response.set_cookie(VISITOR_COOKIE, str(visitor), max_age=365 * 24 * 3600, httponly=True, samesite="lax")
    return response


async def api_activate_cdk(request):
    """Activate Gold using a user-supplied CDK instead of buying a new one."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "Yêu cầu không hợp lệ."}, status=400)
    uid = str(data.get("uid", "")).strip()
    username = str(data.get("username", "")).strip() or uid
    avatar = str(data.get("avatar", "")).strip() or None
    code = str(data.get("code", "")).strip().upper()
    if not uid:
        return web.json_response({"ok": False, "error": "Thiếu UID tài khoản."}, status=400)
    if not code:
        return web.json_response({"ok": False, "error": "Nhập mã CDK của bạn."}, status=400)

    visitor = _visitor_id(request)

    if not db.reserve_cdk(code, visitor, secret=CDK_SECRET, ttl_seconds=CDK_RESERVATION_TTL_SECONDS):
        return web.json_response({"ok": False, "error": "CDK không hợp lệ hoặc đã được sử dụng."}, status=400)

    db.mark_uid_paid(uid)
    activation_id = db.create_web_activation(
        None, visitor, uid, username, avatar=avatar, status="queued",
    )
    db.update_web_activation(activation_id, cdk_code=code)

    response = web.json_response({"ok": True, "free": True, "activation_id": activation_id})
    response.set_cookie(VISITOR_COOKIE, str(visitor), max_age=365 * 24 * 3600, httponly=True, samesite="lax")
    return response


async def api_start_order_activation(request):
    """Flip a paid web activation from 'paid' to 'queued' — starts the bot worker."""
    try:
        order_id = int(request.match_info["order_id"])
    except ValueError:
        return web.json_response({"ok": False, "error": "Đơn hàng không hợp lệ."}, status=400)
    activation = db.get_web_activation(order_id=order_id)
    if not activation:
        return web.json_response({"ok": False, "error": "Không tìm thấy kích hoạt."}, status=404)
    status = activation["status"]
    if status in ("paid", "awaiting_payment", "queued", "processing"):
        if status in ("paid", "awaiting_payment"):
            db.update_web_activation(activation["id"], status="queued")
        return web.json_response({"ok": True, "activation_id": activation["id"], "status": "queued"})
    return web.json_response({"ok": True, "activation_id": activation["id"], "status": status})


async def activation_page_handler(request):
    try:
        activation_id = int(request.match_info["activation_id"])
    except ValueError:
        raise web.HTTPNotFound()
    activation = db.get_web_activation(id=activation_id)
    if not activation:
        raise web.HTTPNotFound()
    return web.Response(text=activation_page(activation), content_type="text/html")


async def api_activation_status(request):
    try:
        activation_id = int(request.match_info["activation_id"])
    except ValueError:
        return web.json_response({"status": "not_found"})
    activation = db.get_web_activation(id=activation_id)
    if not activation:
        return web.json_response({"status": "not_found"})
    return web.json_response({
        "status": activation["status"],
        "progress": activation["progress"],
        "result": activation["result"],
        "dns_link": activation["dns_link"],
        "username": activation["username"],
        "uid": activation["uid"],
        "avatar": activation["avatar"],
        "order_id": activation["order_id"],
    })


async def admin_login_page(request):
    if _is_admin(request):
        raise web.HTTPFound("/admin")
    nonce = secrets.token_hex(16)
    token = hmac.new(WEB_SESSION_SECRET.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()
    response = web.Response(
        text=login_page(csrf=token),
        content_type="text/html",
    )
    response.set_cookie(
        CSRF_COOKIE,
        nonce,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(request),
    )
    return response


async def admin_login(request):
    if _is_admin(request):
        raise web.HTTPFound("/admin")
    ip = _client_ip(request)
    csrf = request.cookies.get(CSRF_COOKIE, "")
    if not WEB_ADMIN_PASSWORD_HASH and not WEB_ADMIN_PASSWORD:
        return web.Response(text=login_page("Tài khoản admin chưa được cấu hình.", csrf=csrf), content_type="text/html")
    if not _login_allowed(ip):
        return web.Response(
            text=login_page("Quá nhiều lần thử sai. Vui lòng đợi 15 phút.", csrf=csrf),
            content_type="text/html",
        )
    data = await request.post()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    posted_csrf = data.get("csrf", "")
    if not _check_csrf(request, posted_csrf):
        return web.Response(
            text=login_page("Phiên đăng nhập không hợp lệ. Tải lại trang và thử lại.", csrf=csrf),
            content_type="text/html",
        )
    if username == WEB_ADMIN_USER and _verify_admin_password(password):
        _login_attempts.pop(ip, None)
        response = web.HTTPFound("/admin")
        response.set_cookie(
            SESSION_COOKIE,
            _make_session_token(username),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=_secure_cookie(request),
        )
        return response
    _record_login_fail(ip)
    return web.Response(text=login_page("Sai tên đăng nhập hoặc mật khẩu.", csrf=csrf), content_type="text/html")


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
async def admin_cdks_handler(request):
    csrf = _csrf_token(request) or ""
    return web.Response(text=admin_cdks(csrf=csrf), content_type="text/html")


@_require_admin
async def admin_generate_cdk(request):
    csrf = request.headers.get("X-CSRF-Token", "")
    if not _check_csrf(request, csrf):
        return web.json_response({"ok": False, "error": "Phiên không hợp lệ. Tải lại trang."}, status=403)
    try:
        data = await request.json()
        count = int(data.get("count", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({"ok": False, "error": "Số lượng không hợp lệ."}, status=400)
    if not 1 <= count <= 500:
        return web.json_response({"ok": False, "error": "Số lượng từ 1-500."}, status=400)
    try:
        codes = db.gen_cdk(count, admin_id=ADMIN_ID or 0, cdk_secret=CDK_SECRET, source="admin")
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)
    return web.json_response({"ok": True, "codes": codes})


# ---------------------------------------------------------------------------
# App / entry
# ---------------------------------------------------------------------------

def build_app():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/verify", verify_page_handler)
    app.router.add_post("/api/order", api_create_order)
    app.router.add_post("/api/verify", api_verify)
    app.router.add_post("/api/check", api_check)
    app.router.add_post("/api/activate", api_activate)
    app.router.add_post("/api/activate-cdk", api_activate_cdk)
    app.router.add_get("/order/{order_id}", order_page_handler)
    app.router.add_get("/api/order/{order_id}", api_order_status)
    app.router.add_post("/api/order/{order_id}/start", api_start_order_activation)
    app.router.add_get("/activate/{activation_id}", activation_page_handler)
    app.router.add_get("/api/activation/{activation_id}", api_activation_status)
    app.router.add_get("/admin/login", admin_login_page)
    app.router.add_post("/admin/login", admin_login)
    app.router.add_get("/admin/logout", admin_logout)
    app.router.add_get("/admin", admin_dashboard_handler)
    app.router.add_get("/admin/orders", admin_orders_handler)
    app.router.add_get("/admin/cdks", admin_cdks_handler)
    app.router.add_post("/admin/cdks/generate", admin_generate_cdk)
    app.router.add_static("/static/dns", os.path.join(BASE_DIR, "stepdns"))

    async def _start_poller(_app):
        asyncio.create_task(web_payment_poller())

    app.on_startup.append(_start_poller)
    return app


async def main():
    db.init_db()
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    logger.info("Web store running at http://%s:%s", WEB_HOST, WEB_PORT)
    if payment_config_errors():
        logger.warning("Payment config incomplete — buying disabled: %s", ", ".join(payment_config_errors()))
    if not WEB_ADMIN_PASSWORD:
        logger.warning("WEB_ADMIN_PASSWORD is empty — admin panel disabled.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
