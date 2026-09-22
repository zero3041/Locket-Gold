"""Locket Gold engine — page resolution, RevenueCat status and alias activation.

The activation model is alias-based: a source account that already owns Gold is
aliased to the destination UID, which transfers the entitlement. All network
calls share a global cooldown so the RevenueCat / Locket endpoints do not flag
the server IP (403/429 responses trigger a cool-down window).
"""

import asyncio
import html as _html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime
from email.utils import parsedate_to_datetime

from app.config import REVENUECAT_APP_KEY

HEADERS = {
    "User-Agent": "Locket/3 CFNetwork/3860.300.31 Darwin/25.2.0",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "X-Platform": "iOS",
    "X-Platform-Version": "Version 26.2 (Build 23C55)",
    "X-Platform-Device": "iPhone15,3",
    "X-Client-Bundle-ID": "com.locket.Locket",
}

UID_RE = re.compile(r"[a-zA-Z0-9_-]{28}")

MAX_CONCURRENT_REQUESTS = 2
REQUEST_INTERVAL = 2.0
REFUSAL_COOLDOWN = 60.0
UID_CACHE_TTL = 3600.0
UID_CACHE_LIMIT = 4096

_network_lock = threading.Lock()
_thread_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
_next_request_at = 0.0
_blocked_until = 0.0
_profile_cache = OrderedDict()
_profile_pending = {}


class Clr:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"


