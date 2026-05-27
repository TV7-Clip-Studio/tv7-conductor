from __future__ import annotations

import unittest

from orchestrator.repo_defaults import repo_defaults_for
from orchestrator.tickets import (
    _expansion_quality_score,
    _is_low_quality_expansion,
    _parse_expansion_payload_from_body,
    _prepare_expansion_payload,
    _strip_legacy_issue_metadata,
    _upsert_expansion_block,
)


class ExpandPayloadTests(unittest.TestCase):
    def _item(self) -> dict[str, object]:
        return {
            "item_id": "CO-C01-04-01",
            "title": "Expand seeded issue",
            "tasks": ["Expand seeded issue"],
            "source_file": "docs/roadmaps/core/project-conductor-roadmap.md",
            "source_line": 83,
        }

    def test_upsert_is_idempotent(self) -> None:
        defaults = repo_defaults_for("tv7-core")
        payload, mode = _prepare_expansion_payload(
            item=self._item(),
            repo="tv7-core",
            defaults=defaults,
            raw_payload=None,
            strict_expand=False,
        )
        self.assertEqual(mode, "default")
        original = "## Orchestrator Work Item\n\n- Item ID: `CO-C01-04-01`\n"
        first, changed_first = _upsert_expansion_block(original, payload)
        second, changed_second = _upsert_expansion_block(first, payload)
        self.assertTrue(changed_first)
        self.assertFalse(changed_second)
        self.assertEqual(first, second)

    def test_schema_negative_fallback_and_strict_error(self) -> None:
        defaults = repo_defaults_for("tv7-core")
        invalid = {"context": "", "acceptance_criteria": "not-list"}

        payload, mode = _prepare_expansion_payload(
            item=self._item(),
            repo="tv7-core",
            defaults=defaults,
            raw_payload=invalid,
            strict_expand=False,
        )
        self.assertEqual(mode, "fallback")
        self.assertTrue(payload["context"])

        with self.assertRaises(ValueError):
            _prepare_expansion_payload(
                item=self._item(),
                repo="tv7-core",
                defaults=defaults,
                raw_payload=invalid,
                strict_expand=True,
            )

    def test_strip_legacy_issue_metadata(self) -> None:
        body = (
            "## Orchestrator Work Item\n\n"
            "- Item ID: `CO-C01-04-01`\n"
            "- Stage: `seeded`\n"
            "- Assessment: `queued_antigravity`\n"
            "- Batch ID: `E2E-ASTRO-001`\n"
        )
        cleaned = _strip_legacy_issue_metadata(body)
        self.assertIn("- Item ID:", cleaned)
        self.assertIn("- Batch ID:", cleaned)
        self.assertNotIn("- Stage:", cleaned)
        self.assertNotIn("- Assessment:", cleaned)

    def test_parse_expansion_payload_from_body(self) -> None:
        defaults = repo_defaults_for("tv7-core")
        payload, _mode = _prepare_expansion_payload(
            item=self._item(),
            repo="tv7-core",
            defaults=defaults,
            raw_payload=None,
            strict_expand=False,
        )
        body, _changed = _upsert_expansion_block(
            "## Orchestrator Work Item\n\n- Item ID: `CO-C01-04-01`\n",
            payload,
        )
        parsed = _parse_expansion_payload_from_body(body)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["acceptance_criteria"], payload["acceptance_criteria"])
        self.assertEqual(parsed["validation_commands"], payload["validation_commands"])

    def test_low_quality_expansion_detection(self) -> None:
        high = {
            "context": "This task standardizes marketing page component usage and codifies shared styling patterns in src/pages.",
            "acceptance_criteria": ["A", "B", "C"],
            "validation_commands": ["npm run lint", "npm run build"],
            "risks": ["Visual regressions in CTA cards."],
            "out_of_scope": ["Cross-repo orchestration changes."],
        }
        low = {
            "context": "I've loaded the roadmap context; now I'll inspect files.",
            "acceptance_criteria": [],
            "validation_commands": [],
            "risks": [],
            "out_of_scope": [],
        }
        self.assertFalse(_is_low_quality_expansion(high))
        self.assertTrue(_is_low_quality_expansion(low))
        self.assertGreater(_expansion_quality_score(high), _expansion_quality_score(low))


if __name__ == "__main__":
    unittest.main()
