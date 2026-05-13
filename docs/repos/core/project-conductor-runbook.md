# Project Conductor Runbook

## Purpose
Run deterministic ticket planning and ticket syncing for TV7 repos using Project Conductor.

## Governance References
- Charter: `docs/repos/core/project-conductor-charter.md`
- Phase gates: `docs/repos/core/project-conductor-phase-gates.md`
- Failure policy: `docs/repos/core/project-conductor-failure-policy.md`
- Policy config: `tooling/orchestrator/conductor_policy.toml`
- Execution sandbox policy: `tooling/orchestrator/execution_policy.toml`
- Review loop policy: `tooling/orchestrator/review_policy.toml`

## Prerequisites
- Python 3.11+
- `gh` authenticated (`gh auth status`)
- `gemini` and/or `codex` CLI available (depending on selected profile)
- Workspace layout rooted at `TV7/` with sibling repos

## State Location
- Runtime state: `docs/.conductor/state.json`
- Events log: `docs/.conductor/events.jsonl`
- Iteration artifacts: `docs/.conductor/runs/<iteration_id>/`
- Ticket sync reports: `docs/.conductor/reports/ticket-sync-*.json|.md`
- Planning artifacts: `state.planning.idea_runs` and `state.planning.intent_queue`
- Execution artifacts: `state.agent_runs[]` entries with `run_type=execute_handoff` and run logs under `docs/.conductor/runs/execute-*/`
- Local review artifacts: `docs/.conductor/reports/local-review/`, `docs/.conductor/runs/local-review-*/`, and `work_items[].review_feedback.local_review`
- External review artifacts: `work_items[].review_feedback` (normalized retry payloads + findings), `work_items[].review_verdict`
- Repo defaults config: `tooling/orchestrator/repo_defaults.toml`

## Commands
```bash
cd tv7-core
make conductor-init
make conductor-plan
make conductor-graph-plan    # dry-run IDEA ingestion + queue emit (+ expand handoff dry-run)
make conductor-graph-plan-apply
make conductor-tickets        # dry-run issue sync
make conductor-tickets-apply  # create/link issues in GitHub
make conductor-expand         # dry-run issue enrichment (expand stage)
make conductor-expand-apply   # apply expansion + stage label transition
make conductor-expand-label-check
make conductor-execute        # dry-run execution bridge (ao spawn)
make conductor-execute-apply
make conductor-local-review   # dry-run local validation/review gate
make conductor-external-review # dry-run external DeepSeek/GitHub review gate for PR-stage items
make conductor-review          # compatibility alias for conductor-external-review
make conductor-status
make conductor-ops-status     # AO session board + actionable hints
make conductor-events
```

Single-repo run:
```bash
cd tv7-core
make conductor-run CONDUCTOR_TICKET_REPO=tv7-astro-site
```

Roadmap-scoped run:
```bash
cd tv7-core
make conductor-run CONDUCTOR_ROADMAP_FILE=docs/roadmaps/tv7-astro-site-roadmap.md
```

All-repo run:
```bash
cd tv7-core
make conductor-run
```

Ground-zero cleanup:
```bash
cd tv7-core
make conductor-tickets-reset-scan      # dry-run close scan
make conductor-tickets-reset-scan-apply
rm -rf docs/.conductor
make conductor-init
```

## Ticket labels (fixed base set)
Every created/linked issue gets:
- `conductor`
- `type:task`
- `source:roadmap`
- `repo:<repo-name>`
- `batch:<batch-id>` (when provided)
- `stage:seed`
- Optional repo defaults labels from `repo_defaults.toml`

Label assertion helper:
```bash
cd tv7-core
python3 tooling/orchestrator/verify_seed_labels.py \
  --repo TV7-Clip-Studio/tv7-core \
  --issue <issue-number-or-url> \
  --target-repo tv7-core \
  --batch-id <batch-id>
```

