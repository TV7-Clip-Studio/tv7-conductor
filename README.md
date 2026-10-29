# TV7 Conductor

Standalone orchestration runtime for the TV7 multi-repo workflow.

## Scope
- Roadmap plan/seed/expand/execute/review lifecycle.
- State, reports, and execution bridge policies.
- Conductor-focused tests and operator runbooks.
- Provider-neutral agent orchestration: AO is one execution provider, local/API reviewers are configurable provider paths.

## Quick Start
```bash
cd /Users/joaquinastelarra/Documents/TV7/tv7-conductor
make venv
make install-dev
make conductor-doctor
make conductor-init
make conductor-status
```

See `docs/bootstrap.md` for fresh-machine setup and optional web dashboard dependencies.

## Development Checks
Run the local precommit context before committing conductor changes:

```bash
make precommit-context
```

It compiles conductor Python entry points/modules, runs the orchestrator unit suite, and checks the diff for whitespace errors.

## Runtime State
Conductor state and reports are stored in `docs/.conductor` in this repo.

## Lifecycle
Default lifecycle:

```text
seeded -> expanded -> executing -> local_review -> pr_ready -> external_review -> integrated -> done
```

`local-review` is separate by default. It runs local validation/review commands after execution and only promotes an item to `pr_ready` when those checks pass. `pr_ready` is an operator gate: Conductor reports that a PR may be opened or updated, but it does not do that automatically by default.

Legacy `review_failed` and `review_passed` stages remain readable during migration.

## Local Review
Run a dry local review:

```bash
python3 tooling/conductor.py local-review \
  --repo tv7-conductor \
  --command "PYTHONPATH=tooling python3 -m unittest discover -s tooling/tests/orchestrator -p 'test_*.py'"
```

Persist local review results and stage transitions:

```bash
python3 tooling/conductor.py local-review --apply \
  --repo tv7-conductor \
  --command "PYTHONPATH=tooling python3 -m unittest discover -s tooling/tests/orchestrator -p 'test_*.py'"
```

Use `--worktree-path` to review an isolated task worktree instead of the main checkout.

## Provider Roles
Conductor uses role-based provider config in `tooling/orchestrator/provider_config.toml`:

- `planner`: default `gemini_cli_planner`
- `executor`: default `ao_codex_executor`
- `reviewer`: default `local_command_reviewer`

AO is one executor provider. Paid/API reviewers, such as DeepSeek, are optional and blocked by default unless provider policy explicitly allows them.

Current command wiring:

- `tickets` resolves a `planner` provider for assessment defaults.
- `expand` resolves a `planner` provider for expansion defaults.
- `execute` resolves an `executor` provider and currently supports `kind = "ao"`.
- `local-review` resolves a `reviewer` provider and supports `kind = "local_command"`.
- `external-review` resolves an external/API `reviewer` provider and requires `--allow-paid-review` for paid reviewers.
- `review` remains a compatibility alias for `external-review`.

## Automation Levels
Repo profiles in `tooling/orchestrator/workspace.tv7.toml` declare an `automation_level`:

```text
observe < plan < apply < execute < review < integrate
```

Apply-mode commands are blocked when a target repo is configured below the required level. Dry runs remain available for inspection.

## Operator Summaries
Stage commands print an `Operator summary` block with the stage, mode, outcome counts, report paths, risks, and the next recommended action. The summary is generated from Conductor reports/state and is intended to be the first thing an operator checks after each command.

## Fixture Scenario
The test suite includes a generated fixture project scenario:

```bash
PYTHONPATH=tooling python3 -m unittest tooling/tests/orchestrator/test_fixture_e2e_scenario.py
```

It creates a temporary project, ingests its roadmap, creates an isolated git worktree, runs local review inside that worktree, and verifies the work item reaches `pr_ready` without GitHub mutation or paid/API review.

## Compatibility
`tv7-core` keeps shim Make targets (`make conductor-*`) that call this repo.

## License

TV7 source in this repository is available under the [MIT License](LICENSE). External dependencies and tools retain their respective licenses. See the [TV7 project overview](https://github.com/TV7-Clip-Studio/tv7-core/blob/main/PROJECT.md) for the repository map and current scope.
