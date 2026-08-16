import asyncio
import re
import unicodedata
from typing import Any
from urllib.parse import urlencode

import aiohttp


SEPAY_TRANSACTIONS_URL = "https://userapi.sepay.vn/v2/transactions"
VIETQR_BASE_URL = "https://img.vietqr.io/image"
MAX_PER_PAGE = 100
DEFAULT_PER_PAGE = 20

_ALNUM_RE = re.compile(r"[A-Z0-9]")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")


class SePayError(Exception):
    """Base error for SePay integration failures."""


class SePayHTTPError(SePayError):
    """Raised when SePay returns a non-2xx HTTP response."""

    def __init__(self, status: int, message: str = ""):
        self.status = status
        safe_message = (message or "").strip()
        if len(safe_message) > 500:
            safe_message = f"{safe_message[:500]}..."
        super().__init__(f"SePay API HTTP {status}: {safe_message}")


class SePayTimeoutError(TimeoutError, SePayError):
    """Raised when SePay does not respond before the configured timeout."""


class SePayResponseError(SePayError):
    """Raised when SePay returns an unexpected payload shape."""


def normalize_order_token(value: Any) -> str:
    """Normalize a bank transfer token/content while preserving separators.

    Vietnamese accents are stripped and letters are uppercased so order codes
    can be matched in bank descriptions that often lose accents/case.
    """

    text = "" if value is None else str(value)
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # NFKD does not decompose Vietnamese đ/Đ.
    without_marks = without_marks.replace("đ", "d").replace("Đ", "D")
    return re.sub(r"\s+", " ", without_marks.strip().upper())


def _token_core(value: Any) -> str:
    normalized = normalize_order_token(value)
    return _NON_ALNUM_RE.sub("", normalized)


def _contains_exact_order_token(content: Any, order_token: Any) -> bool:
    token = _token_core(order_token)
    if not token:
        return False

    normalized_content = normalize_order_token(content)
    token_pattern = r"[^A-Z0-9]*".join(re.escape(ch) for ch in token)
    pattern = re.compile(rf"(?<![A-Z0-9]){token_pattern}(?![A-Z0-9])")
    return bool(pattern.search(normalized_content))


def _to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if value is None:
        return default

    text = str(value).strip()
    if not text:
        return default

    # SePay currency fields are integers, but legacy integrations sometimes
    # keep thousands separators in strings.
    cleaned = re.sub(r"[^\d-]", "", text)
    if not cleaned or cleaned == "-":
        return default
    return int(cleaned)


def _transaction_id(transaction: dict[str, Any]) -> str | None:
    for key in ("id", "transaction_id", "reference_id"):
        value = transaction.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _transaction_content(transaction: dict[str, Any]) -> str:
    for key in ("transaction_content", "content", "description", "addInfo"):
        value = transaction.get(key)
        if value is not None:
            return str(value)
    return ""


def _is_incoming(transaction: dict[str, Any]) -> bool:
    transfer_type = str(transaction.get("transfer_type", "")).strip().lower()
    if transfer_type in {"out", "debit", "withdraw", "withdrawal"}:
        return False
    if transfer_type in {"in", "credit", "deposit"}:
        return True

    amount_in = _to_int(transaction.get("amount_in"))
    amount_out = _to_int(transaction.get("amount_out"))
    amount = _to_int(transaction.get("amount"))
    if amount_in > 0:
        return True
    if amount_out > 0:
        return False
    return amount > 0


def _account_matches(transaction: dict[str, Any], account_number: str | None) -> bool:
    if account_number is None:
        return True
    expected = str(account_number).strip()
    actual = str(transaction.get("account_number", "")).strip()
    return bool(expected) and actual == expected


