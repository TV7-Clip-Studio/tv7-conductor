from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.planning_pipeline import ingest_idea_records, run_planning_pipeline


class PlanningPipelineTests(unittest.TestCase):
    def test_ingest_idea_records_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-idea-") as td:
            idea_file = Path(td) / "IDEA.md"
            idea_file.write_text(
                "\n".join(
                    [
                        "# IDEA",
                        "- [CO-C01-05-11] Explicit item repos=tv7-core,tv7-astro-site batch=C01 epic=05",
                        "- Queue bridge repo=tv7-core epic=05",
                        "- Site docs touchpoint for tv7-astro-site batch=C01 epic=06",
                        "  - subtask should be ignored",
                    ]
                )
                + "\n"
            )

            first = ingest_idea_records(
                idea_file=idea_file,
                default_repo="tv7-core",
                roadmap_code="CO",
                batch_code="C01",
                default_epic="05",
            )
            second = ingest_idea_records(
                idea_file=idea_file,
                default_repo="tv7-core",
                roadmap_code="CO",
                batch_code="C01",
                default_epic="05",
            )

            self.assertEqual([x.to_dict() for x in first], [x.to_dict() for x in second])
            self.assertEqual([x.item_id for x in first], ["CO-C01-05-11", "CO-C01-05-01", "CO-C01-06-01"])
            self.assertEqual(first[0].repos, ["tv7-astro-site", "tv7-core"])
            self.assertEqual(first[1].repos, ["tv7-core"])
            self.assertEqual(first[2].repos, ["tv7-astro-site"])

    def test_queue_parity_matches_repo_targets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-idea-") as td:
            core_dir = Path(td)
            idea_file = core_dir / "IDEA.md"
            idea_file.write_text(
                "\n".join(
                    [
                        "- Build router contract repos=tv7-core,tv7-astro-site batch=C01 epic=05",
                        "- Add retry note repo=tv7-core batch=C01 epic=05",
                    ]
                )
                + "\n"
            )

            state = {"work_items": []}
            summary, _actions = run_planning_pipeline(
                core_dir=core_dir,
                state=state,
                idea_file=idea_file,
                default_repo="tv7-core",
                roadmap_code="CO",
                batch_code="C01",
                default_epic="05",
                owner="TV7-Clip-Studio",
                apply_changes=True,
                handoff_expand=False,
                strict_expand=False,
                batch_id="20260228-001",
                progress=False,
                limit=0,
                resume_from="",
                dry_run_diff=False,
            )

            queue = state["planning"]["intent_queue"]
            self.assertEqual(summary["ingested"], 2)
            self.assertEqual(summary["queue_records"], 3)
            self.assertEqual(len(queue), 3)

            repo_target_count = sum(len(item.get("repos", [])) for item in state["work_items"])
            self.assertEqual(repo_target_count, len(queue))

            known_items = {str(item.get("item_id", "")) for item in state["work_items"]}
            queued_items = {str(entry.get("item_id", "")) for entry in queue}
            self.assertEqual(queued_items, known_items)

    def test_expand_handoff_contract_passes_resume_controls(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tv7-idea-") as td:
            core_dir = Path(td)
            idea_file = core_dir / "IDEA.md"
            idea_file.write_text(
                "\n".join(
                    [
                        "- First handoff target repo=tv7-core batch=C01 epic=01",
                        "- Second handoff target repo=tv7-core batch=C01 epic=01",
                    ]
                )
                + "\n"
            )

            state = {"work_items": []}
            captured: dict[str, object] = {}

            def fake_expand_repo_issues(**kwargs):
                captured.update(kwargs)
                return (
                    1,
                    0,
                    0,
                    0,
                    2,
                    2,
                    [
                        {
                            "action": "would_expand",
                            "item_id": "CO-C01-01-02",
                            "repo": "tv7-core",
                            "title": "Second handoff target",
                            "url": "https://example.test/2",
                            "note": "fallback",
                        }
                    ],
                )

            with patch("orchestrator.planning_pipeline.expand_repo_issues", side_effect=fake_expand_repo_issues):
                summary, _actions = run_planning_pipeline(
                    core_dir=core_dir,
                    state=state,
                    idea_file=idea_file,
                    default_repo="tv7-core",
                    roadmap_code="CO",
                    batch_code="C01",
                    default_epic="01",
                    owner="TV7-Clip-Studio",
                    apply_changes=False,
                    handoff_expand=True,
                    strict_expand=True,
                    batch_id="20260228-001",
                    progress=False,
                    limit=1,
                    resume_from="CO-C01-01-02",
                    dry_run_diff=True,
                )

            self.assertEqual(captured["owner"], "TV7-Clip-Studio")
            self.assertEqual(captured["limit"], 1)
            self.assertEqual(captured["resume_from"], "CO-C01-01-02")
            self.assertEqual(captured["repo_filters"], ["tv7-core"])
            self.assertEqual(captured["item_prefix_filters"], ["CO-C01-01-01", "CO-C01-01-02"])
            self.assertTrue(bool(captured["strict_expand"]))
            self.assertTrue(bool(captured["dry_run_diff"]))

            handoff = summary["handoff"]
            self.assertEqual(handoff["expanded"], 1)
            self.assertEqual(handoff["repo_targets"], 2)

            queue_by_id = {entry["intent_id"]: entry for entry in state["planning"]["intent_queue"]}
            self.assertEqual(queue_by_id["CO-C01-01-02:tv7-core"]["status"], "handoff_planned")
            self.assertEqual(queue_by_id["CO-C01-01-01:tv7-core"]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
