from __future__ import annotations

from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import json
import tempfile
import unittest

from orchestrator import cli


class CliPortabilityTests(unittest.TestCase):
    def test_plan_and_status_work_against_fixture_repo_without_tv7_core(self) -> None:
        with tempfile.TemporaryDirectory(prefix="conductor-fixture-") as td:
            root = Path(td)
            fixture = root / "example-repo"
            state_dir = root / ".conductor"
            roadmap = fixture / "ROADMAP.md"
            fixture.mkdir()
            roadmap.write_text(
                "\n".join(
                    [
                        "# Example Roadmap",
                        "Roadmap Code: EX",
                        "Batch Code: A01",
                        "",
                        "## Phase 1",
                        "- [ ] [EX-A01-01-01] Add fixture task",
                    ]
                )
                + "\n"
            )

            out = StringIO()
            with redirect_stdout(out):
                rc = cli.main(
                    [
                        "--core-dir",
                        str(fixture),
                        "--workspace-dir",
                        str(root),
                        "--conductor-dir",
                        str(root / "conductor"),
                        "--state-dir",
                        str(state_dir),
                        "plan",
                        "--single-roadmap",
                        "--roadmap",
                        str(roadmap),
                        "--default-repos",
                        "example-repo",
                    ]
                )

            self.assertEqual(rc, 0)
            self.assertIn("Operator summary:", out.getvalue())
            self.assertIn("- next: run status", out.getvalue())
            state = json.loads((state_dir / "state.json").read_text())
            self.assertEqual(state["work_items"][0]["item_id"], "EX-A01-01-01")
            self.assertEqual(state["work_items"][0]["repos"], ["example-repo"])

            with redirect_stdout(StringIO()):
                rc = cli.main(
                    [
                        "--core-dir",
                        str(fixture),
                        "--workspace-dir",
                        str(root),
                        "--conductor-dir",
                        str(root / "conductor"),
                        "--state-dir",
                        str(state_dir),
                        "status",
                    ]
                )

            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
