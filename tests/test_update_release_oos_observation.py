from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.update_release_oos_observation import unfrozen_skip_payload


class UpdateReleaseOosObservationTest(unittest.TestCase):
    def test_unfrozen_release_is_cleanly_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir(parents=True)
            (root / "config" / "strategy_release_freeze.json").write_text(
                json.dumps({
                    "status": "UNFROZEN_PENDING_CERTIFICATION",
                    "release_id": "portfolio-a-c-e-d-pending",
                }),
                encoding="utf-8",
            )
            payload = unfrozen_skip_payload(root, "20260824")
            self.assertIsNotNone(payload)
            self.assertEqual(payload["status"], "UNFROZEN_SKIPPED")
            self.assertFalse(payload["trading_side_effects"])

    def test_frozen_release_continues_to_oos_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir(parents=True)
            (root / "config" / "strategy_release_freeze.json").write_text(
                json.dumps({"status": "FROZEN", "release_id": "release-1"}),
                encoding="utf-8",
            )
            self.assertIsNone(unfrozen_skip_payload(root, "20260824"))


if __name__ == "__main__":
    unittest.main()
