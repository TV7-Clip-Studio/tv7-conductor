from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator import cli


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_ok_for_minimal_local_checkout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-doctor-") as td:
            root = Path(td)
            state_dir = root / "docs" / ".conductor"
            orch = root / "tooling" / "orchestrator"
            state_dir.mkdir(parents=True)
            orch.mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]\nname='fixture'\n")
            (orch / "workspace.tv7.toml").write_text("[repos]\n")
            (orch / "provider_config.toml").write_text("[providers]\n")

            out = StringIO()
            with (
                patch("orchestrator.cli.shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name == "git" else None),
                redirect_stdout(out),
            ):
                rc = cli.main(
                    [
                        "--source-dir",
                        str(root),
                        "--workspace-dir",
                        str(root),
                        "--conductor-dir",
                        str(root),
                        "--state-dir",
                        str(state_dir),
                        "--workspace-config",
                        str(orch / "workspace.tv7.toml"),
                        "doctor",
                    ]
                )

            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("Conductor doctor:", text)
            self.assertIn("- OK: python:", text)
            self.assertIn("- OK: command:git:", text)
            self.assertIn("optional; not found", text)
            self.assertIn("- next: run conductor-test", text)

    def test_doctor_fails_when_required_git_is_missing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-doctor-") as td:
            root = Path(td)
            state_dir = root / "docs" / ".conductor"
            orch = root / "tooling" / "orchestrator"
            state_dir.mkdir(parents=True)
            orch.mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]\nname='fixture'\n")
            (orch / "workspace.tv7.toml").write_text("[repos]\n")
            (orch / "provider_config.toml").write_text("[providers]\n")

            out = StringIO()
            with (
                patch("orchestrator.cli.shutil.which", return_value=None),
                redirect_stdout(out),
            ):
                rc = cli.main(
                    [
                        "--source-dir",
                        str(root),
                        "--workspace-dir",
                        str(root),
                        "--conductor-dir",
                        str(root),
                        "--state-dir",
                        str(state_dir),
                        "--workspace-config",
                        str(orch / "workspace.tv7.toml"),
                        "doctor",
                    ]
                )

            self.assertEqual(rc, 1)
            self.assertIn("- FAIL: command:git: required; not found", out.getvalue())


if __name__ == "__main__":
    unittest.main()
