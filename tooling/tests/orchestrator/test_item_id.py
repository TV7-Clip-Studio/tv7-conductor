from __future__ import annotations

import unittest

from orchestrator.item_id import build_item_id, parse_item_id


class ItemIdTests(unittest.TestCase):
    def test_parse_valid_item_id(self) -> None:
        parsed = parse_item_id("CO-C01-02-04")
        self.assertEqual(parsed.repo, "CO")
        self.assertEqual(parsed.batch, "C01")
        self.assertEqual(parsed.epic, "02")
        self.assertEqual(parsed.ticket, "04")

    def test_build_item_id(self) -> None:
        self.assertEqual(
            build_item_id(repo_code="co", batch_code="c01", epic_code="02", ticket_code="04"),
            "CO-C01-02-04",
        )

    def test_rejects_malformed_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "must match `REPO-BATCH-EPIC-TICKET`"):
            parse_item_id("CO-01")

    def test_rejects_unknown_repo_code(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown repo code"):
            parse_item_id("ZZ-C01-02-04")

    def test_rejects_repo_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "repo code mismatch"):
            parse_item_id("CO-C01-02-04", expected_repo_code="PY")


if __name__ == "__main__":
    unittest.main()
