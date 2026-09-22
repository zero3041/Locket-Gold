import json
import os
import threading

USERS_FILE = "users_data.json"
_lock = threading.Lock()

def _load_data():
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_data(data):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_user_balance(user_id: int) -> int:
    """Lấy số dư hiện tại của user (mặc định 0đ)."""
    with _lock:
        data = _load_data()
        user = data.get(str(user_id), {})
        return user.get("balance", 0)

def add_user_balance(user_id: int, amount: int, username: str = "") -> int:
    """Cộng tiền vào tài khoản user."""
    with _lock:
        data = _load_data()
        uid = str(user_id)
        if uid not in data:
            data[uid] = {
                "balance": 0,
                "username": username,
                "total_recharged": 0,
                "total_spent": 0,
                "purchased_count": 0
            }
        data[uid]["balance"] += amount
        data[uid]["total_recharged"] = data[uid].get("total_recharged", 0) + amount
        if username:
            data[uid]["username"] = username
        _save_data(data)
        return data[uid]["balance"]

def deduct_user_balance(user_id: int, amount: int) -> bool:
    """Trừ tiền tài khoản user (nếu đủ số dư)."""
    with _lock:
        data = _load_data()
        uid = str(user_id)
        if uid not in data or data[uid].get("balance", 0) < amount:
            return False
        data[uid]["balance"] -= amount
        data[uid]["total_spent"] = data[uid].get("total_spent", 0) + amount
        data[uid]["purchased_count"] = data[uid].get("purchased_count", 0) + 1
        _save_data(data)
        return True

def refund_user_balance(user_id: int, amount: int) -> int:
    """Hoàn lại tiền vào tài khoản user khi giao dịch lỗi (thread-safe)."""
    with _lock:
        data = _load_data()
        uid = str(user_id)
        if uid in data:
            data[uid]["balance"] += amount
            data[uid]["total_spent"] = max(0, data[uid].get("total_spent", 0) - amount)
            data[uid]["purchased_count"] = max(0, data[uid].get("purchased_count", 0) - 1)
            _save_data(data)
            return data[uid]["balance"]
        return 0

def get_user_info(user_id: int) -> dict:
    with _lock:
        data = _load_data()
        return data.get(str(user_id), {"balance": 0, "purchased_count": 0})

KEYS_FILE = "keys_data.json"

def _load_keys():
    if not os.path.exists(KEYS_FILE):
        return {}
    try:
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_keys(keys):
    with open(KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(keys, f, ensure_ascii=False, indent=2)

def create_key(spins: int = 1, created_by: int = 0, note: str = "", plan: str = "1m") -> str:
    """Tạo mã key mới có số lượt kích hoạt (spins) và gói thời hạn (plan: '1m' hoặc '1y')."""
    import secrets
    token = secrets.token_hex(3).upper() # 6 ký tự hex ngẫu nhiên
    key_code = f"LK-GOLD-{token}"
    with _lock:
        keys = _load_keys()
        keys[key_code] = {
            "spins": spins,
            "spins_left": spins,
            "plan": plan.lower(),
            "created_by": created_by,
            "note": note,
            "used_by": []
        }
        _save_keys(keys)
    return key_code

def use_key(key_code: str, user_id: int, target_username: str) -> tuple[bool, str, str]:
    """Sử dụng 1 lượt của key. Trả về (success, message, plan)."""
    with _lock:
        keys = _load_keys()
        key_code = key_code.strip().upper()
        if key_code not in keys:
            return False, "Key không tồn tại hoặc không hợp lệ!", "1m"
        k = keys[key_code]
        if k.get("spins_left", 0) <= 0:
            return False, "Key này đã hết lượt sử dụng!", k.get("plan", "1m")
        k["spins_left"] -= 1
        k["used_by"].append({
            "user_id": user_id,
            "target": target_username
        })
        _save_keys(keys)
        plan = k.get("plan", "1m")
        plan_name = "1 Năm" if plan == "1y" else "1 Tháng"
        return True, f"Key hợp lệ gói {plan_name} (còn {k['spins_left']} lượt).", plan

def refund_key(key_code: str) -> bool:
    """Hoàn lại 1 lượt cho key khi tiến trình kích hoạt thất bại (thread-safe)."""
    with _lock:
        keys = _load_keys()
        k_code = key_code.strip().upper()
        if k_code in keys:
            keys[k_code]["spins_left"] = keys[k_code].get("spins_left", 0) + 1
            if keys[k_code].get("used_by"):
                keys[k_code]["used_by"].pop()
            _save_keys(keys)
            return True
        return False

