# Project Conductor Phase Gates

## Purpose
Define go/no-go controls for migration phases with explicit owners, rollback triggers, and sign-off evidence.

## Gate Model
Each phase gate has:
- Entry criteria
- Exit criteria
- Owner
- Evidence path
- Decision (`GO`, `NO-GO`, `BLOCKED`)

## Global Rollback Triggers
Rollback to legacy-oriented operation if any trigger is true:
- Off-scope issue creation is detected in apply mode.
- Work-item state becomes non-recoverable or cannot be loaded.
- Review loop exceeds configured max without explicit escalation.
- Conductor run produces blocking regressions in two consecutive runs.

## Phase Gates
| Phase | Entry Criteria | Exit Criteria | Owner | Evidence |
| --- | --- | --- | --- | --- |
| 1 Program Charter And Controls | Roadmap scope agreed | Charter, gates, failure policy published in `docs/repos/core/` | Architect | `project-conductor-charter.md`, `project-conductor-failure-policy.md`, this file |
| 2 Data Model, IDs, State Machine | Phase 1 gate GO | Schema/versioning + transition policy validated | Architect | `docs/.conductor/state.json`, tests/reports |
| 3 Seed Workflow | Phase 2 gate GO | Deterministic minimal issues + scoped filters validated | Architect | `docs/.conductor/reports/ticket-sync-*.md` |
| 4 Expand Workflow | Phase 3 gate GO | Idempotent expansion + stage label transition validated | Architect | expand reports/checks |
| 5 LangGraph Integration | Phase 4 gate GO | Idea ingestion and queue handoff stable | Architect | graph integration reports |
| 6 Composio Execution | Phase 5 gate GO | Handoff bridge and artifact capture validated | Architect | execution logs/state artifacts |
| 7 DeepSeek Quality Loop | Phase 6 gate GO | Bounded deterministic review retries + escalation state | Architect | review loop reports |
| 8 Integrator Validation | Phase 7 gate GO | Final validation matrix gating completion | Architect | integration validation reports |
| 9 Unified Run Command | Phase 8 gate GO | Full flow command supports scoped + full runs | Architect | `make conductor-run` reports |
| 10 Observability And Decommission | Phase 9 gate GO | Two clean batches and legacy dependency removed | Architect | sign-off record + migration report |

## Decision Checklist Per Gate
- [ ] Entry criteria verified.
- [ ] Exit criteria evidence attached.
- [ ] Rollback triggers checked.
- [ ] Owner sign-off recorded.

## Dry-Run Simulation Protocol (Blocked + Passed)
Use this protocol for gate validation drills:

1. Blocked simulation
- Force one invalid or missing prerequisite (for example empty state/work items).
- Run: `make conductor-status` then scoped dry-run with restrictive filters.
- Expect: decision `BLOCKED` with explicit blocker note and no apply changes.

2. Passed simulation
- Prepare valid state and scoped target.
- Run: `make conductor-plan` then scoped dry-run ticket sync.
- Expect: deterministic report with zero off-scope actions and decision `GO`.

3. Record evidence
- Save decision, blocker/pass reason, and report paths in this file or linked migration log.

## Current Decision Snapshot
- Phase 1: `GO` (docs published and linked).
- Phase 2-10: `BACKLOG`.
