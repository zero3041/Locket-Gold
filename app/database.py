import sqlite3
import time
import base64
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime

DB_NAME = "bot_data.db"

CDK_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CDK_SECRET_ENV = "CDK_SECRET"

def _now_ts():
    return int(time.time())

def _now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _parse_ts(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(datetime.strptime(stripped, fmt).timestamp())
            except ValueError:
                continue
    raise ValueError("timestamp must be an integer epoch or YYYY-mm-dd[ HH:MM:SS]")

def _connect():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    if DB_NAME != ":memory:":
        try:
            os.chmod(DB_NAME, 0o600)
        except OSError:
            pass
    return conn

def _row_to_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}

def _resolve_cdk_secret(secret=None, cdk_secret=None):
    resolved = cdk_secret or secret or os.environ.get(CDK_SECRET_ENV)
    if not resolved:
        raise ValueError(f"{CDK_SECRET_ENV} is required for secure CDK generation")
    if not isinstance(resolved, str):
        resolved = str(resolved)
    if len(resolved) < 32:
        raise ValueError(f"{CDK_SECRET_ENV} must be at least 32 characters")
    return resolved.encode("utf-8")

def _base32_digest(data):
    return base64.b32encode(data).decode("ascii").rstrip("=")

def _code_hash(code, secret=None, cdk_secret=None):
    key = _resolve_cdk_secret(secret=secret, cdk_secret=cdk_secret)
    return hmac.new(key, _normalize_cdk(code).encode("utf-8"), hashlib.sha256).hexdigest()

def _normalize_cdk(code):
    return (code or "").strip().upper()

def _code_from_nonce(nonce, secret=None, cdk_secret=None, version=2):
    key = _resolve_cdk_secret(secret=secret, cdk_secret=cdk_secret)
    if version not in (1, 2):
        raise ValueError("unsupported CDK format version")
    tag_length = 8 if version == 1 else 16
    group_length = 6 if version == 1 else 8
    tag = _base32_digest(hmac.new(key, nonce.encode("utf-8"), hashlib.sha256).digest())[:tag_length]
    body = nonce + tag
    groups = [body[i:i + group_length] for i in range(0, len(body), group_length)]
    return "LOCK-" + "-".join(groups)

def _gen_cdk_parts(secret=None, cdk_secret=None):
    nonce = "".join(secrets.choice(CDK_ALPHABET) for _ in range(16))
    return nonce, _code_from_nonce(nonce, secret=secret, cdk_secret=cdk_secret, version=2)

def _ensure_columns(cursor, table, columns):
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    for name, definition in columns:
        if name not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

def _migrate_secure_cdk_plaintext(cursor):
    """Remove recoverable plaintext while preserving pre-v2 secure CDKs."""
    cursor.execute(
        """SELECT code, code_hash FROM cdk_codes
           WHERE code_hash IS NOT NULL AND code_nonce IS NULL AND code LIKE 'LOCK-%'"""
    )
    for code, code_hash in cursor.fetchall():
        body = code.removeprefix("LOCK-").replace("-", "")
        if len(body) not in (24, 32):
            continue
        cursor.execute(
            """UPDATE cdk_codes
               SET code = ?, code_nonce = ?, code_version = ?
               WHERE code = ? AND code_hash = ?""",
            (f"HMAC-{code_hash}", body[:16], 1 if len(body) == 24 else 2, code, code_hash),
        )

