from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from orchestrator.state_migration import compare_state_roots


class StateMigrationTests(unittest.TestCase):
    def test_compare_state_roots_reports_item_differences(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-state-migration-") as td:
            root = Path(td)
            legacy = root / "legacy"
            active = root / "active"
            legacy.mkdir()
            active.mkdir()
            (legacy / "state.json").write_text(json.dumps({"work_items": [{"item_id": "A"}], "agent_runs": [{}]}) + "\n")
            (active / "state.json").write_text(json.dumps({"work_items": [{"item_id": "B"}], "agent_runs": []}) + "\n")

            summary = compare_state_roots(legacy, active)

            self.assertTrue(summary["legacy_exists"])
            self.assertTrue(summary["active_exists"])
            self.assertFalse(summary["same_digest"])
            self.assertEqual(summary["legacy_work_items"], 1)
            self.assertEqual(summary["active_work_items"], 1)
            self.assertEqual(summary["only_legacy_item_ids"], ["A"])
            self.assertEqual(summary["only_active_item_ids"], ["B"])


if __name__ == "__main__":
    unittest.main()
