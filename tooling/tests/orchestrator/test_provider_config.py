from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from orchestrator.provider_config import load_provider_config, provider_for_role


class ProviderConfigTests(unittest.TestCase):
    def test_default_provider_roles_are_resolved(self) -> None:
        config = load_provider_config()

        planner = provider_for_role(config, "planner")
        executor = provider_for_role(config, "executor")
        reviewer = provider_for_role(config, "reviewer")

        self.assertEqual(planner.name, "gemini_cli_planner")
        self.assertEqual(planner.kind, "local_command")
        self.assertEqual(executor.name, "ao_codex_executor")
        self.assertEqual(executor.kind, "ao")
        self.assertEqual(reviewer.name, "local_command_reviewer")
        self.assertFalse(reviewer.paid)

    def test_paid_reviewer_requires_explicit_allowance(self) -> None:
        config = load_provider_config()

        with self.assertRaises(PermissionError):
            provider_for_role(config, "reviewer", name="deepseek_api_reviewer")

        provider = provider_for_role(
            config,
            "reviewer",
            name="deepseek_api_reviewer",
            allow_paid=True,
        )
        self.assertTrue(provider.paid)
        self.assertEqual(provider.kind, "api")

    def test_default_provider_role_must_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-provider-config-") as td:
            path = Path(td) / "providers.toml"
            path.write_text(
                """
[providers.bad_executor]
role = "executor"
kind = "ao"

[providers.good_planner]
role = "planner"
kind = "local_command"

[providers.good_reviewer]
role = "reviewer"
kind = "local_command"

[defaults]
planner = "bad_executor"
executor = "bad_executor"
reviewer = "good_reviewer"
""".strip()
                + "\n"
            )

            with self.assertRaisesRegex(ValueError, "default provider for planner has role executor"):
                load_provider_config(path)


if __name__ == "__main__":
    unittest.main()
