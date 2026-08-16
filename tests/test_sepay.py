import asyncio
import unittest
from urllib.parse import parse_qs, urlparse

from app.services.sepay import (
    SePayClient,
    SePayHTTPError,
    build_vietqr_url,
    extract_transactions,
    find_matching_transaction,
    normalize_order_token,
)


class SePayPureHelperTests(unittest.TestCase):
    def test_extract_transactions_accepts_v2_and_legacy_shapes(self):
        v2_response = {
            "status": "success",
            "data": [{"id": "uuid-1", "amount_in": 100_000}],
            "meta": {"pagination": {"per_page": 20}},
        }
        legacy_response = {
            "code": 200,
            "data": {"transactions": [{"id": 987, "amount_in": "120000"}]},
        }
        bare_legacy_response = {"transactions": [{"transaction_id": "legacy-id"}]}

        self.assertEqual(extract_transactions(v2_response), v2_response["data"])
        self.assertEqual(
            extract_transactions(legacy_response),
            legacy_response["data"]["transactions"],
        )
        self.assertEqual(
            extract_transactions(bare_legacy_response),
            bare_legacy_response["transactions"],
        )

    def test_normalize_order_token_removes_accents_and_uppercases(self):
        self.assertEqual(normalize_order_token(" Đơn-Hàng 42 "), "DON-HANG 42")

    def test_find_matching_transaction_requires_exact_token_boundaries(self):
        transactions = [
            {
                "id": "too-small",
                "transfer_type": "in",
                "amount_in": 49_000,
                "transaction_content": "Thanh toan PKGOLD42",
                "account_number": "123456789",
            },
            {
                "id": "embedded-token",
                "transfer_type": "in",
                "amount_in": 99_000,
                "transaction_content": "Thanh toan X PKGOLD42Y",
                "account_number": "123456789",
            },
            {
                "id": "match-1",
                "transfer_type": "in",
                "amount_in": "99000",
                "transaction_content": "Thanh toán gói PK-GOLD-42 cho khách",
                "account_number": "123456789",
            },
        ]

        match = find_matching_transaction(
            transactions,
            order_token="PK GOLD 42",
            expected_amount=99_000,
            account_number="123456789",
        )

        self.assertIsNotNone(match)
        self.assertEqual(match["id"], "match-1")
        self.assertEqual(match["amount_in"], 99_000)
        self.assertEqual(match["transaction_content"], transactions[2]["transaction_content"])

    def test_find_matching_transaction_rejects_outgoing_wrong_account_and_missing_id(self):
        transactions = [
            {
                "id": "outgoing",
                "transfer_type": "out",
                "amount_in": 120_000,
                "transaction_content": "ORDER777",
                "account_number": "999",
            },
            {
                "id": "wrong-account",
                "transfer_type": "in",
                "amount_in": 120_000,
                "transaction_content": "ORDER777",
                "account_number": "888",
            },
            {
                "transfer_type": "in",
                "amount_in": 120_000,
                "transaction_content": "ORDER777",
                "account_number": "999",
            },
        ]

        self.assertIsNone(
            find_matching_transaction(
                transactions,
                order_token="ORDER777",
                expected_amount=100_000,
                account_number="999",
            )
        )

    def test_find_matching_transaction_requires_exact_amount(self):
        transactions = [{
            "id": "overpaid",
            "transfer_type": "in",
            "amount_in": 100_001,
            "transaction_content": "ORDER999",
            "account_number": "123456789",
        }]

        self.assertIsNone(find_matching_transaction(
            transactions,
            order_token="ORDER999",
            expected_amount=100_000,
            account_number="123456789",
        ))

    def test_build_vietqr_url_validates_and_encodes_inputs(self):
        url = build_vietqr_url(
            bank_bin="970422",
            account_number="1234567890",
            amount=99_000,
            add_info="PK GOLD 42 / user@example.com",
            account_name="LOCKET SHOP",
        )

        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "img.vietqr.io")
        self.assertEqual(parsed.path, "/image/970422-1234567890-compact2.png")
        self.assertEqual(query["amount"], ["99000"])
        self.assertEqual(query["addInfo"], ["PK GOLD 42 / user@example.com"])
        self.assertEqual(query["accountName"], ["LOCKET SHOP"])

        with self.assertRaises(ValueError):
            build_vietqr_url("9704xx", "1234567890", 99_000, "ORDER1")
        with self.assertRaises(ValueError):
            build_vietqr_url("970422", "1234-567", 99_000, "ORDER1")
        with self.assertRaises(ValueError):
            build_vietqr_url("970422", "1234567890", 99_000, "")


class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status = status
        self._payload = payload
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def text(self):
        return self._text


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


class SePayClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_transactions_uses_official_endpoint_bearer_and_caps_per_page(self):
        response = FakeResponse(
            200,
            {
                "status": "success",
                "data": [{"id": "uuid-1", "amount_in": 100_000}],
            },
        )
        session = FakeSession(response)
        client = SePayClient("secret-token", session=session)

        transactions = await client.list_transactions(per_page=250, q="ORDER1")

        self.assertEqual(transactions, [{"id": "uuid-1", "amount_in": 100_000}])
        call = session.calls[0]
        self.assertEqual(call["url"], "https://userapi.sepay.vn/v2/transactions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret-token")
        self.assertEqual(call["params"]["per_page"], 100)
        self.assertEqual(call["params"]["q"], "ORDER1")
        self.assertIsInstance(call["timeout"].total, (int, float))

    async def test_find_matching_transaction_fetches_incoming_filtered_transactions(self):
        response = FakeResponse(
            200,
            {
                "status": "success",
                "data": [
                    {
                        "id": "uuid-2",
                        "transfer_type": "in",
                        "amount_in": 99_000,
                        "transaction_content": "LOCKET-ABC-123",
                    }
                ],
            },
        )
        session = FakeSession(response)
        client = SePayClient("secret-token", session=session)

        match = await client.find_matching_transaction("LOCKET ABC 123", 99_000)

        self.assertEqual(match["id"], "uuid-2")
        call = session.calls[0]
        self.assertEqual(call["params"]["transfer_type"], "in")
        self.assertEqual(call["params"]["amount_in_min"], 99_000)
        self.assertEqual(call["params"]["amount_in_max"], 99_000)
        self.assertEqual(call["params"]["q"], "LOCKET ABC 123")

    async def test_list_transactions_raises_http_error_without_exposing_token(self):
        response = FakeResponse(401, {"message": "Unauthorized"}, text="Unauthorized")
        session = FakeSession(response)
        client = SePayClient("super-secret-token", session=session)

        with self.assertRaises(SePayHTTPError) as ctx:
            await client.list_transactions()

        self.assertEqual(ctx.exception.status, 401)
        self.assertNotIn("super-secret-token", str(ctx.exception))

    async def test_list_transactions_wraps_timeout(self):
        class TimeoutSession:
            def get(self, *_args, **_kwargs):
                raise asyncio.TimeoutError()

        client = SePayClient("secret-token", session=TimeoutSession())

        with self.assertRaises(TimeoutError):
            await client.list_transactions()


if __name__ == "__main__":
    unittest.main()
