# TV7 Conductor Roadmap

Roadmap Code: CD
Batch Code: A01

## Phase 1: Extraction Stabilization
- [X] [CD-A01-01-01] Validate `tv7-core` shim targets invoke `tv7-conductor` runtime for all conductor stages.
    **COMPLETED** | Architect Agent | 2026-05-12 14:20
    - **NOTE**: Added `tv7-core/tooling/check-conductor-shims.py` and `make conductor-shim-check`.
    - **NOTE**: The check dry-runs core conductor stage targets, rejects deprecated `tooling/orchestrator.py` routing, verifies the standalone CLI is reached through `tv7-core/tooling/conductor.py`, and confirms `--state-dir` writes to the requested state root.
    - **NOTE**: Validation commands: `make conductor-shim-check` from `tv7-core`; `PYTHONPATH=tooling python3 -m unittest discover -s tooling/tests/orchestrator -p 'test_*.py'` from `tv7-conductor`.
- [ ] [CD-A01-01-02] Add CI checks for conductor unit tests and lint.
    **BACKLOG** | Architect Agent | 2026-04-05 13:35
- [ ] [CD-A01-01-03] Externalize AO dashboard/dependency docs and install instructions.
    **BACKLOG** | Architect Agent | 2026-04-05 13:35

## Phase 2: Path Boundary Hardening
- [ ] [CD-A01-02-01] Split the overloaded `core_dir` runtime argument into explicit workspace, core, conductor, state, and target repo path roles.
    **BACKLOG** | Architect Agent | 2026-04-25 10:55
    - **NOTE**: `tv7-core` remains the canonical roadmap/governance source, while `tv7-conductor/docs/.conductor` becomes the active runtime state root.
- [X] [CD-A01-02-02] Update `tv7-core` `conductor-*` Make targets and shim scripts to pass explicit path roles into `tv7-conductor`.
    **COMPLETED** | Architect Agent | 2026-04-27 12:50
    - **NOTE**: Acceptance: every stage target resolves `tv7-core/docs/roadmaps` for roadmap reads and `tv7-conductor/docs/.conductor` for runtime state.
    - **NOTE**: `tv7-core` conductor shims now inject `--core-dir`, `--workspace-dir`, `--conductor-dir`, and `--state-dir`; Make targets route through the shims.
- [ ] [CD-A01-02-03] Add regression tests for direct `tv7-conductor` CLI runs and `tv7-core` shimmed runs.
    **BACKLOG** | Architect Agent | 2026-04-25 10:55
    - **NOTE**: Include plan/status/run dry-runs and confirm no import path falls back to deleted `tv7-core/tooling/orchestrator` modules.
- [X] [CD-A01-02-04] Remove or relocate conductor-side tests that still cover `tv7-core`-owned DeepSeek shared tooling.
    **COMPLETED** | Architect Agent | 2026-04-27 12:50
    - **NOTE**: Current failing imports: `deepseek_review_comment` and `deepseek_followup`; those modules remain in `tv7-core` and should be tested there.
    - **NOTE**: Removed conductor-side DeepSeek shared-tooling tests; conductor suite now passes without `tv7-core/tooling` on `PYTHONPATH`.

## Phase 3: State And Docs De-Duplication
- [X] [CD-A01-03-01] Add a state migration/check command for historical `tv7-core/docs/.conductor` versus active `tv7-conductor/docs/.conductor`.
    **COMPLETED** | Architect Agent | 2026-04-27 13:05
    - **NOTE**: The command must report diffs and require an explicit apply before archiving or replacing state.
    - **NOTE**: Added `state-migration-check` CLI/Make target. It compares state roots and reports digests, work item counts, agent run counts, and item ID differences without modifying either state root.
- [ ] [CD-A01-03-02] Deduplicate Project Conductor docs between `tv7-core` and `tv7-conductor`.
    **BACKLOG** | Architect Agent | 2026-04-25 10:55
    - **NOTE**: Keep governance and architecture decision records in `tv7-core`; keep runtime runbooks and operator details in `tv7-conductor`.
- [ ] [CD-A01-03-03] Define removal criteria for deprecated `orchestrator-*` aliases after two clean full Conductor batches.
    **BACKLOG** | Architect Agent | 2026-04-25 10:55
    - **NOTE**: Do not remove compatibility names until integrate stage, runbook validation, and phase gates are complete.

## Phase 4: Portable Productization
- [X] [CD-A01-04-01] Add install/bootstrap flow for running `tv7-conductor` on a fresh machine.
    **COMPLETED** | Architect Agent | 2026-04-25 11:10
    - **NOTE**: Include prerequisites, Python environment setup, GitHub/agent dependencies, `.env` handling, and first-run validation.
    - **NOTE**: Added `pyproject.toml`, `make install-dev`, `make install-web`, `conductor doctor`, `make conductor-doctor`, and `docs/bootstrap.md`.