def extract_transactions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract transaction rows from SePay v2 and legacy-shaped responses."""

    if not isinstance(payload, dict):
        raise SePayResponseError("SePay response must be a JSON object")

    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if isinstance(data, dict):
        for key in ("transactions", "items", "records"):
            transactions = data.get(key)
            if isinstance(transactions, list):
                return [item for item in transactions if isinstance(item, dict)]

    for key in ("transactions", "items", "records"):
        transactions = payload.get(key)
        if isinstance(transactions, list):
            return [item for item in transactions if isinstance(item, dict)]

    raise SePayResponseError("SePay response does not contain a transaction list")


def find_matching_transaction(
    transactions: list[dict[str, Any]],
    order_token: str,
    expected_amount: int,
    account_number: str | None = None,
) -> dict[str, Any] | None:
    """Return the first incoming transaction that exactly matches an order token."""

    minimum_amount = _to_int(expected_amount)
    for transaction in transactions:
        if not isinstance(transaction, dict):
            continue

        stable_id = _transaction_id(transaction)
        if stable_id is None:
            continue

        if not _is_incoming(transaction):
            continue

        amount_in = _to_int(transaction.get("amount_in", transaction.get("amount")))
        if amount_in != minimum_amount:
            continue

        if not _account_matches(transaction, account_number):
            continue

        content = _transaction_content(transaction)
        if not _contains_exact_order_token(content, order_token):
            continue

        return {
            "id": stable_id,
            "amount_in": amount_in,
            "account_number": str(transaction.get("account_number", "")).strip() or None,
            "transaction_content": content,
            "transaction_date": transaction.get("transaction_date"),
            "reference_number": transaction.get("reference_number"),
            "raw": transaction,
        }

    return None


def _validate_digits(name: str, value: Any, min_len: int, max_len: int) -> str:
    text = "" if value is None else str(value).strip()
    if not text.isdigit() or not (min_len <= len(text) <= max_len):
        raise ValueError(f"{name} must be {min_len}-{max_len} digits")
    return text


def build_vietqr_url(
    bank_bin: str,
    account_number: str,
    amount: int,
    add_info: str,
    account_name: str | None = None,
    template: str = "compact2",
) -> str:
    """Build a VietQR image URL with validated path pieces and encoded query."""

    safe_bin = _validate_digits("bank_bin", bank_bin, 3, 12)
    safe_account = _validate_digits("account_number", account_number, 4, 32)

    safe_template = str(template or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", safe_template):
        raise ValueError("template must be 1-32 chars: letters, numbers, _ or -")

    safe_amount = _to_int(amount, default=-1)
    if safe_amount <= 0:
        raise ValueError("amount must be a positive integer")

    safe_add_info = str(add_info or "").strip()
    if not safe_add_info:
        raise ValueError("add_info must not be empty")
    if len(safe_add_info) > 140:
        raise ValueError("add_info must be 140 characters or fewer")

    query: dict[str, str] = {
        "amount": str(safe_amount),
        "addInfo": safe_add_info,
    }

    if account_name is not None and str(account_name).strip():
        query["accountName"] = str(account_name).strip()

    encoded_query = urlencode(query, doseq=False, safe="")
    return f"{VIETQR_BASE_URL}/{safe_bin}-{safe_account}-{safe_template}.png?{encoded_query}"


class SePayClient:
    """Small async client for the official SePay API v2 transactions endpoint."""

    def __init__(
        self,
        api_token: str,
        *,
        base_url: str = SEPAY_TRANSACTIONS_URL,
        timeout_seconds: float = 10,
        session: Any | None = None,
    ):
        token = str(api_token or "").strip()
        if not token:
            raise ValueError("api_token is required")

        self._api_token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return False

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_token}",
        }

    async def list_transactions(self, *, per_page: int = DEFAULT_PER_PAGE, **params: Any) -> list[dict[str, Any]]:
        if self._session is None:
            self._session = aiohttp.ClientSession()

        requested_per_page = _to_int(per_page, default=DEFAULT_PER_PAGE)
        safe_per_page = max(1, min(requested_per_page, MAX_PER_PAGE))
        query_params = {
            key: value
            for key, value in params.items()
            if value is not None and value != ""
        }
        query_params["per_page"] = safe_per_page

        try:
            async with self._session.get(
                self._base_url,
                params=query_params,
                headers=self._headers(),
                timeout=self._timeout,
            ) as response:
                if response.status < 200 or response.status >= 300:
                    try:
                        error_payload = await response.json()
                        message = (
                            error_payload.get("message")
                            or error_payload.get("error")
                            or str(error_payload)
                        )
                    except Exception:
                        message = await response.text()
                    raise SePayHTTPError(response.status, str(message))

                try:
                    payload = await response.json()
                except Exception as exc:
                    raise SePayResponseError("SePay response is not valid JSON") from exc

                return extract_transactions(payload)
        except asyncio.TimeoutError as exc:
            raise SePayTimeoutError("SePay API request timed out") from exc
        except aiohttp.ClientError as exc:
            raise SePayError(f"SePay API request failed: {exc}") from exc

    async def find_matching_transaction(
        self,
        order_token: str,
        expected_amount: int,
        *,
        account_number: str | None = None,
        per_page: int = MAX_PER_PAGE,
        **params: Any,
    ) -> dict[str, Any] | None:
        transactions = await self.list_transactions(
            q=order_token,
            amount_in_min=_to_int(expected_amount),
            amount_in_max=_to_int(expected_amount),
            transfer_type="in",
            per_page=per_page,
            **params,
        )
        return find_matching_transaction(
            transactions,
            order_token=order_token,
            expected_amount=expected_amount,
            account_number=account_number,
        )
