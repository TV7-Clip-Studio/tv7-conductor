from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from orchestrator.state_store import load_state, paths


class StateMigrationTests(unittest.TestCase):
    def test_load_migrates_v1_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-conductor-state-") as td:
            core_dir = Path(td)
            state_dir = core_dir / "docs" / ".conductor"
            state_dir.mkdir(parents=True, exist_ok=True)

            legacy = {
                "version": 1,
                "updated_at": "2026-02-01T00:00:00+00:00",
                "settings": {"auto_push": True},
                "work_items": [
                    {
                        "item_id": "CO-C01-02-01",
                        "title": "Example",
                        "tasks": ["Example"],
                        "repos": ["tv7-core"],
                        "status": "READY",
                        "source_file": "docs/roadmaps/core/project-conductor-roadmap.md",
                        "source_line": 42,
                        "issues": {},
                        "created_at": "2026-02-01T00:00:00+00:00",
                        "updated_at": "2026-02-01T00:00:00+00:00",
                        "last_error": "legacy error",
                    }
                ],
                "iterations": [],
                "agent_runs": [],
            }
            (state_dir / "state.json").write_text(json.dumps(legacy, indent=2) + "\n")
            (state_dir / "events.jsonl").write_text("")

            prev = os.environ.get("TV7_STATE_ROOT_NAME")
            os.environ["TV7_STATE_ROOT_NAME"] = ".conductor"
            try:
                state = load_state(core_dir)
            finally:
                if prev is None:
                    os.environ.pop("TV7_STATE_ROOT_NAME", None)
                else:
                    os.environ["TV7_STATE_ROOT_NAME"] = prev

            self.assertEqual(state["version"], 3)
            self.assertEqual(state["schema"]["version"], 3)
            self.assertIn("idempotency", state)
            self.assertIn("planning", state)
            self.assertIn("intent_queue", state["planning"])
            self.assertEqual(state["work_items"][0]["stage"], "seeded")
            self.assertEqual(state["work_items"][0]["errors"], ["legacy error"])

            persisted = json.loads((state_dir / "state.json").read_text())
            self.assertEqual(persisted["version"], 3)
            self.assertIn("schema", persisted)
            self.assertIn("idempotency", persisted)
            self.assertIn("planning", persisted)

    def test_explicit_state_dir_overrides_core_docs_storage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-conductor-state-") as td:
            root = Path(td)
            core_dir = root / "repo"
            state_dir = root / "conductor-state"
            core_dir.mkdir()

            prev = os.environ.get("TV7_CONDUCTOR_STATE_DIR")
            os.environ["TV7_CONDUCTOR_STATE_DIR"] = str(state_dir)
            try:
                state = load_state(core_dir)
                resolved = paths(core_dir)
            finally:
                if prev is None:
                    os.environ.pop("TV7_CONDUCTOR_STATE_DIR", None)
                else:
                    os.environ["TV7_CONDUCTOR_STATE_DIR"] = prev

            self.assertEqual(state["version"], 3)
            self.assertEqual(resolved["root"], state_dir.resolve())
            self.assertTrue((state_dir / "state.json").exists())
            self.assertFalse((core_dir / "docs" / ".conductor" / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
