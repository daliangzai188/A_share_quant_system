from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from src.secret_config import (
    ensure_tushare_token,
    load_local_env,
    load_tushare_token,
)


class SecretConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {"data_source": {"token_env": "A_SYSTEM_TEST_TOKEN"}}

    def test_loads_token_from_gitignored_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "A_SYSTEM_TEST_TOKEN=local-secret\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("A_SYSTEM_TEST_TOKEN", None)
                token = load_tushare_token(self.config, project_root=root)
            self.assertEqual(token, "local-secret")

    def test_environment_has_priority_over_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "A_SYSTEM_TEST_TOKEN=file-secret\n", encoding="utf-8"
            )
            with mock.patch.dict(
                os.environ, {"A_SYSTEM_TEST_TOKEN": "process-secret"}, clear=False
            ):
                token = load_tushare_token(self.config, project_root=root)
            self.assertEqual(token, "process-secret")

    def test_local_env_supports_quotes_and_never_overrides_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "A_SYSTEM_TEST_TOKEN='quoted-secret'\nSECOND_VALUE=two\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"A_SYSTEM_TEST_TOKEN": "existing"}, clear=False
            ):
                loaded = load_local_env(root)
                self.assertEqual(os.environ["A_SYSTEM_TEST_TOKEN"], "existing")
                self.assertEqual(os.environ["SECOND_VALUE"], "two")
            self.assertEqual(loaded, {"A_SYSTEM_TEST_TOKEN", "SECOND_VALUE"})

    def test_tracked_config_token_is_never_used_as_fallback(self) -> None:
        config = {
            "data_source": {
                "token_env": "A_SYSTEM_TEST_TOKEN",
                "token": "must-not-be-used",
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("A_SYSTEM_TEST_TOKEN", None)
                with self.assertRaisesRegex(RuntimeError, "禁止写入config/config.json"):
                    ensure_tushare_token(
                        config,
                        project_root=Path(tmp),
                        allow_prompt=False,
                    )

    def test_production_config_contains_no_plaintext_token(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = json.loads(
            (root / "config" / "config.json").read_text(encoding="utf-8")
        )
        self.assertFalse(str(config.get("data_source", {}).get("token", "")).strip())


if __name__ == "__main__":
    unittest.main()
