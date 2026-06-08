# Project Conductor Runtime Runbook

## Purpose
Operate the standalone Conductor runtime directly from `tv7-conductor`.

For normal TV7 operator entrypoints, use `tv7-core` `make conductor-*` shims.
Use this runbook when working inside the runtime repo, preparing isolated worktrees, running local review, launching previews, or validating Conductor itself.

## Runtime Ownership
`tv7-conductor` owns:
- CLI entrypoints: `tooling/conductor.py`, `tooling/conductor_run.py`
- runtime modules and tests under `tooling/orchestrator/`
- provider, workspace, roadmap ID, execution, and review config
- active state and reports under `docs/.conductor/`
- isolated worktree/preview/local-review helper commands

`tv7-core` owns:
- canonical TV7 roadmap files except `tv7-conductor/docs/roadmaps/tv7-conductor-roadmap.md`
- governance docs and compatibility runbook
- shim targets that pass explicit paths into this runtime

## State And Config
Active state:
- `docs/.conductor/state.json`
- `docs/.conductor/events.jsonl`
- `docs/.conductor/runs/`
- `docs/.conductor/reports/`

Runtime config:
- `tooling/orchestrator/workspace.tv7.toml`
- `tooling/orchestrator/roadmap_ids.toml`
- `tooling/orchestrator/repo_defaults.toml`
- `tooling/orchestrator/provider_config.toml`
- `tooling/orchestrator/execution_policy.toml`
- `tooling/orchestrator/review_policy.toml`

Historical pre-extraction state may exist at `../tv7-core/docs/.conductor`.
Compare it with:

```bash
make conductor-state-migration-check
```

## Setup
```bash
cd tv7-conductor
make install-dev
make conductor-doctor
make conductor-test
```

Expected validation:

```bash
PYTHONPATH=tooling python3 -m unittest discover -s tooling/tests/orchestrator -p 'test_*.py'
```

## Direct Runtime Commands
```bash
make conductor-init
make conductor-status
make conductor-plan
make conductor-run
make conductor-test
```

For TV7 roadmaps, prefer the `tv7-core` shims because they pass the canonical `core_dir`, `workspace_dir`, `conductor_dir`, and `state_dir` paths explicitly.

Direct explicit example:

```bash
python3 tooling/conductor.py \
  --core-dir ../tv7-core \
  --workspace-dir .. \
  --conductor-dir . \
  --state-dir docs/.conductor \
  plan
```

## Target Repo Model
Issues are created or linked in each target repo.

Conductor reads roadmap sources, resolves item repo targets, and then syncs issues to the matching GitHub repo:
- `tv7-conductor` items -> `TV7-Clip-Studio/tv7-conductor`
- `tv7-core` items -> `TV7-Clip-Studio/tv7-core`
- `tv7-backend` items -> `TV7-Clip-Studio/tv7-backend`
- `tv7-studio` items -> `TV7-Clip-Studio/tv7-studio`
- `tv7-site` items -> `TV7-Clip-Studio/tv7-site`
- `tv7-engine` items -> `TV7-Clip-Studio/tv7-engine`

Label assertion helper:

```bash
cd tv7-core
python3 tooling/orchestrator/verify_seed_labels.py \
  --repo TV7-Clip-Studio/tv7-core \
  --issue <issue-number-or-url> \
  --target-repo tv7-core \
  --batch-id <batch-id>
```

Cross-repo work may have more than one target repo. Each target keeps its own issue, validation, and PR path.

## Lifecycle
```text
seeded -> expanded -> executing -> local_review -> pr_ready -> external_review -> integrated -> done
```

Local review is first-class. It runs before PR-stage review and promotes passing items to `pr_ready`.
`pr_ready` means a human operator may open or update a PR.
External review runs only after PRs exist.

## Operator Summaries
Conductor stage commands print an `Operator summary` after the headline result. The block includes:
- stage and mode
- outcome counts
- report paths
- risks surfaced by the command
- next recommended operator action

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

## Worktree Flow
Prepare an isolated worktree:

```bash
make conductor-prepare-worktree \
  CONDUCTOR_REPO=tv7-site \
  CONDUCTOR_ITEM_ID=SI-A01-02-01 \
  CONDUCTOR_APPLY=1
```

Override path when the repo path differs from the repo id:

```bash
make conductor-prepare-worktree \
  CONDUCTOR_REPO=tv7-backend \
  CONDUCTOR_REPO_PATH=../tv7-backend \
  CONDUCTOR_ITEM_ID=BE-A01-02-01 \
  CONDUCTOR_APPLY=1
```

Conductor reports:
- worktree path
- branch
- changed files
- preview command/port metadata when supplied
- explicit cleanup command

## Preview Flow
Launch a configured preview from an isolated worktree:

```bash
make conductor-launch-preview \
  CONDUCTOR_REPO=tv7-site \
  CONDUCTOR_ITEM_ID=SI-A01-02-01 \
  CONDUCTOR_WORKTREE_PATH=docs/.conductor/worktrees/tv7-site/SI-A01-02-01 \
  CONDUCTOR_PREVIEW_PORT=4322 \
  CONDUCTOR_PREVIEW_COMMAND="npm run dev -- --host 127.0.0.1 --port 4322" \
  CONDUCTOR_HEALTH_URL="http://localhost:4322/"
```

Preview reports include PID, log path, preview URL, health URL, and stop command.
Do not stop unrelated developer services.

## Validation And Local Review
Validate a worktree:

```bash
make conductor-validate-worktree \
  CONDUCTOR_REPO=tv7-site \
  CONDUCTOR_ITEM_ID=SI-A01-02-01 \
  CONDUCTOR_WORKTREE_PATH=docs/.conductor/worktrees/tv7-site/SI-A01-02-01 \
  CONDUCTOR_VALIDATION_COMMAND="npm run build"
```

Run local review:

```bash
make conductor-local-review \
  CONDUCTOR_REPO=tv7-site \
  CONDUCTOR_ITEM_ID=SI-A01-02-01 \
  CONDUCTOR_WORKTREE_PATH=docs/.conductor/worktrees/tv7-site/SI-A01-02-01 \
  CONDUCTOR_LOCAL_REVIEW_COMMAND="npm run build" \
  CONDUCTOR_APPLY=1
```

Local review apply mode requires the repo automation level to allow review.
Keep repo levels at `plan` until the operator intentionally raises them for a controlled batch.

## External Review
External review is PR-stage review. It expects linked PR metadata and is separate from local validation.

```bash
make conductor-external-review \
  CONDUCTOR_REPO=tv7-site \
  CONDUCTOR_ITEM_ID=SI-A01-02-01
```

Paid/API reviewers require explicit opt-in:

```bash
make conductor-external-review \
  CONDUCTOR_REPO=tv7-site \
  CONDUCTOR_ITEM_ID=SI-A01-02-01 \
  CONDUCTOR_REVIEW_ALLOW_PAID=1 \
  CONDUCTOR_APPLY=1
```

`make conductor-review` remains a compatibility alias for `make conductor-external-review`.

## TV7 Repo Validation Defaults
Keep these aligned with `workspace.tv7.toml` and `repo_defaults.toml`:

| Repo | Validation |
| --- | --- |
| `tv7-core` | `make conductor-shim-check` |
| `tv7-conductor` | `PYTHONPATH=tooling python3 -m unittest discover -s tooling/tests/orchestrator -p 'test_*.py'` |
| `tv7-backend` | `make lint`, `make type`, `make test` |
| `tv7-studio` | `make check` |
| `tv7-site` | `npm run build` |
| `tv7-engine` | `make check`, `make test` |

## Safe Batch Practice
1. Run all planning and issue sync commands in dry-run first.
2. Keep scope filters tight: `--repo`, `--roadmap`, `--item-prefix`.
3. Apply tickets one repo at a time.
4. Expand one item or small prefix at a time.
5. Execute only after expansion reports look correct.
6. Run local review before PR action.
7. Run external review after PR action.
8. Do not delete state or worktrees without reviewing the printed report and cleanup command.
