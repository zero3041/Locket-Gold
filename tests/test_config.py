import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config


class DotenvTests(unittest.TestCase):
    def test_dotenv_restores_local_values_without_overriding_process_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            dotenv.write_text(
                "LOCAL_ONLY=from-file\n"
                "PROCESS_WINS=from-file\n"
                "JSON_VALUE='[{\"is_sandbox\":false}]'\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"PROCESS_WINS": "from-process"}, clear=True):
                config._load_dotenv(dotenv)
                self.assertEqual("from-file", os.environ["LOCAL_ONLY"])
                self.assertEqual("from-process", os.environ["PROCESS_WINS"])
                self.assertEqual('[{"is_sandbox":false}]', os.environ["JSON_VALUE"])


class PlanPriceTests(unittest.TestCase):
    def test_price_for_plan_defaults_to_monthly_price(self):
        with patch.object(config, "CDK_UNIT_PRICE", 10000), patch.object(config, "CDK_UNIT_PRICE_1Y", 0):
            self.assertEqual(10000, config.price_for_plan("1m"))
            self.assertEqual(10000, config.price_for_plan(None))

    def test_price_for_plan_uses_yearly_price(self):
        with patch.object(config, "CDK_UNIT_PRICE_1Y", 50000):
            self.assertEqual(config.CDK_UNIT_PRICE_1Y, config.price_for_plan("1y"))
            self.assertEqual(50000, config.price_for_plan("1y"))

    def test_plan_labels(self):
        self.assertEqual("Gói Vĩnh Viễn", config.plan_label("1m", "VI"))
        self.assertEqual("Gói 1 Năm", config.plan_label("1y", "VI"))
        self.assertEqual("Permanent Plan", config.plan_label("1m", "EN"))
        self.assertEqual("1-Year Plan", config.plan_label("1y", "EN"))


class PaymentConfigTests(unittest.TestCase):
    def _patch(self, **overrides):
        values = {
            "SEPAY_API_TOKEN": "token",
            "BANK_BIN": "970422",
            "BANK_ACCOUNT": "1234567890",
            "BANK_NAME": "MB",
            "BANK_OWNER": "OWNER",
            "CDK_SECRET": "x" * 40,
            "CDK_UNIT_PRICE": 10000,
            "CDK_UNIT_PRICE_1Y": 50000,
        }
        values.update(overrides)
        return patch.multiple(config, **values)

    def test_complete_configuration_has_no_errors(self):
        with self._patch():
            self.assertEqual([], config.payment_config_errors())

    def test_missing_yearly_price_is_reported(self):
        with self._patch(CDK_UNIT_PRICE_1Y=0):
            self.assertIn("CDK_UNIT_PRICE_1Y", config.payment_config_errors())

    def test_short_secret_is_reported(self):
        with self._patch(CDK_SECRET="short"):
            errors = config.payment_config_errors()
            self.assertTrue(any(err.startswith("CDK_SECRET") for err in errors))

    def test_invalid_bank_account_is_reported(self):
        with self._patch(BANK_ACCOUNT="12ab"):
            self.assertIn("BANK_ACCOUNT(digits_only)", config.payment_config_errors())


class TextTests(unittest.TestCase):
    def test_every_language_has_the_same_keys(self):
        vi_keys = set(config.TEXTS["VI"])
        en_keys = set(config.TEXTS["EN"])
        self.assertEqual(set(), vi_keys ^ en_keys)

    def test_missing_key_falls_back_to_key_name(self):
        self.assertEqual("__nope__", config.T("__nope__", "VI"))


if __name__ == "__main__":
    unittest.main()
