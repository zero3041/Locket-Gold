import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config


class TokenSetConfigTests(unittest.TestCase):
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

    def test_token_set_requires_boolean_is_sandbox(self):
        missing = '[{"fetch_token":"f","app_transaction":"a"}]'
        string_value = '[{"fetch_token":"f","app_transaction":"a","is_sandbox":"false"}]'

        for raw in (missing, string_value):
            with self.subTest(raw=raw), patch.dict(os.environ, {"TOKEN_SETS_JSON": raw}):
                with self.assertRaises(RuntimeError):
                    config._load_token_sets()

    def test_valid_token_set_is_loaded_without_mutation(self):
        raw = '[{"fetch_token":"f","app_transaction":"a","is_sandbox":false}]'
        with patch.dict(os.environ, {"TOKEN_SETS_JSON": raw}):
            loaded = config._load_token_sets()

        self.assertEqual(
            [{"fetch_token": "f", "app_transaction": "a", "is_sandbox": False}],
            loaded,
        )


if __name__ == "__main__":
    unittest.main()