- [X] [CD-A01-04-02] Add generic workspace/repo registry config so Conductor can target non-TV7 repos.
    **COMPLETED** | Architect Agent | 2026-04-27 12:50
    - **NOTE**: Config should declare repo path, roadmap/task sources, issue tracker settings, labels, validation commands, preview commands, and allowed automation level.
    - **NOTE**: Added `workspace_config.py`, generic config tests, and a TV7 workspace profile with validation and preview command fields.
- [X] [CD-A01-04-03] Keep Make targets as the primary human-facing interface and scripts as implementation details.
    **COMPLETED** | Architect Agent | 2026-04-27 13:20
    - **NOTE**: Make targets should print what happened, what was skipped, where reports/logs live, and the next human decision.
    - **NOTE**: Added Make targets for tests, isolated worktree preparation, preview launch, validation, and state migration checks; implementation remains in Python CLI/helpers.
- [X] [CD-A01-04-04] Add fixture-repo portability tests.
    **COMPLETED** | Architect Agent | 2026-04-27 13:05
    - **NOTE**: Prove plan/status/dry-run execution works without importing `tv7-core` modules or assuming TV7 paths.
    - **NOTE**: Added CLI portability coverage using a temporary non-TV7 fixture repo with explicit path roles and isolated state root.
- [X] [CD-A01-04-05] Implement `resolve-conflicts` command in Conductor CLI and integrate with `git-dev.sh`.
    **COMPLETED** | Architect Agent | 2026-06-07 20:13
    - **NOTE**: Added `resolve-conflicts` subcommand under `cli.py` in `tv7-conductor`. It scans for unmerged files, runs `codex exec` to resolve them, and stages them if conflict markers are cleared. Updated `git-dev.sh` in `tv7-core` to invoke Conductor.
- [X] [CD-A01-04-06] Add `describe-changes` command to Conductor CLI to display PR-like bulleted change summaries.
    **COMPLETED** | Architect Agent | 2026-06-07 20:53
    - **NOTE**: Added `describe-changes` subcommand under `cli.py` in `tv7-conductor`. It queries `gemini-2.5-flash` to generate a bulleted summary of current repository changes. Wired it to display context immediately in `git-dev.sh`.



## Phase 5: Isolated Human Test Environments
- [X] [CD-A01-05-01] Add worktree-backed execution support for task changes.
    **COMPLETED** | Architect Agent | 2026-04-27 12:50
    - **NOTE**: Conductor should be able to prepare or hand off work in an isolated worktree so the human's primary checkout remains available.
    - **NOTE**: Added dry-run-first `prepare-worktree` CLI/Make target and report generation. Apply mode creates a git worktree; cleanup remains explicit.
- [X] [CD-A01-05-02] Add per-repo preview port override support.
    **COMPLETED** | Architect Agent | 2026-04-27 12:50
    - **NOTE**: Example: if the normal service runs on `localhost:5000`, Conductor can launch the task preview on `localhost:5001` or the next configured free port.
    - **NOTE**: Workspace profiles and `prepare-worktree` reports now carry preview command/port/URL metadata; service lifecycle orchestration remains a follow-up.
- [X] [CD-A01-05-03] Add preview/test run reporting.
    **COMPLETED** | Architect Agent | 2026-04-27 13:05
    - **NOTE**: Reports must include worktree path, branch, command, preview URL, logs, validation result, changed files, and cleanup instructions.
    - **NOTE**: Worktree reports include changed files/status and cleanup command; preview reports include PID, log path, preview/health URLs, and validation result.
- [X] [CD-A01-05-04] Add explicit cleanup and safety controls for isolated worktrees and preview services.
    **COMPLETED** | Architect Agent | 2026-04-27 13:05
    - **NOTE**: Cleanup should require an explicit command and must not stop unrelated services or delete unreported work.
    - **NOTE**: Worktree cleanup and preview stop commands are reported explicitly; Conductor does not auto-remove worktrees or stop unrelated services.

## Phase 6: Human-In-The-Loop Automation Levels
- [X] [CD-A01-06-01] Define automation levels: observe, plan, apply, execute, review, and integrate.
    **COMPLETED** | Architect Agent | 2026-04-25 11:10
    - **NOTE**: Dry-run/observe should be default; merge, deploy, destructive cleanup, and broad issue mutations require explicit operator intent.
    - **NOTE**: Apply-mode ticket/expand/execute/local-review/external-review commands now enforce per-repo `automation_level` from workspace config.
