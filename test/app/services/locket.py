import re
import urllib.request
import urllib.parse
import urllib.error
import json
import asyncio
import time
import threading
from collections import OrderedDict
from email.utils import parsedate_to_datetime
from app.config import token as TOKEN_LIST

# Headers giả lập Locket iOS Client
HEADERS = {
    "User-Agent": "Locket/3 CFNetwork/3860.300.31 Darwin/25.2.0",
    "Content-Type": "application/json",
    "Accept": "*/*",
    "X-Platform": "iOS",
    "X-Platform-Version": "Version 26.2 (Build 23C55)",
    "X-Platform-Device": "iPhone15,3",
    "X-Client-Bundle-ID": "com.locket.Locket",
}

MAX_CONCURRENT_REQUESTS = 2
_request_slots = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
REQUEST_INTERVAL = 2.0
REFUSAL_COOLDOWN = 60.0
UID_CACHE_TTL = 3600.0
UID_CACHE_LIMIT = 4096
_network_lock = threading.Lock()
_next_request_at = 0.0
_blocked_until = 0.0
_uid_cache = OrderedDict()
_uid_pending = {}


def _retry_delay(value):
    try:
        return max(REFUSAL_COOLDOWN, float(value))
    except (TypeError, ValueError):
        try:
            return max(REFUSAL_COOLDOWN, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return REFUSAL_COOLDOWN


def is_valid_proxy_url(proxy_url: str) -> bool:
    if not proxy_url:
        return False
    try:
        parsed = urllib.parse.urlparse(proxy_url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def _transport_open(req, timeout: int, proxy_url: str = None):
    if not proxy_url:
        return urllib.request.urlopen(req, timeout=timeout)

    proxy_handler = urllib.request.ProxyHandler({
        "http": proxy_url,
        "https": proxy_url,
    })
    opener = urllib.request.build_opener(proxy_handler)
    return opener.open(req, timeout=timeout)


def _open_request(req, timeout: int, proxy_url: str = None):
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
    if not TOKEN_LIST:
        return None
    t = TOKEN_LIST[0].strip()
    return t if t.startswith("Bearer ") else f"Bearer {t}"


async def resolve_uid(username_or_url: str, proxy_url: str = None) -> str:
    raw = (username_or_url or "").strip()
    if "links/" in raw.lower():
        code = raw.split("links/")[-1].split("?", 1)[0].strip("/")
        raw = f"links/{code}"
    elif "locket.camera/invites/" in raw:
        raw = raw.split("locket.camera/invites/", 1)[1].split("?", 1)[0].strip("/")
    elif "locket.cam/invites/" in raw:
        raw = raw.split("locket.cam/invites/", 1)[1].split("?", 1)[0].strip("/")
    elif "locket.cam/" in raw:
        raw = raw.split("locket.cam/", 1)[1].split("?", 1)[0].strip("/")
    elif "locket.camera/" in raw:
        raw = raw.split("locket.camera/", 1)[1].split("?", 1)[0].strip("/")
    raw = raw.lstrip("@")
    # Keep proxy routes separate and never cache failures.
    key = (raw, proxy_url)
    cached = _uid_cache.get(key)
    if cached and cached[1] > time.monotonic():
        _uid_cache.move_to_end(key)
        return cached[0]
    _uid_cache.pop(key, None)
    task = _uid_pending.get(key)
    if task is None:
        async def lookup():
            try:
                uid = await _resolve_uid(raw, proxy_url)
                if uid and re.fullmatch(r"[a-zA-Z0-9_-]{28}", uid):
                    _uid_cache[key] = (uid, time.monotonic() + UID_CACHE_TTL)
                    while len(_uid_cache) > UID_CACHE_LIMIT:
                        _uid_cache.popitem(last=False)
                return uid
            finally:
                _uid_pending.pop(key, None)
        task = asyncio.create_task(lookup())
        _uid_pending[key] = task
    return await asyncio.shield(task)


async def _resolve_uid(username_or_url: str, proxy_url: str = None) -> str:
    """
    Phân giải username hoặc link Locket thành Firebase UID (28 ký tự).
    Hỗ trợ:
    - Username: pdlinhh, toiii
    - Link hồ sơ: https://locket.cam/toiii
    - Link lời mời: https://locket.camera/invites/pdlinhh
    - Link động (Dynamic Links): https://locket.camera/links/oTuuThx5GxDxunHRA
    - UID trực tiếp: 28 ký tự (Firebase UID)
    """
    if not username_or_url:
        return None
    if proxy_url and not is_valid_proxy_url(proxy_url):
        return "PROXY_ERROR"

    # Làm sạch input
    raw = username_or_url.strip()
    raw = re.sub(r'^@+', '', raw)

    # 1. Nếu là Dynamic link (locket.camera/links/... hoặc locket.cam/links/...)
    if "links/" in raw.lower():
        code = raw.split("links/")[-1].split("?")[0].strip("/")
        targets = [
            f"https://locket.camera/links/{code}",
            f"https://locket.cam/links/{code}",
        ]
    else:
        if "locket.camera/invites/" in raw:
            raw = raw.split("locket.camera/invites/")[-1].split("?")[0].strip("/")
        elif "locket.cam/invites/" in raw:
            raw = raw.split("locket.cam/invites/")[-1].split("?")[0].strip("/")
        elif "locket.cam/" in raw:
            raw = raw.split("locket.cam/")[-1].split("?")[0].strip("/")
        elif "locket.camera/" in raw:
            raw = raw.split("locket.camera/")[-1].split("?")[0].strip("/")

        # 2. Nếu đã là UID 28 ký tự (hoặc 44 ký tự có prefix)
        if re.match(r'^[a-zA-Z0-9_-]{28}$', raw):
            return raw

        targets = [
            f"https://locket.cam/{raw}",
            f"https://locket.camera/invites/{raw}",
        ]

    def _fetch():
        user_agents = [
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        ]

        for url in targets:
            for ua in user_agents:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": ua,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                    }
                )
                try:
                    with _open_request(req, timeout=8, proxy_url=proxy_url) as resp:
                        html = resp.read().decode("utf-8", errors="ignore")
                        unquoted = urllib.parse.unquote(html)
                        
                        # Match UID từ users/<UID>, invites/<UID>, links/<UID>
                        match = re.search(r'(?:invites|users|links)/([a-zA-Z0-9_-]{28})', unquoted)
                        if match:
                            return match.group(1)

                        # Match UID dự phòng dạng JSON hoặc query param
                        match_json = re.search(r'["\'](?:uid|user_id|userId)["\']\s*:\s*["\']([a-zA-Z0-9_-]{28})["\']', unquoted)
                        if match_json:
                            return match_json.group(1)
                except urllib.error.HTTPError as e:
                    if e.code in (403, 429):
                        return "IP_BLOCKED"
                    elif e.code == 407:
                        return "PROXY_ERROR"
                    elif e.code == 404:
                        return None
                except urllib.error.URLError:
                    if proxy_url:
                        return "PROXY_ERROR"
                except Exception:
                    if proxy_url:
                        return "PROXY_ERROR"

        return None

    async with _request_slots:
        return await asyncio.to_thread(_fetch)


async def check_status(uid: str, proxy_url: str = None) -> dict:
    """
    Kiểm tra trạng thái subscription Locket Gold qua RevenueCat.
    """
    auth = get_auth_token()
    if not auth or not uid:
        return {"active": False, "expires": "Unknown"}
    if proxy_url and not is_valid_proxy_url(proxy_url):
        return {"active": False, "expires": "Unknown", "error": "PROXY_ERROR"}

    def _check():
        url = f"https://api.revenuecat.com/v1/subscribers/{uid}"
        headers = {**HEADERS, "Authorization": auth}
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with _open_request(req, timeout=10, proxy_url=proxy_url) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                subscriber = data.get("subscriber", {})
                entitlements = subscriber.get("entitlements", {})
                
                # Kiểm tra gói Gold
                gold = entitlements.get("Gold") or entitlements.get("gold") or {}
                expires_date = gold.get("expires_date", "")
                if expires_date:
                    # Format: 2027-08-16 10:15:31
                    formatted_exp = expires_date.replace("T", " ").split(".")[0].replace("Z", "")
                    return {
                        "active": True,
                        "expires": formatted_exp
                    }
                return {"active": False, "expires": "Unknown"}
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                return {"active": False, "expires": "Unknown", "error": "IP_BLOCKED"}
            if e.code == 407:
                return {"active": False, "expires": "Unknown", "error": "PROXY_ERROR"}
            return {"active": False, "expires": "Unknown", "error": f"HTTP_{e.code}"}
        except urllib.error.URLError:
            error = "PROXY_ERROR" if proxy_url else "NETWORK_ERROR"
            return {"active": False, "expires": "Unknown", "error": error}
        except Exception:
            error = "PROXY_ERROR" if proxy_url else "NETWORK_ERROR"
            return {"active": False, "expires": "Unknown", "error": error}

    async with _request_slots:
        return await asyncio.to_thread(_check)


async def aliasSubscriber(source_uid: str, dest_uid: str):
    """
    Gán gói Gold từ tài khoản nguồn sang tài khoản đích qua RevenueCat Alias.
    """
    auth = get_auth_token()
    if not auth:
        return False, "Chưa cấu hình RevenueCat Token trong app/config.py"

    def _alias():
        url = f"https://api.revenuecat.com/v1/subscribers/{source_uid}/alias"
        headers = {**HEADERS, "Authorization": auth}
        payload = json.dumps({"new_app_user_id": dest_uid}).encode("utf-8")
        
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with _open_request(req, timeout=10) as resp:
                if resp.status in (200, 201):
                    return True, "Success"
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                return False, "IP_BLOCKED"
            err_body = e.read().decode("utf-8", errors="ignore")
            try:
                err_json = json.loads(err_body)
                msg = err_json.get("message", err_body)
                return False, msg
            except Exception:
                return False, f"HTTP {e.code}: {err_body}"
        except Exception as e:
            return False, str(e)

    async with _request_slots:
        return await asyncio.to_thread(_alias)
