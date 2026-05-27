# Project Conductor Failure Policy

## Purpose
Define timeout, retry, escalation, and human-override policy per orchestration step.

## Policy Matrix
| Step | Timeout Policy | Retry Policy | Escalation | Human Override |
| --- | --- | --- | --- | --- |
| `plan` | Fail fast on parse/load errors | No implicit retries | Mark step failed and stop run | Fix roadmap/config and rerun plan |
| `seed` | Ticket assessment timeout is bounded | Retry only where command supports explicit rerun | Fallback to default acceptance when non-strict assess is enabled | Use `--no-assess` or rerun scoped apply |
| `expand` | Bounded per-ticket enrichment timeout | Retry by explicit `--resume-from` or limited reruns | Move item to manual escalation when repeated schema failures occur | Manually edit issue and resume from next item |
| `execute` | Per-repo execution attempt bounded by runner limits | Use configured agent retries | Escalate repo-item pair after max retries | Re-run specific repo/item via scoped filters |
| `review` | DeepSeek loop bounded by max loops | Retry within configured loop count only | Escalate to `needs-human` after loop cap | Human triage then restart loop |
| `integrate` | Validation command timeouts bounded per repo | Retry once after deterministic fix | Block completion on repeated failures | Manual validation + explicit completion decision |

## Policy Configuration Source
Policy is encoded in `tooling/orchestrator/conductor_policy.toml`.

Current CLI/runtime controls that reflect policy:
- `tickets --assess-timeout-sec`
- `tickets --strict-assess` / `--no-assess`
- `tickets --assess-log-max-chars`
- `execute --limit` / `--resume-from` / `--force`
- `execute --policy-path` / `--ao-command`
- `external-review --max-loops` / `--review-policy-path` / `--execution-policy-path`
- `review --include-recommendations` / `--hard-only`
- `run-once --max-review-loops`
- `run-once --agent-retries`

## Event And Log Expectations
The following event classes must remain present and actionable:
- `plan.sync`
- `ticket.sync`
- `ticket.assessed`
- `ticket.assess_failed`
- `execute.handoff`
- `execute.spawn`
- `external_review.gate`
- `external_review.finding`
- `external_review.item`
- `iteration.review.loop`
- `iteration.failed`
- `iteration.end`

Each failure path must produce:
- A concise error summary
- Context payload (item/repo/loop)
- Pointer to report/log artifact

## Escalation Rules
Escalate immediately when:
- A step fails with potential off-scope side effects.
- Retry budget is exhausted.
- State cannot be loaded or persisted.
- Review loop hits max threshold without approval.

Escalation target:
- Architect owner logs `needs-human` note in roadmap and pauses apply-mode operations for affected scope.

## Human Override Rules
- Only override with explicit scope (`repo`, `roadmap`, `item-prefix`).
- Use dry-run before any follow-up apply run after override.
- Record override reason and command scope in a status note.

## Minimum Operator Playbook
1. Stop apply-mode command.
2. Inspect latest report in `docs/.conductor/reports/`.
3. Inspect `make conductor-events` output.
4. Apply smallest scoped corrective action.
5. Resume with dry-run, then apply if safe.