- [X] [CD-A01-06-02] Add command summaries and next-action prompts for every Conductor stage.
    **COMPLETED** | Architect Agent | 2026-04-25 11:10
    - **NOTE**: Output should state actions taken, skipped items, failures, risks, generated artifacts, and what approval is still needed.
    - **NOTE**: Added a reusable operator summary block for plan, graph-plan, tickets, expand, label check, execute, local review, external review, and ticket reset paths.
- [X] [CD-A01-06-03] Add reusable per-repo validation command profiles.
    **COMPLETED** | Architect Agent | 2026-04-27 12:50
    - **NOTE**: Profiles should support unit checks, lint/type checks, service preview commands, health checks, and repo-specific environment overrides.
    - **NOTE**: Added validation command fields to generic workspace config and TV7 repo profile.
    - **NOTE**: Added `validate-worktree` CLI/Make target to run validation commands inside an isolated worktree and write logs/report metadata.

## Phase 7: Local-First Review And Provider Roles
- [X] [CD-A01-07-01] Add first-class local review lifecycle stages before PR-stage review.
    **COMPLETED** | Architect Agent | 2026-05-09 00:00
    - **NOTE**: Added lifecycle path `seeded -> expanded -> executing -> local_review -> pr_ready -> external_review -> integrated -> done`.
    - **NOTE**: `pr_ready` is an operator gate; Conductor reports readiness but does not open or update PRs by default.
    - **NOTE**: Legacy `review_failed` and `review_passed` stages remain readable during migration.
- [X] [CD-A01-07-02] Add a separate dry-run-first local review command.
    **COMPLETED** | Architect Agent | 2026-05-09 00:00
    - **NOTE**: Added `local-review` CLI and `make conductor-local-review`.
    - **NOTE**: Local review writes reports under `docs/.conductor/reports/local-review/` and promotes passing items to `pr_ready` only in apply mode.
- [X] [CD-A01-07-03] Define provider roles for planner, executor, and reviewer.
    **COMPLETED** | Architect Agent | 2026-05-09 00:00
    - **NOTE**: AO should be modeled as one executor provider, not as the execution architecture itself.
    - **NOTE**: Default role intent: planner via Gemini CLI, executor via AO/Codex CLI, reviewer via local command; API reviewers remain optional.
- [X] [CD-A01-07-04] Add provider configuration and selection tests.
    **COMPLETED** | Architect Agent | 2026-05-09 00:00
    - **NOTE**: Config should declare provider role, command/API shape, timeout, retry policy, artifact paths, and whether paid/API review is allowed.
    - **NOTE**: Added `provider_config.toml`, `provider_config.py`, and provider selection tests. Paid/API reviewers are blocked unless explicitly allowed.
- [X] [CD-A01-07-05] Move external GitHub/DeepSeek review under the `external_review` provider path.
    **COMPLETED** | Architect Agent | 2026-05-09 00:00
    - **NOTE**: Local validation/review should resolve problems before PR-stage review whenever possible.
    - **NOTE**: Added `external-review` as the primary command name, kept `review` as a compatibility alias, and moved reports/events to `external_review.*` naming.
- [X] [CD-A01-07-06] Wire provider selection into planner, executor, local review, and external review commands.
    **COMPLETED** | Architect Agent | 2026-05-09 00:00
    - **NOTE**: Keep current command defaults stable while allowing explicit provider overrides.
    - **NOTE**: `tickets` and `expand` now resolve planner providers for assessment/expansion defaults; executor/local-review/external-review provider resolution remains wired through command boundaries.
- [X] [CD-A01-07-07] Enforce external API reviewer opt-in at the command boundary.
    **COMPLETED** | Architect Agent | 2026-05-09 00:00
    - **NOTE**: `review` resolves `deepseek_api_reviewer` as the external reviewer path and refuses paid/API review unless `--allow-paid-review` is set.
    - **NOTE**: `local-review` remains the default local reviewer path and rejects API reviewer providers.

## Phase 8: End-To-End Testing Scenario
- [X] [CD-A01-08-01] Add a fixture project scenario for conductor end-to-end testing.
    **COMPLETED** | Architect Agent | 2026-05-09 00:00
    - **NOTE**: Use a tiny local project with a roadmap item, validation command, isolated worktree, local review, and operator-gated PR-ready transition.
    - **NOTE**: The scenario should prove the conductor can run without TV7-specific paths, GitHub mutation, or paid/API review.
    - **NOTE**: Added a generated fixture project test that plans a roadmap item, creates an isolated git worktree, runs local review in that worktree, and promotes the item to `pr_ready`.
