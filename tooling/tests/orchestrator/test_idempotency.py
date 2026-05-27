from __future__ import annotations

import unittest

from orchestrator.idempotency import has_idempotency_key, make_idempotency_key, record_idempotency_key


class IdempotencyTests(unittest.TestCase):
    def test_key_format(self) -> None:
        key = make_idempotency_key(item_id="CO-C01-02-04", repo="tv7-core", batch_id="", stage="seeded")
        self.assertEqual(key, "CO-C01-02-04:tv7-core:none:seeded")

    def test_record_and_check(self) -> None:
        state = {}
        key = make_idempotency_key(
            item_id="CO-C01-02-04",
            repo="tv7-core",
            batch_id="20260228-001",
            stage="seeded",
        )
        self.assertFalse(has_idempotency_key(state, key))
        record_idempotency_key(
            state,
            key=key,
            item_id="CO-C01-02-04",
            repo="tv7-core",
            batch_id="20260228-001",
            stage="seeded",
            action="created",
        )
        self.assertTrue(has_idempotency_key(state, key))


if __name__ == "__main__":
    unittest.main()