def _retry_delay(value):
    try:
        return max(REFUSAL_COOLDOWN, float(value))
    except (TypeError, ValueError):
        try:
            return max(REFUSAL_COOLDOWN, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return REFUSAL_COOLDOWN


def is_blocked():
    """True while the shared IP is in the 403/429 cool-down window."""
    with _network_lock:
        return time.monotonic() < _blocked_until


def blocked_remaining():
    with _network_lock:
        return max(0.0, _blocked_until - time.monotonic())


def is_valid_proxy_url(proxy_url):
    if not proxy_url:
        return False
    try:
        parsed = urllib.parse.urlparse(proxy_url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def _transport_open(req, timeout, proxy_url=None):
    if not proxy_url:
        return urllib.request.urlopen(req, timeout=timeout)
    proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(proxy_handler)
    return opener.open(req, timeout=timeout)


def _open_request(req, timeout, proxy_url=None):
    global _next_request_at, _blocked_until
    # Shared across direct and proxy traffic; queued requests cannot bypass cooldown.
    with _network_lock:
        if time.monotonic() < _blocked_until:
            raise urllib.error.HTTPError(req.full_url, 403, "Local cooldown", {}, None)
        delay = _next_request_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _next_request_at = time.monotonic() + REQUEST_INTERVAL
        try:
            return _transport_open(req, timeout, proxy_url)
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                retry_after = error.headers.get("Retry-After") if error.headers else None
                _blocked_until = time.monotonic() + _retry_delay(retry_after)
            raise


def get_auth_token():
    token = (REVENUECAT_APP_KEY or "").strip()
    if not token:
        return None
    return token if token.startswith("Bearer ") else f"Bearer {token}"


def _normalize_target(username_or_url):
    raw = (username_or_url or "").strip()
    if not raw:
        return ""
    if "links/" in raw.lower():
        code = raw.split("links/")[-1].split("?", 1)[0].strip("/")
        return f"links/{code}"
    for marker in ("locket.camera/invites/", "locket.cam/invites/", "locket.camera/", "locket.cam/"):
        if marker in raw:
            raw = raw.split(marker, 1)[1]
            break
    return raw.split("?", 1)[0].strip().strip("/").lstrip("@")


def _extract_avatar(page):
    match = re.search(r'class="profile-pic-img"\s+src=([^\s>]+)', page)
    if not match:
        return None
    return _html.unescape(match.group(1).strip('"\''))


def resolve_profile_sync(username_or_url, proxy_url=None, timeout=8):
    """Blocking profile lookup. Returns dict, None, or an error sentinel string."""
    if not username_or_url:
        return None
    if proxy_url and not is_valid_proxy_url(proxy_url):
        return "PROXY_ERROR"

    raw = _normalize_target(username_or_url)
    if not raw:
        return None
    if UID_RE.fullmatch(raw):
        return {"uid": raw, "avatar": None}

    if raw.startswith("links/"):
        code = raw.split("/", 1)[1]
        targets = [
            f"https://locket.camera/links/{code}",
            f"https://locket.cam/links/{code}",
        ]
    else:
        targets = [
            f"https://locket.cam/{raw}",
            f"https://locket.camera/invites/{raw}",
        ]

    user_agents = [
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]

    with _thread_slots:
        for url in targets:
            for agent in user_agents:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": agent,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                )
                try:
                    with _open_request(req, timeout=timeout, proxy_url=proxy_url) as resp:
                        page = resp.read().decode("utf-8", errors="ignore")
                        unquoted = urllib.parse.unquote(page)
                        match = re.search(r"(?:invites|users|links)/([a-zA-Z0-9_-]{28})", unquoted)
                        uid = match.group(1) if match else None
                        if not uid:
                            match = re.search(
                                r'["\'](?:uid|user_id|userId)["\']\s*:\s*["\']([a-zA-Z0-9_-]{28})["\']',
                                unquoted,
                            )
                            uid = match.group(1) if match else None
                        if uid:
                            return {"uid": uid, "avatar": _extract_avatar(page)}
                except urllib.error.HTTPError as error:
                    if error.code in (403, 429):
                        return "IP_BLOCKED"
                    if error.code == 407:
                        return "PROXY_ERROR"
                    continue
                except urllib.error.URLError:
                    if proxy_url:
                        return "PROXY_ERROR"
                    continue
                except Exception:
                    if proxy_url:
                        return "PROXY_ERROR"
                    continue
    return None


async def _profile_lookup(raw_key, target, proxy_url):
    try:
        return await asyncio.to_thread(resolve_profile_sync, target, proxy_url)
    finally:
        _profile_pending.pop(raw_key, None)


async def fetch_profile(username_or_url, proxy_url=None):
    """Cached async profile lookup.

    Returns {"uid","avatar"} / None / "IP_BLOCKED" / "PROXY_ERROR".
    """
    raw = _normalize_target(username_or_url)
    if not raw:
        return None
    if proxy_url and not is_valid_proxy_url(proxy_url):
        return "PROXY_ERROR"
    key = (raw, proxy_url)
    cached = _profile_cache.get(key)
    if cached and cached[1] > time.monotonic():
        _profile_cache.move_to_end(key)
        return cached[0]
    _profile_cache.pop(key, None)
    task = _profile_pending.get(key)
    if task is None:
        task = asyncio.create_task(_profile_lookup(key, raw, proxy_url))
        _profile_pending[key] = task
    result = await asyncio.shield(task)
    if isinstance(result, dict) and result.get("uid"):
        _profile_cache[key] = (result, time.monotonic() + UID_CACHE_TTL)
        while len(_profile_cache) > UID_CACHE_LIMIT:
            _profile_cache.popitem(last=False)
    return result


async def resolve_uid(username_or_url, proxy_url=None):
    """Resolve to a 28-char UID. Returns None or an error sentinel."""
    result = await fetch_profile(username_or_url, proxy_url=proxy_url)
    if isinstance(result, dict):
        return result.get("uid")
    return result


async def resolve_profile(username_or_url, proxy_url=None):
    """Backward-compatible wrapper used by the bot and web store."""
    result = await fetch_profile(username_or_url, proxy_url=proxy_url)
    return result if isinstance(result, dict) else None


def _status_sync(uid, proxy_url=None):
    auth = get_auth_token()
    if not uid:
        return {"active": False, "expires": "Unknown", "error": "NOT_FOUND"}
    if not auth:
        return {"active": False, "expires": "Unknown", "error": "NO_TOKEN"}

    url = f"https://api.revenuecat.com/v1/subscribers/{uid}"
    headers = {**HEADERS, "Authorization": auth}
    req = urllib.request.Request(url, headers=headers, method="GET")
    with _thread_slots:
        try:
            with _open_request(req, timeout=10, proxy_url=proxy_url) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                subscriber = data.get("subscriber", {})
                entitlements = subscriber.get("entitlements", {})
                gold = entitlements.get("Gold") or entitlements.get("gold") or {}
                expires_date = gold.get("expires_date", "")
                if expires_date:
                    formatted = expires_date.replace("T", " ").split(".")[0].replace("Z", "")
                    return {"active": True, "expires": formatted}
                return {"active": False, "expires": "Unknown"}
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                return {"active": False, "expires": "Unknown", "error": "IP_BLOCKED"}
            if error.code == 407:
                return {"active": False, "expires": "Unknown", "error": "PROXY_ERROR"}
            return {"active": False, "expires": "Unknown", "error": f"HTTP_{error.code}"}
        except urllib.error.URLError:
            error = "PROXY_ERROR" if proxy_url else "NETWORK_ERROR"
            return {"active": False, "expires": "Unknown", "error": error}
        except Exception:
            error = "PROXY_ERROR" if proxy_url else "NETWORK_ERROR"
            return {"active": False, "expires": "Unknown", "error": error}


async def check_status(uid, proxy_url=None):
    """RevenueCat entitlement check. Returns {active, expires} (+error)."""
    return await asyncio.to_thread(_status_sync, uid, proxy_url)


def _alias_sync(source_uid, dest_uid, proxy_url=None):
    auth = get_auth_token()
    if not auth:
        return False, "NO_TOKEN"

    url = f"https://api.revenuecat.com/v1/subscribers/{source_uid}/alias"
    headers = {**HEADERS, "Authorization": auth}
    payload = json.dumps({"new_app_user_id": dest_uid}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with _thread_slots:
        try:
            with _open_request(req, timeout=12, proxy_url=proxy_url) as resp:
                if resp.status in (200, 201):
                    return True, "SUCCESS"
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                return False, "IP_BLOCKED"
            body = error.read().decode("utf-8", errors="ignore")
            try:
                message = json.loads(body).get("message", body)
            except Exception:
                message = f"HTTP {error.code}: {body}"
            return False, message
        except Exception as exc:
            return False, str(exc)


async def alias_subscriber(source_uid, dest_uid, proxy_url=None):
    """Alias a source subscriber onto the destination UID (transfers Gold)."""
    if not source_uid or not dest_uid:
        return False, "INVALID_INPUT"
    return await asyncio.to_thread(_alias_sync, source_uid, dest_uid, proxy_url)


def plan_days_left(expires_text):
    """Days remaining from a RevenueCat expiry string ('YYYY-mm-dd HH:MM:SS')."""
    if not expires_text or expires_text in ("Unknown", "N/A"):
        return 0
    stamp = re.search(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", str(expires_text))
    if not stamp:
        return 0
    try:
        expiry = datetime.strptime(stamp.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return max(0, (expiry - datetime.now()).days)