def init_db():
    conn = _connect()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS usage_logs (
                    user_id INTEGER,
                    date TEXT,
                    count INTEGER,
                    PRIMARY KEY (user_id, date)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    language TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    uid TEXT,
                    status TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS cdk_codes (
                    code TEXT PRIMARY KEY,
                    code_hash TEXT UNIQUE,
                    code_nonce TEXT,
                    code_version INTEGER,
                    used INTEGER DEFAULT 0,
                    used_by INTEGER,
                    used_at TEXT,
                    used_at_ts INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    created_ts INTEGER,
                    source TEXT DEFAULT 'admin',
                    created_by INTEGER,
                    order_id INTEGER,
                    reserved_by INTEGER,
                    reserved_at TEXT,
                    reserved_at_ts INTEGER,
                    reserved_until_ts INTEGER
                )''')
    _ensure_columns(c, "cdk_codes", [
        ("code_hash", "TEXT"),
        ("code_nonce", "TEXT"),
        ("code_version", "INTEGER"),
        ("used_at_ts", "INTEGER"),
        ("created_ts", "INTEGER"),
        ("source", "TEXT DEFAULT 'admin'"),
        ("created_by", "INTEGER"),
        ("order_id", "INTEGER"),
        ("reserved_by", "INTEGER"),
        ("reserved_at", "TEXT"),
        ("reserved_at_ts", "INTEGER"),
        ("reserved_until_ts", "INTEGER"),
        # Multi-use keys: a key can activate up to `spins` Locket accounts.
        ("spins", "INTEGER DEFAULT 1"),
        ("spins_left", "INTEGER"),
        # Plan decides which source pool tier is used (1m / 1y).
        ("plan", "TEXT DEFAULT '1m'"),
    ])
    _migrate_secure_cdk_plaintext(c)
    c.execute("UPDATE cdk_codes SET spins = 1 WHERE spins IS NULL")
    c.execute("UPDATE cdk_codes SET spins_left = 1 WHERE spins_left IS NULL AND used = 0")
    c.execute("UPDATE cdk_codes SET spins_left = 0 WHERE spins_left IS NULL AND used = 1")
    c.execute('''CREATE TABLE IF NOT EXISTS cdk_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER,
                    quantity INTEGER NOT NULL,
                    total_price INTEGER,
                    payment_content TEXT UNIQUE,
                    transaction_id TEXT UNIQUE,
                    matched_amount INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    completed_at INTEGER,
                    canceled_at INTEGER,
                    canceled_by INTEGER,
                    canceled_chat_id INTEGER,
                    plan TEXT DEFAULT '1m'
                )''')
    _ensure_columns(c, "cdk_orders", [
        ("plan", "TEXT DEFAULT '1m'"),
    ])
    c.execute('''CREATE TABLE IF NOT EXISTS user_activations (
                    user_id INTEGER,
                    locket_uid TEXT,
                    username TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (user_id, locket_uid)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS web_activations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER,
                    visitor_id INTEGER,
                    uid TEXT NOT NULL,
                    username TEXT,
                    avatar TEXT,
                    status TEXT DEFAULT 'awaiting_payment',
                    cdk_code TEXT,
                    progress TEXT,
                    result TEXT,
                    dns_link TEXT,
                    created_at INTEGER,
                    updated_at INTEGER,
                    completed_at INTEGER
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS web_paid_uids (
                    uid TEXT PRIMARY KEY,
                    order_id INTEGER,
                    created_at INTEGER
                )''')
    # Source pool for the alias-based activation engine: each row is a Locket
    # account that currently has Gold and can donate it up to MAX_SOURCE_SPINS.
    c.execute('''CREATE TABLE IF NOT EXISTS gold_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    uid TEXT,
                    count INTEGER NOT NULL DEFAULT 0,
                    in_flight INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    expires_ts INTEGER,
                    last_reserved_ts INTEGER,
                    created_at INTEGER,
                    updated_at INTEGER
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS key_redemptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_code TEXT NOT NULL,
                    user_id INTEGER,
                    target TEXT,
                    target_uid TEXT,
                    plan TEXT,
                    status TEXT NOT NULL DEFAULT 'success',
                    detail TEXT,
                    created_at INTEGER
                )''')
    # Every UID ever activated by the shop — used for free re-activation on web.
    c.execute('''CREATE TABLE IF NOT EXISTS activated_uids (
                    uid TEXT PRIMARY KEY,
                    first_at INTEGER,
                    last_at INTEGER,
                    activations INTEGER NOT NULL DEFAULT 0
                )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_gold_sources_pool ON gold_sources(count, in_flight, expires_ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_key_redemptions_user ON key_redemptions(user_id, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cdk_codes_created_by ON cdk_codes(created_by, used)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cdk_codes_order_id ON cdk_codes(order_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cdk_codes_available ON cdk_codes(used, reserved_by)")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cdk_codes_code_hash ON cdk_codes(code_hash) WHERE code_hash IS NOT NULL")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cdk_orders_payment_content ON cdk_orders(payment_content) WHERE payment_content IS NOT NULL")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cdk_orders_status_expires ON cdk_orders(status, expires_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_web_activations_status ON web_activations(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_web_activations_order_id ON web_activations(order_id)")
    conn.commit()
    conn.close()

def get_user_usage(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("SELECT count FROM usage_logs WHERE user_id = ? AND date = ?", (user_id, today))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def increment_usage(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    
    c.execute("SELECT count FROM usage_logs WHERE user_id = ? AND date = ?", (user_id, today))
    result = c.fetchone()
    
    if result:
        new_count = result[0] + 1
        c.execute("UPDATE usage_logs SET count = ? WHERE user_id = ? AND date = ?", (new_count, user_id, today))
    else:
        c.execute("INSERT INTO usage_logs (user_id, date, count) VALUES (?, ?, ?)", (user_id, today, 1))
        
    conn.commit()
    conn.close()

def check_can_request(user_id, max_limit=5):
    current = get_user_usage(user_id)
    return current < max_limit

def set_lang(user_id, lang):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_settings (user_id, language) VALUES (?, ?)", (user_id, lang))
    conn.commit()
    conn.close()

def get_lang(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT language FROM user_settings WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def get_all_users():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT DISTINCT user_id FROM usage_logs UNION SELECT user_id FROM user_settings")
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users

def reset_usage(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("DELETE FROM usage_logs WHERE user_id = ? AND date = ?", (user_id, today))
    conn.commit()
    conn.close()

def set_config(key, value):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def get_config(key, default=None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT value FROM bot_config WHERE key = ?", (key,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else default

def log_request(user_id, uid, status):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO request_logs (user_id, uid, status) VALUES (?, ?, ?)", (user_id, uid, status))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM request_logs")
    total = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM request_logs WHERE status = 'SUCCESS'")
    success = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM request_logs WHERE status != 'SUCCESS'")
    fail = c.fetchone()[0]
    
    c.execute("SELECT COUNT(DISTINCT user_id) FROM request_logs")
    unique_users = c.fetchone()[0]
    
    conn.close()
    return {
        "total": total,
        "success": success,
        "fail": fail,
        "unique_users": unique_users
    }

def gen_cdk(count, admin_id, cdk_secret=None, secret=None, source="admin", order_id=None, conn=None,
            plan="1m", spins=1):
    if count <= 0:
        return []
    if source not in ("admin", "purchase"):
        raise ValueError("source must be admin or purchase")
    plan = (plan or "1m").lower()
    if plan not in ("1m", "1y"):
        raise ValueError("plan must be 1m or 1y")
    try:
        spins = max(1, int(spins))
    except (TypeError, ValueError):
        spins = 1
    key = _resolve_cdk_secret(secret=secret, cdk_secret=cdk_secret)
    owns_conn = conn is None
    if owns_conn:
        conn = _connect()
    c = conn.cursor()
    codes = []
    tries = 0
    max_tries = max(count * 50, 50)
    now_ts = _now_ts()
    while len(codes) < count and tries < max_tries:
        tries += 1
        nonce, code = _gen_cdk_parts(cdk_secret=key.decode("utf-8"))
        hashed = _code_hash(code, cdk_secret=key.decode("utf-8"))
        try:
            c.execute(
                """INSERT INTO cdk_codes
                   (code, code_hash, code_nonce, code_version, source, created_by, order_id,
                    created_ts, plan, spins, spins_left)
                   VALUES (?, ?, ?, 2, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"HMAC-{hashed}",
                    hashed,
                    nonce,
                    source,
                    admin_id,
                    order_id,
                    now_ts,
                    plan,
                    spins,
                    spins,
                ),
            )
            codes.append(code)
        except sqlite3.IntegrityError:
            continue
    if owns_conn:
        conn.commit()
        conn.close()
    return codes

def _lookup_cdk_row(cursor, code, secret=None):
    normalized = _normalize_cdk(code)
    if secret:
        try:
            hashed = _code_hash(normalized, secret=secret)
        except ValueError:
            hashed = None
        if hashed:
            cursor.execute("SELECT * FROM cdk_codes WHERE code_hash = ?", (hashed,))
            row = cursor.fetchone()
            if row:
                return row
        cursor.execute("SELECT * FROM cdk_codes WHERE code = ? AND code_hash IS NULL", (normalized,))
        return cursor.fetchone()
    cursor.execute("SELECT * FROM cdk_codes WHERE code = ?", (normalized,))
    return cursor.fetchone()

def validate_cdk(code, secret=None):
    normalized = _normalize_cdk(code)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    row = _lookup_cdk_row(c, normalized, secret=secret)
    now_ts = _now_ts()
    reserved_until = row["reserved_until_ts"] if row else None
    reservation_active = (
        row
        and row["reserved_by"] is not None
        and (reserved_until is None or reserved_until > now_ts)
    )
    ok = bool(row and row["used"] == 0 and not reservation_active)
    conn.close()
    return ok

def get_cdk_source(code, secret=None):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    row = _lookup_cdk_row(c, code, secret=secret)
    conn.close()
    if not row:
        return None
    if secret and row["code_hash"] and row["code_hash"] != _code_hash(code, secret=secret):
        return None
    return row["source"] or "admin"

def reserve_cdk(code, user_id, secret=None, ttl_seconds=3600):
    normalized = _normalize_cdk(code)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now_ts = _now_ts()
    reserved_until_ts = now_ts + int(ttl_seconds)
    now_text = _now_text()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = _lookup_cdk_row(c, normalized, secret=secret)
        reservation_active = (
            row
            and row["reserved_by"] is not None
            and (row["reserved_until_ts"] is None or row["reserved_until_ts"] > now_ts)
        )
        if (
            not row
            or row["used"] != 0
            or (reservation_active and row["reserved_by"] != user_id)
        ):
            conn.rollback()
            return False
        c.execute(
            """UPDATE cdk_codes
               SET reserved_by = ?, reserved_at = ?, reserved_at_ts = ?, reserved_until_ts = ?
               WHERE code = ? AND used = 0
                 AND (reserved_by IS NULL OR reserved_by = ? OR reserved_until_ts IS NULL OR reserved_until_ts <= ?)""",
            (user_id, now_text, now_ts, reserved_until_ts, row["code"], user_id, now_ts),
        )
        ok = c.rowcount == 1
        conn.commit()
        return ok
    finally:
        conn.close()

def lock_reserved_cdk(code, user_id, secret=None):
    """Make an owned, active reservation durable while external activation runs."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now_ts = _now_ts()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = _lookup_cdk_row(c, code, secret=secret)
        if not row or row["used"] != 0 or row["reserved_by"] != user_id:
            conn.rollback()
            return False
        if row["reserved_until_ts"] is not None and row["reserved_until_ts"] <= now_ts:
            conn.rollback()
            return False
        c.execute(
            """UPDATE cdk_codes SET reserved_until_ts = NULL
               WHERE code = ? AND used = 0 AND reserved_by = ?
                 AND (reserved_until_ts IS NULL OR reserved_until_ts > ?)""",
            (row["code"], user_id, now_ts),
        )
        ok = c.rowcount == 1
        conn.commit()
        return ok
    finally:
        conn.close()

def release_cdk(code, user_id, secret=None):
    normalized = _normalize_cdk(code)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = _lookup_cdk_row(c, normalized, secret=secret)
        if not row:
            conn.rollback()
            return False
        c.execute(
            """UPDATE cdk_codes
               SET reserved_by = NULL, reserved_at = NULL, reserved_at_ts = NULL, reserved_until_ts = NULL
               WHERE code = ? AND used = 0 AND reserved_by = ?""",
            (row["code"], user_id),
        )
        ok = c.rowcount == 1
        conn.commit()
        return ok
    finally:
        conn.close()

def redeem_reserved_cdk(code, user_id, secret=None):
    normalized = _normalize_cdk(code)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now_ts = _now_ts()
    now_text = _now_text()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = _lookup_cdk_row(c, normalized, secret=secret)
        if not row:
            conn.rollback()
            return False
        c.execute(
            """UPDATE cdk_codes
               SET used = 1, used_by = ?, used_at = ?, used_at_ts = ?,
                   reserved_by = NULL, reserved_at = NULL, reserved_at_ts = NULL, reserved_until_ts = NULL
               WHERE code = ? AND used = 0 AND reserved_by = ?
                 AND (reserved_until_ts IS NULL OR reserved_until_ts > ?)""",
            (user_id, now_text, now_ts, row["code"], user_id, now_ts),
        )
        ok = c.rowcount == 1
        conn.commit()
        return ok
    finally:
        conn.close()

def redeem_cdk(code, user_id, secret=None):
    normalized = _normalize_cdk(code)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now_ts = _now_ts()
    now_text = _now_text()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = _lookup_cdk_row(c, normalized, secret=secret)
        if not row:
            conn.rollback()
            return False
        c.execute(
            """UPDATE cdk_codes
               SET used = 1, used_by = ?, used_at = ?, used_at_ts = ?,
                   reserved_by = NULL, reserved_at = NULL, reserved_at_ts = NULL, reserved_until_ts = NULL
               WHERE code = ? AND used = 0
                 AND (reserved_by IS NULL OR (reserved_by = ? AND reserved_until_ts IS NOT NULL AND reserved_until_ts > ?))""",
            (user_id, now_text, now_ts, row["code"], user_id, now_ts),
        )
        ok = c.rowcount == 1
        conn.commit()
        return ok
    finally:
        conn.close()

def create_cdk_order(user_id, chat_id, quantity, total_price, payment_content, expires_at=None,
                     transaction_id=None, plan="1m"):
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    plan = (plan or "1m").lower()
    if plan not in ("1m", "1y"):
        plan = "1m"
    now_ts = _now_ts()
    expires_ts = _parse_ts(expires_at)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """UPDATE cdk_orders
               SET status = 'expired', updated_at = ?
               WHERE status = 'pending'
                 AND expires_at IS NOT NULL
                 AND expires_at <= ?""",
            (now_ts, now_ts),
        )
        c.execute(
            """SELECT * FROM cdk_orders
               WHERE user_id = ?
                 AND status = 'pending'
                 AND COALESCE(plan, '1m') = ?
                 AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY created_at ASC, id ASC
               LIMIT 1""",
            (user_id, plan, now_ts),
        )
        existing = _row_to_dict(c.fetchone())
        if existing:
            conn.commit()
            return existing

        c.execute(
            """INSERT INTO cdk_orders
               (user_id, chat_id, quantity, total_price, payment_content, transaction_id,
                status, created_at, updated_at, expires_at, plan)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (
                user_id,
                chat_id,
                quantity,
                total_price,
                payment_content,
                transaction_id,
                now_ts,
                now_ts,
                expires_ts,
                plan,
            ),
        )
        c.execute("SELECT * FROM cdk_orders WHERE id = ?", (c.lastrowid,))
        order = _row_to_dict(c.fetchone())
        conn.commit()
        return order
    except sqlite3.IntegrityError:
        conn.rollback()
        if transaction_id:
            return get_cdk_order(transaction_id=transaction_id)
        raise
    finally:
        conn.close()

def get_cdk_order(id=None, order_id=None, transaction_id=None):
    lookup_id = order_id if order_id is not None else id
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if lookup_id is not None:
        c.execute("SELECT * FROM cdk_orders WHERE id = ?", (lookup_id,))
    elif transaction_id is not None:
        c.execute("SELECT * FROM cdk_orders WHERE transaction_id = ?", (transaction_id,))
    else:
        conn.close()
        return None
    order = _row_to_dict(c.fetchone())
    conn.close()
    return order

def get_active_cdk_order_for_user(user_id, now=None):
    now_ts = _parse_ts(now) if now is not None else _now_ts()
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT * FROM cdk_orders
               WHERE user_id = ?
                 AND status = 'pending'
                 AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY created_at ASC, id ASC
               LIMIT 1""",
            (user_id, now_ts),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()

def get_pending_cdk_orders(now=None):
    now_ts = _parse_ts(now) if now is not None else _now_ts()
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        """SELECT * FROM cdk_orders
           WHERE status = 'pending' AND (expires_at IS NULL OR expires_at > ?)
           ORDER BY created_at ASC, id ASC""",
        (now_ts,),
    )
    orders = [_row_to_dict(row) for row in c.fetchall()]
    conn.close()
    return orders

def list_pending_cdk_orders(now=None):
    return get_pending_cdk_orders(now=now)

def cancel_cdk_order(id, user_id=None, chat_id=None):
    now_ts = _now_ts()
    where = ["id = ?", "status = 'pending'"]
    params = [id]
    if user_id is not None:
        where.append("user_id = ?")
        params.append(user_id)
    if chat_id is not None:
        where.append("chat_id = ?")
        params.append(chat_id)
    conn = _connect()
    c = conn.cursor()
    c.execute(
        f"""UPDATE cdk_orders
            SET status = 'canceled', canceled_at = ?, canceled_by = ?, canceled_chat_id = ?,
                updated_at = ?
            WHERE {' AND '.join(where)}""",
        [now_ts, user_id, chat_id, now_ts] + params,
    )
    conn.commit()
    ok = c.rowcount == 1
    conn.close()
    return ok

def expire_cdk_orders(now=None):
    now_ts = _parse_ts(now) if now is not None else _now_ts()
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT * FROM cdk_orders WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at <= ?",
        (now_ts,),
    )
    expired = [_row_to_dict(row) for row in c.fetchall()]
    if expired:
        c.execute(
            """UPDATE cdk_orders
               SET status = 'expired', updated_at = ?
               WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at <= ?""",
            (now_ts, now_ts),
        )
    conn.commit()
    conn.close()
    return expired

def _get_order_codes(cursor, order_id, secret):
    cursor.execute(
        """SELECT code, code_hash, code_nonce, code_version
           FROM cdk_codes WHERE order_id = ? ORDER BY rowid ASC""",
        (order_id,),
    )
    codes = []
    for stored_code, stored_hash, nonce, version in cursor.fetchall():
        if stored_hash and nonce:
            code = _code_from_nonce(nonce, secret=secret, version=version or 1)
            if not hmac.compare_digest(_code_hash(code, secret=secret), stored_hash):
                raise ValueError("stored CDK integrity check failed")
            codes.append(code)
        else:
            codes.append(stored_code)
    return codes

def complete_cdk_order(order_id=None, transaction_id=None, matched_amount=None, secret=None, cdk_secret=None, completed_by=None):
    if order_id is None and transaction_id is None:
        raise ValueError("order_id or transaction_id is required")
    resolved_secret = (secret if secret is not None else cdk_secret)
    _resolve_cdk_secret(secret=resolved_secret)
    now_ts = _now_ts()
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        if order_id is not None:
            c.execute("SELECT * FROM cdk_orders WHERE id = ?", (order_id,))
        else:
            c.execute("SELECT * FROM cdk_orders WHERE transaction_id = ?", (transaction_id,))
        order = c.fetchone()
        if not order:
            conn.rollback()
            return None
        if order["status"] == "completed":
            if transaction_id is not None and order["transaction_id"] != transaction_id:
                conn.rollback()
                return None
            codes = _get_order_codes(c, order["id"], resolved_secret)
            conn.commit()
            return codes
        if order["status"] != "pending":
            conn.rollback()
            return None
        if order["expires_at"] is not None and order["expires_at"] <= now_ts:
            c.execute(
                "UPDATE cdk_orders SET status = 'expired', updated_at = ? WHERE id = ? AND status = 'pending'",
                (now_ts, order["id"]),
            )
            conn.commit()
            return None
        if (
            matched_amount is not None
            and order["total_price"] is not None
            and int(matched_amount) != int(order["total_price"])
        ):
            conn.rollback()
            return None
        if transaction_id is not None:
            c.execute(
                "SELECT id, status FROM cdk_orders WHERE transaction_id = ? AND id != ?",
                (transaction_id, order["id"]),
            )
            existing = c.fetchone()
            if existing:
                conn.rollback()
                return None
        elif order["transaction_id"] is None:
            conn.rollback()
            return None
        claimed_transaction_id = transaction_id or order["transaction_id"]
        creator_id = completed_by if completed_by is not None else order["user_id"]
        order_plan = (order["plan"] if "plan" in order.keys() else None) or "1m"
        codes = gen_cdk(
            order["quantity"],
            admin_id=creator_id,
            cdk_secret=resolved_secret,
            source="purchase",
            order_id=order["id"],
            conn=conn,
            plan=order_plan,
        )
        if len(codes) != order["quantity"]:
            conn.rollback()
            return None
        c.execute(
            """UPDATE cdk_orders
               SET status = 'completed', transaction_id = ?, matched_amount = ?,
                   completed_at = ?, updated_at = ?
               WHERE id = ? AND status = 'pending'""",
            (claimed_transaction_id, matched_amount, now_ts, now_ts, order["id"]),
        )
        if c.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        return codes
    except sqlite3.IntegrityError:
        conn.rollback()
        if transaction_id is not None and order_id is not None:
            existing = get_cdk_order(transaction_id=transaction_id)
            if existing and existing["id"] == order_id and existing["status"] == "completed":
                read_conn = _connect()
                try:
                    return _get_order_codes(read_conn.cursor(), existing["id"], resolved_secret)
                finally:
                    read_conn.close()
        return None
    finally:
        conn.close()

def has_cdk(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT 1 FROM cdk_codes WHERE used_by = ?", (user_id,))
    ok = c.fetchone() is not None
    conn.close()
    return ok

def save_activation(user_id, uid, username):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO user_activations (user_id, locket_uid, username, updated_at) VALUES (?, ?, ?, ?)",
        (user_id, uid, username, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()

def get_activation_uids(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT locket_uid FROM user_activations WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def cdk_stats():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM cdk_codes")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM cdk_codes WHERE used = 1")
    used = c.fetchone()[0]
    conn.close()
    return {"total": total, "used": used, "unused": total - used}

def get_recent_cdks(limit=10):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT code, used, used_by, used_at FROM cdk_codes ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_cdk_detail(code, secret=None):
    """Return human-readable CDK status for web verification pages.

    Returns a dict: {"found": bool, "status": "valid"|"used"|"reserved"|"not_found",
    "source", "used_by", "used_at", "created_at"}.
    """
    normalized = _normalize_cdk(code)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    row = _lookup_cdk_row(c, normalized, secret=secret)
    conn.close()
    if not row:
        return {"found": False, "status": "not_found", "code": normalized}
    now_ts = _now_ts()
    reserved_active = (
        row["reserved_by"] is not None
        and (row["reserved_until_ts"] is None or row["reserved_until_ts"] > now_ts)
    )
    if row["used"]:
        status = "used"
    elif reserved_active:
        status = "reserved"
    else:
        status = "valid"
    spins = row["spins"] if row["spins"] is not None else 1
    spins_left = row["spins_left"]
    if spins_left is None:
        spins_left = 0 if row["used"] else spins
    if status == "valid" and spins_left <= 0:
        status = "used"
    return {
        "found": True,
        "status": status,
        "code": normalized,
        "source": row["source"] or "admin",
        "plan": row["plan"] or "1m",
        "spins": spins,
        "spins_left": spins_left,
        "used_by": row["used_by"],
        "used_at": row["used_at"],
        "created_at": row["created_at"],
    }


def list_cdk_orders(limit=100, status=None):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if status:
        c.execute(
            "SELECT * FROM cdk_orders WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        )
    else:
        c.execute("SELECT * FROM cdk_orders ORDER BY id DESC LIMIT ?", (limit,))
    orders = [_row_to_dict(row) for row in c.fetchall()]
    conn.close()
    return orders


def list_cdk_codes(limit=100, secret=None):
    """Decoded CDK rows (with codes) for the admin panel."""
    key = _resolve_cdk_secret(secret=secret)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        """SELECT code, code_hash, code_nonce, code_version, used, used_by, used_at,
                  created_at, source, order_id
           FROM cdk_codes ORDER BY created_ts DESC, rowid DESC LIMIT ?""",
        (limit,),
    )
    rows = []
    for r in c.fetchall():
        item = dict(r)
        if r["code_hash"] and r["code_nonce"]:
            try:
                decoded = _code_from_nonce(
                    r["code_nonce"], secret=key.decode("utf-8"), version=r["code_version"] or 1
                )
                if hmac.compare_digest(_code_hash(decoded, secret=key.decode("utf-8")), r["code_hash"]):
                    item["code"] = decoded
            except ValueError:
                pass
        rows.append(item)
    conn.close()
    return rows


def cdk_order_stats():
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT status, COUNT(*) FROM cdk_orders GROUP BY status")
    counts = {row[0]: row[1] for row in c.fetchall()}
    c.execute("SELECT COALESCE(SUM(total_price), 0) FROM cdk_orders WHERE status = 'completed'")
    revenue = c.fetchone()[0]
    conn.close()
    return {
        "total": sum(counts.values()),
        "pending": counts.get("pending", 0),
        "completed": counts.get("completed", 0),
        "expired": counts.get("expired", 0),
        "canceled": counts.get("canceled", 0),
        "revenue": revenue or 0,
    }


# ---------------------------------------------------------------------------
# Web activations (auto-activate after web payment)
# ---------------------------------------------------------------------------

def create_web_activation(order_id, visitor_id, uid, username, avatar=None, status="awaiting_payment"):
    now_ts = _now_ts()
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute(
            """INSERT INTO web_activations
               (order_id, visitor_id, uid, username, avatar, status,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_id, visitor_id, uid, username, avatar, status, now_ts, now_ts),
        )
        conn.commit()
        return c.lastrowid
    finally:
        conn.close()


def get_web_activation(id=None, order_id=None, uid=None):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if id is not None:
        c.execute("SELECT * FROM web_activations WHERE id = ?", (id,))
    elif order_id is not None:
        c.execute("SELECT * FROM web_activations WHERE order_id = ?", (order_id,))
    elif uid is not None:
        c.execute(
            "SELECT * FROM web_activations WHERE uid = ? ORDER BY id DESC LIMIT 1",
            (uid,),
        )
    else:
        conn.close()
        return None
    row = _row_to_dict(c.fetchone())
    conn.close()
    return row


def update_web_activation(id, **fields):
    allowed = {"status", "cdk_code", "progress", "result", "dns_link", "completed_at", "updated_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates.setdefault("updated_at", _now_ts())
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        sets = ", ".join(f"{k} = ?" for k in updates)
        c.execute(
            f"UPDATE web_activations SET {sets} WHERE id = ?",
            (*updates.values(), id),
        )
        ok = c.rowcount == 1
        conn.commit()
        return ok
    finally:
        conn.close()


def claim_web_activation(id):
    """Atomically flip a queued activation to processing (bot worker claims it)."""
    conn = _connect()
    c = conn.cursor()
    now_ts = _now_ts()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """UPDATE web_activations
               SET status = 'processing', updated_at = ?
               WHERE id = ? AND status = 'queued'""",
            (now_ts, id),
        )
        ok = c.rowcount == 1
        conn.commit()
        return ok
    finally:
        conn.close()


def list_queued_web_activations(limit=20):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        """SELECT * FROM web_activations
           WHERE status = 'queued'
           ORDER BY id ASC LIMIT ?""",
        (limit,),
    )
    rows = [_row_to_dict(row) for row in c.fetchall()]
    conn.close()
    return rows


def list_web_activations(limit=50):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        """SELECT * FROM web_activations ORDER BY id DESC LIMIT ?""",
        (limit,),
    )
    rows = [_row_to_dict(row) for row in c.fetchall()]
    conn.close()
    return rows


def web_activation_stats():
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT status, COUNT(*) FROM web_activations GROUP BY status")
    counts = {row[0]: row[1] for row in c.fetchall()}
    conn.close()
    return {
        "total": sum(counts.values()),
        "awaiting_payment": counts.get("awaiting_payment", 0),
        "paid": counts.get("paid", 0),
        "queued": counts.get("queued", 0),
        "processing": counts.get("processing", 0),
        "success": counts.get("success", 0),
        "failed": counts.get("failed", 0),
    }


def mark_uid_paid(uid, order_id=None):
    """Record that a Locket UID was paid for once — re-activation is free forever."""
    conn = _connect()
    c = conn.cursor()
    now_ts = _now_ts()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """INSERT OR IGNORE INTO web_paid_uids (uid, order_id, created_at)
               VALUES (?, ?, ?)""",
            (uid, order_id, now_ts),
        )
        conn.commit()
    finally:
        conn.close()


def is_uid_paid(uid):
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT 1 FROM web_paid_uids WHERE uid = ?", (uid,))
    ok = c.fetchone() is not None
    conn.close()
    return ok


def delete_web_activation(id):
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM web_activations WHERE id = ?", (id,))
        ok = c.rowcount == 1
        conn.commit()
        return ok
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Redeem keys (multi-use keys with a plan: 1m / 1y)
# ---------------------------------------------------------------------------

MAX_SOURCE_SPINS = 5


def _row_to_key(row):
    if row is None:
        return None
    item = _row_to_dict(row)
    spins = item.get("spins") or 1
    left = item.get("spins_left")
    if left is None:
        left = 0 if item.get("used") else spins
    item["spins"] = spins
    item["spins_left"] = left
    item["plan"] = item.get("plan") or "1m"
    return item


def consume_key(code, user_id=None, secret=None):
    """Atomically use one spin of a key.

    Returns (ok, message, plan, spins_left, source).
    """
    normalized = _normalize_cdk(code)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now_ts = _now_ts()
    now_text = _now_text()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = _lookup_cdk_row(c, normalized, secret=secret)
        if not row:
            conn.rollback()
            return False, "not_found", None, 0, None
        spins = row["spins"] if row["spins"] is not None else 1
        left = row["spins_left"]
        if left is None:
            left = 0 if row["used"] else spins
        if spins <= 0 or left <= 0:
            conn.rollback()
            return False, "exhausted", row["plan"] or "1m", 0, row["source"]
        left -= 1
        c.execute(
            """UPDATE cdk_codes
               SET spins_left = ?, used = ?, used_by = ?, used_at = ?, used_at_ts = ?,
                   reserved_by = NULL, reserved_at = NULL, reserved_at_ts = NULL, reserved_until_ts = NULL
               WHERE code = ? AND used = 0""",
            (left, 1 if left == 0 else 0, user_id, now_text, now_ts, row["code"]),
        )
        ok = c.rowcount == 1
        conn.commit()
        if not ok:
            return False, "exhausted", row["plan"] or "1m", left, row["source"]
        return True, "ok", row["plan"] or "1m", left, row["source"]
    finally:
        conn.close()


def refund_key_spin(code, secret=None):
    """Give back one unused spin after a failed activation."""
    normalized = _normalize_cdk(code)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = _lookup_cdk_row(c, normalized, secret=secret or os.environ.get(CDK_SECRET_ENV))
        if not row:
            conn.rollback()
            return False
        spins = row["spins"] if row["spins"] is not None else 1
        left = row["spins_left"]
        if left is None:
            left = 0 if row["used"] else spins
        left = min(spins, left + 1)
        c.execute(
            "UPDATE cdk_codes SET spins_left = ?, used = ? WHERE code = ?",
            (left, 0 if left > 0 else 1, row["code"]),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def mark_uid_activated(uid, when=None):
    """Record that a Locket UID was successfully activated (enables free re-activation)."""
    if not uid:
        return False
    now_ts = int(when) if when is not None else _now_ts()
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("SELECT uid FROM activated_uids WHERE uid = ?", (uid,))
        row = c.fetchone()
        if row:
            c.execute(
                "UPDATE activated_uids SET last_at = ?, activations = activations + 1 WHERE uid = ?",
                (now_ts, uid),
            )
        else:
            c.execute(
                "INSERT INTO activated_uids (uid, first_at, last_at, activations) VALUES (?, ?, ?, 1)",
                (uid, now_ts, now_ts),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def get_uid_activation(uid):
    if not uid:
        return None
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM activated_uids WHERE uid = ?", (uid,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def count_free_reactivations(user_id, since_ts):
    """How many free re-activations a visitor used since `since_ts`."""
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT COUNT(*) FROM key_redemptions
               WHERE user_id = ? AND key_code = 'FREE-REACTIVATE' AND created_at >= ?""",
            (user_id, int(since_ts)),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def log_key_redemption(key_code, user_id, target, target_uid, plan, status="success", detail=None):
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute(
            """INSERT INTO key_redemptions
               (key_code, user_id, target, target_uid, plan, status, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (key_code, user_id, target, target_uid, plan, status, detail, _now_ts()),
        )
        conn.commit()
    finally:
        conn.close()


def list_key_redemptions(user_id=None, limit=20):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if user_id is None:
        c.execute("SELECT * FROM key_redemptions ORDER BY id DESC LIMIT ?", (limit,))
    else:
        c.execute(
            "SELECT * FROM key_redemptions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
    rows = [_row_to_dict(row) for row in c.fetchall()]
    conn.close()
    return rows


def user_key_stats(user_id):
    """Unused keys owned by a user plus purchase totals."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        """SELECT plan, COUNT(*) AS n FROM cdk_codes
           WHERE created_by = ? AND used = 0 AND COALESCE(spins_left, 0) > 0
           GROUP BY COALESCE(plan, '1m')""",
        (user_id,),
    )
    unused = {row["plan"]: row["n"] for row in c.fetchall()}
    c.execute(
        """SELECT COUNT(*) AS n, COALESCE(SUM(quantity), 0) AS qty,
                  COALESCE(SUM(total_price), 0) AS spent
           FROM cdk_orders WHERE user_id = ? AND status = 'completed'""",
        (user_id,),
    )
    orders = c.fetchone()
    c.execute(
        """SELECT COUNT(*) AS n FROM key_redemptions
           WHERE user_id = ? AND status = 'success'""",
        (user_id,),
    )
    redeemed = c.fetchone()["n"]
    conn.close()
    return {
        "unused": unused,
        "unused_total": sum(unused.values()),
        "orders": orders["n"] if orders else 0,
        "quantity": orders["qty"] if orders else 0,
        "spent": orders["spent"] if orders else 0,
        "redeemed": redeemed or 0,
    }


def list_user_keys(user_id, limit=10):
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        """SELECT code, code_hash, code_nonce, code_version, used, used_at, created_at,
                  COALESCE(plan, '1m') AS plan, COALESCE(spins, 1) AS spins,
                  COALESCE(spins_left, 0) AS spins_left
           FROM cdk_codes
           WHERE created_by = ? AND used = 0 AND COALESCE(spins_left, 0) > 0
           ORDER BY created_ts DESC, rowid DESC LIMIT ?""",
        (user_id, limit),
    )
    rows = [_row_to_key(row) for row in c.fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Gold source pool (alias activation engine)
# ---------------------------------------------------------------------------

def normalize_source_username(value):
    raw = (value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    for marker, prefix in (
        ("locket.camera/invites/", ""),
        ("locket.cam/invites/", ""),
        ("locket.camera/", ""),
        ("locket.cam/", ""),
    ):
        if marker in lowered:
            raw = raw.split(marker, 1)[1]
            break
    raw = raw.split("?", 1)[0].strip().strip("/").lstrip("@")
    return raw


def parse_expiry_text(value):
    """Return (epoch_seconds, 'YYYY-MM-DD HH:MM:SS') from a stored expiry string."""
    if not value:
        return None, None
    text = str(value).strip()
    match = None
    for pattern in (
        r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})",
        r"(\d{4}-\d{2}-\d{2})",
    ):
        match = re.search(pattern, text)
        if match:
            break
    if not match:
        return None, None
    stamp = match.group(1).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(stamp, fmt)
            return int(dt.timestamp()), dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None, None


def days_left_of(expires_ts):
    if not expires_ts:
        return 0
    return max(0, (datetime.fromtimestamp(expires_ts) - datetime.now()).days)


def add_gold_source(username, uid=None, count=0, expires=None, min_days=10):
    """Insert or refresh a source. Returns True when the row changed/added."""
    name = normalize_source_username(username)
    if not name or len(name) < 3:
        return False
    expires_ts, expires_at = parse_expiry_text(expires)
    if expires_ts and days_left_of(expires_ts) < min_days:
        return False
    now_ts = _now_ts()
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("SELECT * FROM gold_sources WHERE username = ? COLLATE NOCASE", (name,))
        row = c.fetchone()
        if row:
            new_count = max(int(row["count"] or 0), int(count or 0))
            new_exp = expires_at or row["expires_at"]
            new_exp_ts = expires_ts or row["expires_ts"]
            c.execute(
                """UPDATE gold_sources
                   SET uid = COALESCE(?, uid), count = ?, expires_at = ?, expires_ts = ?, updated_at = ?
                   WHERE id = ?""",
                (uid, new_count, new_exp, new_exp_ts, now_ts, row["id"]),
            )
            conn.commit()
            return True
        c.execute(
            """INSERT INTO gold_sources (username, uid, count, in_flight, expires_at, expires_ts, created_at, updated_at)
               VALUES (?, ?, ?, 0, ?, ?, ?, ?)""",
            (name, uid, max(0, int(count or 0)), expires_at, expires_ts, now_ts, now_ts),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def gold_source_exists(username):
    name = normalize_source_username(username)
    if not name:
        return False
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM gold_sources WHERE username = ? COLLATE NOCASE", (name,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def add_gold_source_many(items, min_days=10):
    """Bulk add from [(username, expires, days_left), ...]. Returns added count."""
    added = 0
    for item in items or []:
        if isinstance(item, (tuple, list)):
            username = item[0]
            expires = item[1] if len(item) > 1 else ""
        else:
            username, expires = item, ""
        if add_gold_source(username, expires=expires, min_days=min_days):
            added += 1
    return added


def list_gold_sources(available_only=False, plan=None):
    """Sources sorted best-first for the requested plan."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM gold_sources")
    rows = [_row_to_dict(row) for row in c.fetchall()]
    conn.close()
    now_ts = _now_ts()
    for item in rows:
        item["days_left"] = days_left_of(item.get("expires_ts"))
        item["in_flight"] = int(item.get("in_flight") or 0)
        # A crashed activation must not block a slot forever.
        if item.get("last_reserved_ts") and now_ts - int(item["last_reserved_ts"]) > 300:
            item["in_flight"] = 0
        item["slots_left"] = max(0, MAX_SOURCE_SPINS - int(item.get("count") or 0) - item["in_flight"])
    if available_only:
        rows = [r for r in rows if r["slots_left"] > 0]
    return rows


def gold_source_stats():
    sources = list_gold_sources()
    total = len(sources)
    usable = sum(1 for s in sources if s["slots_left"] > 0)
    expired = sum(1 for s in sources if not s["expires_ts"] or s["days_left"] < 10)
    return {"total": total, "usable": usable, "expired": expired}


def cleanup_gold_sources(min_days=10):
    """Drop sources that are expired or too close to expiry. Returns removed count."""
    conn = _connect()
    c = conn.cursor()
    now_ts = _now_ts()
    cutoff = int((datetime.now().timestamp()) + max(0, min_days) * 86400)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "DELETE FROM gold_sources WHERE expires_ts IS NOT NULL AND expires_ts < ?",
            (cutoff,),
        )
        removed = c.rowcount
        conn.commit()
        return max(0, removed)
    finally:
        conn.close()


def remove_gold_source(username):
    name = normalize_source_username(username)
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM gold_sources WHERE username = ? COLLATE NOCASE", (name,))
        ok = c.rowcount == 1
        conn.commit()
        return ok
    finally:
        conn.close()


def reserve_gold_source(plan="1m"):
    """Pick and reserve the best source slot for a plan. Returns source dict or None."""
    plan = "1y" if (plan or "").lower() == "1y" else "1m"
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now_ts = _now_ts()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("SELECT * FROM gold_sources")
        rows = [dict(row) for row in c.fetchall()]
        candidates = []
        for item in rows:
            in_flight = int(item.get("in_flight") or 0)
            if item.get("last_reserved_ts") and now_ts - int(item["last_reserved_ts"]) > 300:
                in_flight = 0
            slots = MAX_SOURCE_SPINS - int(item.get("count") or 0) - in_flight
            if slots <= 0:
                continue
            item["_in_flight"] = in_flight
            item["_days"] = days_left_of(item.get("expires_ts"))
            item["_slots"] = slots
            candidates.append(item)
        if not candidates:
            conn.rollback()
            return None

        def pick_tier(pool, low, high):
            tier = [s for s in pool if low <= s["_days"] <= high]
            if tier:
                tier.sort(key=lambda s: (s["_days"], -s["_slots"]))
                return tier[0]
            return None

        if plan == "1y":
            selected = pick_tier(candidates, 200, 360) or pick_tier(candidates, 190, 370)
            if not selected:
                candidates.sort(key=lambda s: s["_days"], reverse=True)
                selected = candidates[0]
        else:
            selected = pick_tier(candidates, 25, 30) or pick_tier(candidates, 22, 33) or pick_tier(candidates, 15, 60)
            if not selected:
                candidates.sort(key=lambda s: (-s["_slots"], -s["_days"]))
                selected = candidates[0]

        c.execute(
            """UPDATE gold_sources
               SET in_flight = ?, last_reserved_ts = ?, updated_at = ?
               WHERE id = ? AND in_flight = ?""",
            (selected["_in_flight"] + 1, now_ts, now_ts, selected["id"], selected["_in_flight"]),
        )
        if c.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        selected["in_flight"] = selected["_in_flight"] + 1
        selected["days_left"] = selected["_days"]
        selected["slots_left"] = selected["_slots"] - 1
        return selected
    finally:
        conn.close()


def release_gold_source(source_id, success=False, exhausted=False):
    """Release a reserved slot; increments count when Gold was delivered or exhausted."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now_ts = _now_ts()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("SELECT * FROM gold_sources WHERE id = ?", (source_id,))
        row = c.fetchone()
        if not row:
            conn.rollback()
            return None
        in_flight = max(0, int(row["in_flight"] or 0) - 1)
        count = int(row["count"] or 0)
        if exhausted:
            count = MAX_SOURCE_SPINS
        elif success:
            count = min(MAX_SOURCE_SPINS, count + 1)
        c.execute(
            "UPDATE gold_sources SET in_flight = ?, count = ?, updated_at = ? WHERE id = ?",
            (in_flight, count, now_ts, source_id),
        )
        removed = False
        if exhausted or count >= MAX_SOURCE_SPINS:
            c.execute("DELETE FROM gold_sources WHERE id = ?", (source_id,))
            removed = True
        conn.commit()
        return {"count": count, "in_flight": in_flight, "removed": removed}
    finally:
        conn.close()


def import_sources_from_file(path, min_days=10):
    """One-time import of the legacy current_source.txt pool. Returns added count."""
    if not path or not os.path.exists(path):
        return 0
    added = 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("="):
                    continue
                parts = [part.strip() for part in line.split("|")]
                if len(parts) >= 3:
                    username = parts[1]
                    try:
                        count = int(parts[2])
                    except ValueError:
                        count = 0
                    expires = parts[3] if len(parts) >= 4 else ""
                else:
                    username, count, expires = parts[0], 0, ""
                if add_gold_source(username, count=count, expires=expires, min_days=min_days):
                    added += 1
    except OSError:
        return 0
    return added