## Scope Filters
Use any combination of:
- `CONDUCTOR_TICKET_REPO=tv7-...`
- `CONDUCTOR_TICKET_ROADMAP=docs/roadmaps/<repo-roadmap>.md`
- `CONDUCTOR_TICKET_ITEM_PREFIX=<REPO-BATCH|REPO-BATCH-EPIC>`

Examples:
```bash
cd tv7-core
make conductor-tickets-apply CONDUCTOR_TICKET_REPO=tv7-fastapi-backend
make conductor-tickets-apply CONDUCTOR_TICKET_ROADMAP=docs/roadmaps/tv7-fastapi-backend-roadmap.md
make conductor-tickets-apply CONDUCTOR_TICKET_ITEM_PREFIX=PY-A01-02
```

## Automation Levels
Workspace repo profiles declare `automation_level` in `tooling/orchestrator/workspace.tv7.toml`.

Order:
```text
observe < plan < apply < execute < review < integrate
```

Apply-mode commands enforce the configured level for known repos:
- ticket/expand apply require `apply`
- execute apply requires `execute`
- local/external review apply requires `review`

Dry-run commands remain available below those levels.

## Operator Summaries
Conductor stage commands print an `Operator summary` after the headline result. The block includes:
- stage and mode
- outcome counts
- report paths
- risks surfaced by the command
- next recommended operator action

Treat the summary as the handoff between commands; inspect the linked report before any apply-mode follow-up.

## Notes
- `make conductor-run` executes `plan` then `tickets` in one command.
- `CONDUCTOR_RUN_FLAGS` is a direct passthrough to `tooling/conductor_run.py`.
- `CONDUCTOR_ROADMAP_FILE` maps directly to `--roadmap-file` in `conductor-run`.
- Assessment is disabled by default in `conductor-run`; enable with `CONDUCTOR_ASSESS=1`.
- `make conductor-expand` supports `CONDUCTOR_EXPAND_LIMIT`, `CONDUCTOR_EXPAND_RESUME_FROM`, and `CONDUCTOR_EXPAND_DRY_RUN_DIFF=1`.
- `make conductor-graph-plan` reads `CONDUCTOR_IDEA_FILE` (default `tv7-core/IDEA.md`) and supports `CONDUCTOR_GRAPH_HANDOFF=0` to skip expand handoff.
- `make conductor-execute` supports `CONDUCTOR_EXECUTE_LIMIT`, `CONDUCTOR_EXECUTE_RESUME_FROM`, `CONDUCTOR_EXECUTE_FORCE=1`, `CONDUCTOR_AO_COMMAND`, and optional `CONDUCTOR_EXECUTE_POLICY=<path>`.
- `make conductor-local-review` supports `CONDUCTOR_REPO`, optional `CONDUCTOR_ITEM_ID`, optional `CONDUCTOR_WORKTREE_PATH`, `CONDUCTOR_LOCAL_REVIEW_COMMAND`, and `CONDUCTOR_APPLY=1`.
- `local_review` is separate by default. Passing local review promotes an item to `pr_ready`; `pr_ready` means the operator may open or update a PR.
- `make conductor-ops-status` surfaces live AO sessions plus operator hints; supports `CONDUCTOR_AO_COMMAND` and optional `CONDUCTOR_TICKET_REPO=<repo>` filter.
- `make conductor-external-review` supports `CONDUCTOR_REVIEW_PROVIDER`, `CONDUCTOR_REVIEW_ALLOW_PAID=1`, `CONDUCTOR_REVIEW_MAX_LOOPS`, optional `CONDUCTOR_ITEM_ID`, and `CONDUCTOR_APPLY=1`. Treat it as the external review gate after local review and operator PR action.
- `make conductor-review` remains a compatibility alias for `make conductor-external-review`.
- Provider config lives in `tooling/orchestrator/provider_config.toml`. `tickets` and `expand` resolve planner defaults, `execute` resolves an AO executor provider, `local-review` resolves a local command reviewer, and external API review requires an explicit paid-review opt-in.
