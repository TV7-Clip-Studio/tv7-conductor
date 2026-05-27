from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .agent_profiles import list_profiles, resolve_profile
from .automation_policy import automation_violations, load_workspace_config_optional
from .execution_bridge import execute_repo_handoffs
from .iteration_runner import run_once
from .local_review import run_local_review_items
from .models import now_iso
from .planning_pipeline import run_planning_pipeline
from .planner import merge_work_items, parse_roadmap_markdown
from .provider_config import load_provider_config, provider_for_role
from .review_gate import review_repo_items
from .roadmap_id_config import load_roadmap_config
from .state_store import append_event, ensure_layout, load_state, paths, read_events, run_lock, save_state
from .state_migration import compare_state_roots
from .tickets import (
    check_stage_label_consistency,
    expand_repo_issues,
    reset_repo_issues,
    sync_repo_issues,
)
from .worktree_preview import build_worktree_plan, launch_preview, prepare_worktree, run_validation


def _default_conductor_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_workspace_dir() -> Path:
    return _default_conductor_dir().parent


def _default_core_dir() -> Path:
    sibling_core = _default_workspace_dir() / "tv7-core"
    if sibling_core.is_dir():
        return sibling_core
    return Path.cwd()


def _default_state_dir() -> Path:
    return _default_conductor_dir() / "docs" / ".conductor"


def _default_workspace_config() -> Path:
    return _default_conductor_dir() / "tooling" / "orchestrator" / "workspace.tv7.toml"


def _provider_config_path(raw: str) -> Path | None:
    value = str(raw or "").strip()
    if not value:
        return None
    return Path(value).expanduser().resolve()


def _planner_agent_from_provider(provider: object) -> str:
    kind = str(getattr(provider, "kind", "")).strip()
    command = str(getattr(provider, "command", "")).strip().lower()
    name = str(getattr(provider, "name", "")).strip()
    if kind != "local_command":
        raise ValueError(
            f"planner provider `{name}` has unsupported kind `{kind}`; supported kind: local_command"
        )
    if "gemini" in command:
        return "gemini"
    if "codex" in command:
        return "codex"
    if command in {"", "auto"}:
        return "auto"
    raise ValueError(
        f"planner provider `{name}` command `{command}` does not map to a supported planner agent"
    )


def _check_automation(args: argparse.Namespace, *, repos: list[str], required_level: str) -> int:
    workspace_config = load_workspace_config_optional(str(getattr(args, "workspace_config", "") or ""))
    violations = automation_violations(
        workspace_config=workspace_config,
        repos=repos,
        required_level=required_level,
        command=str(getattr(args, "cmd", "") or "command"),
    )
    if not violations:
        return 0
    print("Automation level blocked this command:")
    for violation in violations:
        print(f"- {violation.message()}")
    print("Raise the repo automation_level in workspace config or run a lower-risk dry-run command.")
    return 2


def _default_repo_for_roadmap(roadmap: Path) -> list[str]:
    cfg = load_roadmap_config()
    mapped = cfg.repo_for_roadmap(roadmap.name)
    if mapped:
        return [mapped]

    name = roadmap.name
    if "tv7-fastapi-backend" in name:
        return ["tv7-fastapi-backend"]
    if "tv7-flutter-studio" in name:
        return ["tv7-flutter-studio"]
    if "tv7-astro-site" in name:
        return ["tv7-astro-site"]
    if "tv7-axum-engine" in name:
        return ["tv7-axum-engine"]
    return ["tv7-core"]


def _discover_roadmaps(core_dir: Path) -> list[Path]:
    root = core_dir / "docs" / "roadmaps"
    files: list[Path] = []
    for p in sorted(root.glob("tv7-*.md")):
        if p.is_file():
            files.append(p)
    core_roadmap = root / "core" / "agent-orchestration-roadmap.md"
    if core_roadmap.is_file():
        files.append(core_roadmap)
    return files


def _default_batch_id(core_dir: Path) -> str:
    generated = f"manual-{now_iso().replace('+00:00', 'z').replace(':', '').replace('-', '')}"
    result = subprocess.run(
        ["git", "-C", str(core_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return generated
    branch = result.stdout.strip()
    prefix = "codex/batch-tv7-core-"
    if branch.startswith(prefix):
        return branch[len(prefix):]
    return generated


def _default_graph_batch_code() -> str:
    cfg = load_roadmap_config()
    return cfg.batch_for_roadmap("project-conductor-roadmap.md")


def _write_ticket_sync_report(
    *,
    core_dir: Path,
    mode: str,
    summary: dict[str, int | str],
    actions: list[dict[str, str]],
) -> tuple[Path, Path]:
    report_dir = paths(core_dir)["root"] / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "").replace("+00:00", "z")
    json_path = report_dir / f"ticket-sync-{mode}-{stamp}.json"
    md_path = report_dir / f"ticket-sync-{mode}-{stamp}.md"

    json_path.write_text(json.dumps({"summary": summary, "actions": actions}, indent=2) + "\n")

    lines = [
        f"# Ticket Sync Report ({mode})",
        "",
        "## Summary",
        "",
    ]
    for k, v in summary.items():
        lines.append(f"- {k}: `{v}`")
    lines.extend(
        [
            "",
            "## Actions",
            "",
            "| Action | Repo | Item ID | Title | URL | Note |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for a in actions:
        lines.append(
            f"| {a.get('action','')} | {a.get('repo','')} | {a.get('item_id','')} | "
            f"{a.get('title','')} | {a.get('url','')} | {a.get('note','')} |"
        )
    md_path.write_text("\n".join(lines) + "\n")
    return json_path, md_path


def _print_operator_summary(
    *,
    stage: str,
    mode: str,
    summary: dict[str, object],
    report_json: Path | None = None,
    report_md: Path | None = None,
    elapsed_sec: float = 0.0,
    next_action: str = "",
    risks: list[str] | None = None,
) -> None:
    outcome_keys = [
        "work_items",
        "repo_targets",
        "created",
        "linked_existing",
        "would_create",
        "expanded",
        "already_expanded",
        "spawned",
        "would_spawn",
        "passed",
        "failed",
        "escalated",
        "skipped",
        "fallback",
        "would_review",
        "closed",
        "would_close",
    ]
    outcomes = []
    for key in outcome_keys:
        if key in summary:
            outcomes.append(f"{key}={summary[key]}")

    print("Operator summary:")
    print(f"- stage: {stage}")
    print(f"- mode: {mode}")
    if outcomes:
        print(f"- outcome: {' '.join(outcomes)}")
    if report_json is not None:
        print(f"Report JSON: {report_json}")
    if report_md is not None:
        print(f"Report MD: {report_md}")
    print(f"Elapsed: {elapsed_sec:.1f}s")
    for risk in risks or []:
        if str(risk).strip():
            print(f"- risk: {str(risk).strip()}")
    if next_action:
        print(f"- next: {next_action}")


def _extract_json_list(raw: str) -> list[dict[str, object]]:
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
    except Exception:
        pass
    first = text.find("[")
    last = text.rfind("]")
    if first >= 0 and last > first:
        try:
            parsed = json.loads(text[first : last + 1])
            if isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, dict)]
        except Exception:
            return []
    return []


def _ops_hint(
    *,
    row: dict[str, object],
    execute_run: dict[str, object] | None,
) -> str:
    status = str(row.get("status", "")).strip().lower()
    activity = str(row.get("activity", "")).strip().lower()
    ci_status = str(row.get("ciStatus", "")).strip().lower()
    review_decision = str(row.get("reviewDecision", "")).strip().lower()
    pending_threads_raw = row.get("pendingThreads")
    try:
        pending_threads = int(pending_threads_raw) if pending_threads_raw is not None else 0
    except Exception:
        pending_threads = 0

    if status in {"spawning"}:
        bootstrap = str((execute_run or {}).get("spawn_bootstrap", "")).strip()
        if bootstrap:
            return (
                f"Startup pending (bootstrap={bootstrap}). "
                "If stuck, attach tmux and confirm prompts."
            )
        return "Startup pending. If stuck, run `ao send --no-wait <session> 1`."
    if status in {"killed", "failed", "exited"} or activity in {"exited"}:
        return "Session stopped. Re-run execute handoff with --force."
    if ci_status in {"failed", "failure", "error"}:
        return "CI failed. Trigger fix loop for this PR."
    if pending_threads > 0:
        return f"{pending_threads} review thread(s) pending. Address reviewer comments."
    if review_decision in {"changes_requested", "request_changes"}:
        return "Review changes requested. Run follow-up fix pass."
    if status in {"working"}:
        return "Agent is running."
    if status in {"pr_open", "review_pending"}:
        return "PR open. Monitor CI/review."
    if status in {"approved", "mergeable"}:
        return "Ready to merge after policy checks."
    if status in {"merged"}:
        return "Completed."
    return "Check session output for next action."


def cmd_ops_status(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    state = load_state(core_dir)
    def _norm(value: object) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if text.lower() == "none":
            return ""
        return text

    repo_filters: set[str] = set()
    for raw in args.repo or []:
        for part in str(raw).split(","):
            token = part.strip()
            if token:
                repo_filters.add(token)

    ao_cmd = str(args.ao_command or "ao").strip() or "ao"
    result = subprocess.run(
        [ao_cmd, "status", "--json"],
        cwd=core_dir.parent,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"Failed to query AO status with `{ao_cmd}`.")
        if result.stderr.strip():
            print(result.stderr.strip())
        return 1

    sessions = _extract_json_list(result.stdout)
    if repo_filters:
        sessions = [row for row in sessions if str(row.get("project", "")).strip() in repo_filters]

    agent_runs = state.get("agent_runs", [])
    latest_execute_by_session: dict[str, dict[str, object]] = {}
    if isinstance(agent_runs, list):
        for run in agent_runs:
            if not isinstance(run, dict):
                continue
            if str(run.get("run_type", "")).strip() != "execute_handoff":
                continue
            session_id = str(run.get("session_id", "")).strip()
            if not session_id:
                continue
            latest_execute_by_session[session_id] = run

    rows: list[dict[str, object]] = []
    for row in sessions:
        session = str(row.get("name", "")).strip()
        execute_run = latest_execute_by_session.get(session)
        hint = _ops_hint(row=row, execute_run=execute_run)
        rows.append(
            {
                "session": session,
                "project": _norm(row.get("project", "")),
                "status": _norm(row.get("status", "")),
                "activity": _norm(row.get("activity", "")),
                "issue": _norm(row.get("issue", "")),
                "branch": _norm(row.get("branch", "")),
                "last_activity": _norm(row.get("lastActivity", "")),
                "pr": _norm(row.get("pr", "")) or _norm(row.get("prNumber", "")),
                "ci_status": _norm(row.get("ciStatus", "")),
                "review_decision": _norm(row.get("reviewDecision", "")),
                "pending_threads": row.get("pendingThreads"),
                "spawn_health": str((execute_run or {}).get("spawn_health", "")).strip(),
                "spawn_bootstrap": str((execute_run or {}).get("spawn_bootstrap", "")).strip(),
                "hint": hint,
            }
        )

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print(f"{'SESSION':<16} {'PROJECT':<18} {'STATUS':<12} {'ISSUE':<8} {'BRANCH':<20} HINT")
    print(f"{'-'*16:<16} {'-'*18:<18} {'-'*12:<12} {'-'*8:<8} {'-'*20:<20} {'-'*60}")
    for row in rows:
        print(
            f"{str(row.get('session',''))[:16]:<16} "
            f"{str(row.get('project',''))[:18]:<18} "
            f"{str(row.get('status',''))[:12]:<12} "
            f"{str(row.get('issue',''))[:8]:<8} "
            f"{str(row.get('branch',''))[:20]:<20} "
            f"{row.get('hint','')}"
        )
    if not rows:
        print("(no sessions)")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    p = ensure_layout(core_dir)
    print("State storage ready:")
    print(f"- root: {p['root']}")
    print(f"- state: {p['state']}")
    print(f"- events: {p['events']}")
    print(f"- runs: {p['runs']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    conductor_dir = Path(args.conductor_dir).resolve()
    workspace_dir = Path(args.workspace_dir).resolve()
    state_dir = Path(args.state_dir).resolve()
    workspace_config = Path(args.workspace_config).expanduser().resolve()
    provider_config = _provider_config_path(str(args.provider_config or "")) or (
        conductor_dir / "tooling" / "orchestrator" / "provider_config.toml"
    )

    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))

    add(
        "python",
        sys.version_info >= (3, 11),
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({sys.executable})",
    )
    add("conductor_dir", conductor_dir.is_dir(), str(conductor_dir))
    add("workspace_dir", workspace_dir.is_dir(), str(workspace_dir))
    add("source_dir", core_dir.is_dir(), str(core_dir))
    add("state_dir", state_dir.exists(), str(state_dir))
    add("workspace_config", workspace_config.is_file(), str(workspace_config))
    add("provider_config", provider_config.is_file(), str(provider_config))
    add("pyproject", (conductor_dir / "pyproject.toml").is_file(), str(conductor_dir / "pyproject.toml"))

    for command in ["git", "gh", "ao", "codex", "gemini"]:
        path = shutil.which(command)
        required = command == "git"
        ok = bool(path) or not required
        detail = path or ("optional; not found" if not required else "required; not found")
        add(f"command:{command}", ok, detail)

    for module in ["fastapi", "jinja2", "sqlmodel", "uvicorn"]:
        found = importlib.util.find_spec(module) is not None
        add(f"optional-web:{module}", True, "installed" if found else "optional; not installed")

    print("Conductor doctor:")
    failures = 0
    for name, ok, detail in checks:
        status = "OK" if ok else "FAIL"
        print(f"- {status}: {name}: {detail}")
        if not ok:
            failures += 1

    if failures:
        print("Operator summary:")
        print("- stage: doctor")
        print("- mode: inspect")
        print(f"- outcome: failures={failures}")
        print("- next: fix failed prerequisites, then rerun doctor")
        return 1

    print("Operator summary:")
    print("- stage: doctor")
    print("- mode: inspect")
    print("- outcome: failures=0")
    print("- next: run conductor-test, then conductor-status or start a scoped dry-run")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    state = load_state(core_dir)

    roadmap_files: list[Path]
    if args.all_roadmaps:
        roadmap_files = _discover_roadmaps(core_dir)
    else:
        roadmap_files = [Path(args.roadmap).resolve()]

    items = []
    for roadmap in roadmap_files:
        if args.default_repos:
            default_repos = [x.strip() for x in str(args.default_repos).split(",") if x.strip()]
        else:
            default_repos = _default_repo_for_roadmap(roadmap)
        items.extend(parse_roadmap_markdown(roadmap, default_repos=default_repos))

    if args.replace_roadmap_items:
        kept = []
        for item in state.get("work_items", []):
            source_file = str(item.get("source_file", ""))
            if "/docs/roadmaps/" in source_file.replace("\\", "/"):
                continue
            kept.append(item)
        state["work_items"] = kept

    created, updated = merge_work_items(state, items)
    save_state(core_dir, state)
    append_event(
        core_dir,
        "plan.sync",
        "Synchronized work items from roadmap files",
        {
            "created": created,
            "updated": updated,
            "roadmaps": [str(x) for x in roadmap_files],
            "replace_roadmap_items": bool(args.replace_roadmap_items),
        },
    )
    print(
        f"Plan sync complete: files={len(roadmap_files)} created={created} "
        f"updated={updated} total={len(state.get('work_items', []))}"
    )
    _print_operator_summary(
        stage="plan",
        mode="apply",
        summary={
            "work_items": len(state.get("work_items", [])),
            "created": created,
            "updated": updated,
            "roadmap_files": len(roadmap_files),
        },
        next_action="run status, then scoped tickets dry-run when the planned work set looks correct",
    )
    return 0


def cmd_graph_plan(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    state = load_state(core_dir)
    started = time.monotonic()
    mode = "apply" if args.apply else "dry-run"
    progress = bool(args.progress or args.apply)
    idea_file = Path(args.idea_file).resolve()

    if progress:
        print(
            f"[graph-plan] starting mode={mode} idea_file={idea_file} "
            f"default_repo={args.default_repo} roadmap_code={args.roadmap_code or 'auto'} "
            f"batch_code={args.batch_code} default_epic={args.default_epic} "
            f"handoff_expand={bool(args.handoff_expand)} strict_expand={bool(args.strict_expand)} "
            f"limit={int(args.limit)} resume_from={args.resume_from or 'none'} "
            f"dry_run_diff={bool(args.dry_run_diff)}",
            flush=True,
        )

    target_state = state if args.apply else copy.deepcopy(state)
    try:
        run_summary, actions = run_planning_pipeline(
            core_dir=core_dir,
            state=target_state,
            idea_file=idea_file,
            default_repo=str(args.default_repo).strip(),
            roadmap_code=str(args.roadmap_code).strip(),
            batch_code=str(args.batch_code).strip(),
            default_epic=str(args.default_epic).strip(),
            owner=args.owner,
            apply_changes=bool(args.apply),
            handoff_expand=bool(args.handoff_expand),
            strict_expand=bool(args.strict_expand),
            batch_id=str(args.batch_id or "").strip(),
            progress=progress,
            limit=int(args.limit),
            resume_from=str(args.resume_from or "").strip(),
            dry_run_diff=bool(args.dry_run_diff),
        )
    except FileNotFoundError:
        print(f"Missing IDEA file: {idea_file}")
        return 2

    if args.apply:
        save_state(core_dir, state)

    handoff = run_summary.get("handoff", {})
    summary = {
        "mode": mode,
        "idea_file": str(idea_file),
        "ingested": int(run_summary.get("ingested", 0)),
        "work_items_created": int(run_summary.get("work_items_created", 0)),
        "work_items_updated": int(run_summary.get("work_items_updated", 0)),
        "queue_records": int(run_summary.get("queue_records", 0)),
        "queue_created": int(run_summary.get("queue_created", 0)),
        "queue_updated": int(run_summary.get("queue_updated", 0)),
        "handoff_expand": bool(handoff.get("handoff_enabled", False)),
        "handoff_expanded": int(handoff.get("expanded", 0)),
        "handoff_already_expanded": int(handoff.get("already_expanded", 0)),
        "handoff_skipped": int(handoff.get("skipped", 0)),
        "handoff_fallback": int(handoff.get("fallback", 0)),
        "handoff_work_items": int(handoff.get("work_items", 0)),
        "handoff_repo_targets": int(handoff.get("repo_targets", 0)),
        "batch_id": str(args.batch_id or "").strip(),
    }
    report_json, report_md = _write_ticket_sync_report(
        core_dir=core_dir,
        mode=f"graph-plan-{mode}",
        summary=summary,
        actions=actions,
    )
    append_event(
        core_dir,
        "graph.plan",
        f"Graph plan pipeline completed ({mode})",
        {**summary, "report_json": str(report_json), "report_md": str(report_md)},
    )
    print(
        f"Graph plan ({mode}): ingested={summary['ingested']} queue_records={summary['queue_records']} "
        f"handoff_expand={summary['handoff_expand']} handoff_expanded={summary['handoff_expanded']} "
        f"handoff_already_expanded={summary['handoff_already_expanded']}"
    )
    _print_operator_summary(
        stage="graph-plan",
        mode=mode,
        summary=summary,
        report_json=report_json,
        report_md=report_md,
        elapsed_sec=time.monotonic() - started,
        next_action=(
            "inspect queued intents and expand handoff actions"
            if mode == "dry-run"
            else "run status and review generated queue records"
        ),
        risks=["strict expand fallback occurred"] if int(summary["handoff_fallback"]) else [],
    )
    if args.verbose:
        for a in actions:
            print(
                f"[{a.get('action','')}] repo={a.get('repo','')} "
                f"item={a.get('item_id','')} note={a.get('note','')} url={a.get('url','')}"
            )
    return 0


def cmd_tickets(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    state = load_state(core_dir)
    started = time.monotonic()

    if bool(args.list_agent_profiles):
        print("Available agent profiles:")
        for profile in list_profiles():
            print(
                f"- {profile.name}: assess_agent={profile.assess_agent} "
                f"model={profile.model} reasoning={profile.reasoning} "
                f"personality={profile.personality}"
            )
            if profile.notes:
                print(f"  note: {profile.notes}")
        return 0

    profile = resolve_profile(str(args.agent_profile or "").strip())
    try:
        provider_config = load_provider_config(_provider_config_path(str(args.provider_config or "")))
        planner_provider = provider_for_role(
            provider_config,
            "planner",
            name=str(args.planner_provider or "").strip(),
        )
        planner_agent = _planner_agent_from_provider(planner_provider)
    except (KeyError, PermissionError, ValueError) as exc:
        print(f"Planner provider error: {exc}")
        return 2
    assess_agent = str(args.assess_agent or "").strip() or planner_agent or profile.assess_agent
    model = str(args.model or args.codex_model or "").strip() or planner_provider.model or profile.model
    reasoning = str(args.reasoning or args.codex_reasoning or "").strip() or profile.reasoning
    personality = str(args.personality or args.codex_personality or "").strip() or profile.personality

    progress = bool(args.progress or args.apply)
    repo_filters = list(args.repo or [])
    roadmap_filters = list(args.roadmap or [])
    item_prefix_filters = list(args.item_prefix or [])
    if args.apply:
        blocked = _check_automation(args, repos=repo_filters, required_level="apply")
        if blocked:
            return blocked
    if progress:
        mode = "apply" if args.apply else "dry-run"
        print(
            f"[ticket] starting mode={mode} owner={args.owner} "
            f"profile={profile.name} "
            f"planner_provider={planner_provider.name} "
            f"assess={bool(args.assess and args.apply and assess_agent != 'none')} "
            f"assess_agent={assess_agent} model={model} "
            f"strict_assess={args.strict_assess} "
            f"reasoning={reasoning or 'default'} "
            f"batch_id={args.batch_id or 'none'} "
            f"repo_filter={','.join(repo_filters) or 'all'} "
            f"roadmap_filter={','.join(roadmap_filters) or 'all'} "
            f"item_prefix_filter={','.join(item_prefix_filters) or 'all'}",
            flush=True,
        )
    created, linked, skipped, planned, item_count, target_count, actions = sync_repo_issues(
        core_dir=core_dir,
        state=state,
        owner=args.owner,
        apply_changes=args.apply,
        assess_before_create=bool(args.assess and args.apply and assess_agent != "none"),
        assess_agent=assess_agent,
        strict_assess=bool(args.strict_assess),
        agent_profile=profile.name,
        agent_model=model,
        agent_reasoning=reasoning,
        agent_personality=personality,
        assess_timeout_sec=int(args.assess_timeout_sec),
        assess_log_max_chars=int(args.assess_log_max_chars),
        batch_id=str(args.batch_id or "").strip(),
        progress=progress,
        repo_filters=repo_filters,
        roadmap_filters=roadmap_filters,
        item_prefix_filters=item_prefix_filters,
    )
    assessed_count = sum(1 for a in actions if a.get("action") == "assessed")
    fallback_count = sum(1 for a in actions if a.get("action") == "assess_failed_fallback")
    save_state(core_dir, state)
    mode = "apply" if args.apply else "dry-run"
    summary = {
        "mode": mode,
        "work_items": item_count,
        "repo_targets": target_count,
        "created": created,
        "linked_existing": linked,
        "would_create": planned,
        "skipped_no_repo": skipped,
        "assessed": assessed_count,
        "assess_fallback": fallback_count,
        "batch_id": str(args.batch_id or "").strip(),
        "agent_profile": profile.name,
        "planner_provider": planner_provider.name,
        "planner_kind": planner_provider.kind,
        "assess_agent": assess_agent,
        "model": model,
        "reasoning": reasoning,
        "repo_filter": ",".join(repo_filters),
        "roadmap_filter": ",".join(roadmap_filters),
        "item_prefix_filter": ",".join(item_prefix_filters),
    }
    report_json, report_md = _write_ticket_sync_report(
        core_dir=core_dir,
        mode=mode,
        summary=summary,
        actions=actions,
    )
    append_event(
        core_dir,
        "ticket.sync",
        f"Ticket sync completed ({mode})",
        {**summary, "report_json": str(report_json), "report_md": str(report_md)},
    )
    if target_count == 0:
        print(
            "Ticket sync warning: no repo targets were processed. "
            "Run `make conductor-status` (or `make orchestrator-status`) and verify "
            "`work_items` and `repos` are populated."
        )
    print(
        f"Ticket sync ({mode}): work_items={item_count} repo_targets={target_count} "
        f"created={created} linked_existing={linked} would_create={planned} skipped_no_repo={skipped}"
    )
    _print_operator_summary(
        stage="tickets",
        mode=mode,
        summary=summary,
        report_json=report_json,
        report_md=report_md,
        elapsed_sec=time.monotonic() - started,
        next_action=(
            "inspect the report, then run scoped expand when seeded issues are ready"
            if mode == "apply"
            else "review planned issue actions, then rerun with --apply if scope is correct"
        ),
        risks=["no repo targets processed"] if target_count == 0 else [],
    )
    if args.verbose:
        for a in actions:
            print(
                f"[{a.get('action','')}] repo={a.get('repo','')} "
                f"item={a.get('item_id','')} note={a.get('note','')} url={a.get('url','')}"
            )
    return 0


def cmd_expand(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    state = load_state(core_dir)
    started = time.monotonic()

    mode = "apply" if args.apply else "dry-run"
    progress = bool(args.progress or args.apply)
    repo_filters = list(args.repo or [])
    roadmap_filters = list(args.roadmap or [])
    item_prefix_filters = list(args.item_prefix or [])
    if args.apply:
        blocked = _check_automation(args, repos=repo_filters, required_level="apply")
        if blocked:
            return blocked
    try:
        provider_config = load_provider_config(_provider_config_path(str(args.provider_config or "")))
        planner_provider = provider_for_role(
            provider_config,
            "planner",
            name=str(args.planner_provider or "").strip(),
        )
        planner_agent = _planner_agent_from_provider(planner_provider)
    except (KeyError, PermissionError, ValueError) as exc:
        print(f"Planner provider error: {exc}")
        return 2
    expand_agent = str(args.expand_agent or "").strip() or planner_agent
    expand_model = str(args.expand_model or "").strip() or planner_provider.model
    if progress:
        print(
            f"[expand] starting mode={mode} owner={args.owner} "
            f"strict_expand={bool(args.strict_expand)} "
            f"planner_provider={planner_provider.name} "
            f"expand_agent={expand_agent or 'auto'} "
            f"batch_id={args.batch_id or 'none'} "
            f"repo_filter={','.join(repo_filters) or 'all'} "
            f"roadmap_filter={','.join(roadmap_filters) or 'all'} "
            f"item_prefix_filter={','.join(item_prefix_filters) or 'all'} "
            f"limit={int(args.limit)} resume_from={args.resume_from or 'none'} "
            f"dry_run_diff={bool(args.dry_run_diff)}",
            flush=True,
        )

    expanded, unchanged, skipped, fallback, item_count, target_count, actions = expand_repo_issues(
        core_dir=core_dir,
        state=state,
        owner=args.owner,
        apply_changes=args.apply,
        strict_expand=bool(args.strict_expand),
        batch_id=str(args.batch_id or "").strip(),
        progress=progress,
        repo_filters=repo_filters,
        roadmap_filters=roadmap_filters,
        item_prefix_filters=item_prefix_filters,
        limit=int(args.limit),
        resume_from=str(args.resume_from or "").strip(),
        dry_run_diff=bool(args.dry_run_diff),
        expand_agent=expand_agent,
        expand_model=expand_model,
        expand_reasoning=str(args.expand_reasoning or "").strip(),
        expand_personality=str(args.expand_personality or "").strip(),
        expand_timeout_sec=int(args.expand_timeout_sec),
        expand_log_max_chars=int(args.expand_log_max_chars),
    )
    if args.apply:
        save_state(core_dir, state)

    summary = {
        "mode": mode,
        "work_items": item_count,
        "repo_targets": target_count,
        "expanded": expanded,
        "already_expanded": unchanged,
        "skipped": skipped,
        "fallback": fallback,
        "batch_id": str(args.batch_id or "").strip(),
        "repo_filter": ",".join(repo_filters),
        "roadmap_filter": ",".join(roadmap_filters),
        "item_prefix_filter": ",".join(item_prefix_filters),
        "limit": int(args.limit),
        "resume_from": str(args.resume_from or "").strip(),
        "dry_run_diff": bool(args.dry_run_diff),
        "planner_provider": planner_provider.name,
        "planner_kind": planner_provider.kind,
        "expand_agent": expand_agent,
        "expand_model": expand_model,
    }
    report_json, report_md = _write_ticket_sync_report(
        core_dir=core_dir,
        mode=f"expand-{mode}",
        summary=summary,
        actions=actions,
    )
    append_event(
        core_dir,
        "expand.sync",
        f"Expand sync completed ({mode})",
        {**summary, "report_json": str(report_json), "report_md": str(report_md)},
    )
    print(
        f"Expand sync ({mode}): work_items={item_count} repo_targets={target_count} "
        f"expanded={expanded} already_expanded={unchanged} skipped={skipped} fallback={fallback}"
    )
    _print_operator_summary(
        stage="expand",
        mode=mode,
        summary=summary,
        report_json=report_json,
        report_md=report_md,
        elapsed_sec=time.monotonic() - started,
        next_action=(
            "run execute dry-run for expanded items"
            if mode == "apply"
            else "review expansion actions/diffs, then rerun with --apply if correct"
        ),
        risks=["expansion fallback used"] if fallback else [],
    )
    if args.verbose:
        for a in actions:
            print(
                f"[{a.get('action','')}] repo={a.get('repo','')} "
                f"item={a.get('item_id','')} note={a.get('note','')} url={a.get('url','')}"
            )
    return 0


def cmd_expand_label_check(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    state = load_state(core_dir)
    started = time.monotonic()
    mode = "apply" if args.apply else "dry-run"
    progress = bool(args.progress or args.apply)
    repo_filters = list(args.repo or [])
    item_prefix_filters = list(args.item_prefix or [])

    if progress:
        print(
            f"[expand-label-check] starting mode={mode} owner={args.owner} "
            f"repo_filter={','.join(repo_filters) or 'all'} "
            f"item_prefix_filter={','.join(item_prefix_filters) or 'all'}",
            flush=True,
        )

    checked, drift, fixed, actions = check_stage_label_consistency(
        core_dir=core_dir,
        state=state,
        owner=args.owner,
        apply_changes=args.apply,
        progress=progress,
        repo_filters=repo_filters,
        item_prefix_filters=item_prefix_filters,
    )

    summary = {
        "mode": mode,
        "checked": checked,
        "drift": drift,
        "fixed": fixed,
        "repo_filter": ",".join(repo_filters),
        "item_prefix_filter": ",".join(item_prefix_filters),
    }
    report_json, report_md = _write_ticket_sync_report(
        core_dir=core_dir,
        mode=f"expand-label-check-{mode}",
        summary=summary,
        actions=actions,
    )
    append_event(
        core_dir,
        "expand.label_check",
        f"Expand label check completed ({mode})",
        {**summary, "report_json": str(report_json), "report_md": str(report_md)},
    )
    print(
        f"Expand label check ({mode}): checked={checked} drift={drift} fixed={fixed}"
    )
    _print_operator_summary(
        stage="expand-label-check",
        mode=mode,
        summary=summary,
        report_json=report_json,
        report_md=report_md,
        elapsed_sec=time.monotonic() - started,
        next_action="inspect label drift report before applying fixes" if not args.apply else "continue with execute dry-run",
        risks=["stage label drift detected"] if drift else [],
    )
    if args.verbose:
        for a in actions:
            print(
                f"[{a.get('action','')}] repo={a.get('repo','')} "
                f"item={a.get('item_id','')} note={a.get('note','')} url={a.get('url','')}"
            )
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    state = load_state(core_dir)
    started = time.monotonic()
    mode = "apply" if args.apply else "dry-run"
    progress = bool(args.progress or args.apply)
    repo_filters = list(args.repo or [])
    roadmap_filters = list(args.roadmap or [])
    item_prefix_filters = list(args.item_prefix or [])
    if args.apply:
        blocked = _check_automation(args, repos=repo_filters, required_level="execute")
        if blocked:
            return blocked
    try:
        provider_config = load_provider_config(_provider_config_path(str(args.provider_config or "")))
        executor_provider = provider_for_role(
            provider_config,
            "executor",
            name=str(args.executor_provider or "").strip(),
        )
    except (KeyError, PermissionError, ValueError) as exc:
        print(f"Executor provider error: {exc}")
        return 2
    if executor_provider.kind != "ao":
        print(
            f"Executor provider `{executor_provider.name}` has unsupported kind "
            f"`{executor_provider.kind}` for this execute bridge. Supported kind: ao."
        )
        return 2
    ao_command = str(args.ao_command).strip() or executor_provider.command or "ao"

    if progress:
        print(
            f"[execute] starting mode={mode} provider={executor_provider.name} "
            f"ao_command={ao_command} "
            f"policy={args.policy_path or 'default'} batch_id={args.batch_id or 'none'} "
            f"repo_filter={','.join(repo_filters) or 'all'} "
            f"roadmap_filter={','.join(roadmap_filters) or 'all'} "
            f"item_prefix_filter={','.join(item_prefix_filters) or 'all'} "
            f"limit={int(args.limit)} resume_from={args.resume_from or 'none'} "
            f"force={bool(args.force)}",
            flush=True,
        )

    spawned, failed, skipped, planned, item_count, target_count, actions = execute_repo_handoffs(
        core_dir=core_dir,
        state=state,
        apply_changes=bool(args.apply),
        ao_command=ao_command,
        policy_path=str(args.policy_path or "").strip(),
        batch_id=str(args.batch_id or "").strip(),
        progress=progress,
        repo_filters=repo_filters,
        roadmap_filters=roadmap_filters,
        item_prefix_filters=item_prefix_filters,
        limit=int(args.limit),
        resume_from=str(args.resume_from or "").strip(),
        force=bool(args.force),
    )
    if args.apply:
        save_state(core_dir, state)

    summary = {
        "mode": mode,
        "work_items": item_count,
        "repo_targets": target_count,
        "spawned": spawned,
        "failed": failed,
        "skipped": skipped,
        "would_spawn": planned,
        "batch_id": str(args.batch_id or "").strip(),
        "executor_provider": executor_provider.name,
        "executor_kind": executor_provider.kind,
        "ao_command": ao_command,
        "policy_path": str(args.policy_path or "").strip(),
        "repo_filter": ",".join(repo_filters),
        "roadmap_filter": ",".join(roadmap_filters),
        "item_prefix_filter": ",".join(item_prefix_filters),
        "limit": int(args.limit),
        "resume_from": str(args.resume_from or "").strip(),
        "force": bool(args.force),
    }
    report_json, report_md = _write_ticket_sync_report(
        core_dir=core_dir,
        mode=f"execute-{mode}",
        summary=summary,
        actions=actions,
    )
    append_event(
        core_dir,
        "execute.handoff",
        f"Execution handoff completed ({mode})",
        {**summary, "report_json": str(report_json), "report_md": str(report_md)},
    )
    print(
        f"Execute handoff ({mode}): work_items={item_count} repo_targets={target_count} "
        f"spawned={spawned} failed={failed} skipped={skipped} would_spawn={planned}"
    )
    _print_operator_summary(
        stage="execute",
        mode=mode,
        summary=summary,
        report_json=report_json,
        report_md=report_md,
        elapsed_sec=time.monotonic() - started,
        next_action=(
            "monitor agent sessions, then run local-review after implementation output is ready"
            if mode == "apply"
            else "review planned handoffs, then rerun with --apply if automation level allows"
        ),
        risks=["execution handoff failures present"] if failed else [],
    )
    if args.verbose:
        for a in actions:
            print(
                f"[{a.get('action','')}] repo={a.get('repo','')} "
                f"item={a.get('item_id','')} note={a.get('note','')} url={a.get('url','')}"
            )
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    state = load_state(core_dir)
    started = time.monotonic()
    mode = "apply" if args.apply else "dry-run"
    progress = bool(args.progress or args.apply)
    repo_filters = list(args.repo or [])
    roadmap_filters = list(args.roadmap or [])
    item_prefix_filters = list(args.item_prefix or [])
    if args.apply:
        blocked = _check_automation(args, repos=repo_filters, required_level="review")
        if blocked:
            return blocked
    try:
        provider_config = load_provider_config(_provider_config_path(str(args.provider_config or "")))
        reviewer_provider = provider_for_role(
            provider_config,
            "reviewer",
            name=str(args.reviewer_provider or "deepseek_api_reviewer").strip(),
            allow_paid=bool(args.allow_paid_review),
        )
    except (KeyError, PermissionError, ValueError) as exc:
        print(f"External reviewer provider error: {exc}")
        return 2
    if reviewer_provider.kind != "api":
        print(
            f"External review provider `{reviewer_provider.name}` has kind "
            f"`{reviewer_provider.kind}`. Use `local-review` for local command reviewers."
        )
        return 2

    include_recommendations_override: bool | None
    if bool(args.hard_only):
        include_recommendations_override = False
    elif bool(args.include_recommendations):
        include_recommendations_override = True
    else:
        include_recommendations_override = None

    command_name = str(getattr(args, "cmd", "review") or "review")

    if progress:
        print(
            f"[external-review] starting mode={mode} owner={args.owner} "
            f"provider={reviewer_provider.name} "
            f"max_loops_override={int(args.max_loops)} "
            f"review_policy={args.review_policy_path or 'default'} "
            f"execution_policy={args.execution_policy_path or 'default'} "
            f"include_recommendations={include_recommendations_override if include_recommendations_override is not None else 'policy-default'} "
            f"repo_filter={','.join(repo_filters) or 'all'} "
            f"roadmap_filter={','.join(roadmap_filters) or 'all'} "
            f"item_prefix_filter={','.join(item_prefix_filters) or 'all'} "
            f"limit={int(args.limit)} resume_from={args.resume_from or 'none'}",
            flush=True,
        )

    passed, failed, escalated, skipped, planned, item_count, target_count, actions = review_repo_items(
        core_dir=core_dir,
        state=state,
        owner=args.owner,
        apply_changes=bool(args.apply),
        max_loops_override=int(args.max_loops),
        review_policy_path=str(args.review_policy_path or "").strip(),
        execution_policy_path=str(args.execution_policy_path or "").strip(),
        include_recommendations_override=include_recommendations_override,
        progress=progress,
        repo_filters=repo_filters,
        roadmap_filters=roadmap_filters,
        item_prefix_filters=item_prefix_filters,
        limit=int(args.limit),
        resume_from=str(args.resume_from or "").strip(),
    )
    if args.apply:
        save_state(core_dir, state)

    summary = {
        "mode": mode,
        "work_items": item_count,
        "repo_targets": target_count,
        "passed": passed,
        "failed": failed,
        "escalated": escalated,
        "skipped": skipped,
        "would_review": planned,
        "reviewer_provider": reviewer_provider.name,
        "reviewer_kind": reviewer_provider.kind,
        "allow_paid_review": bool(args.allow_paid_review),
        "max_loops_override": int(args.max_loops),
        "review_policy_path": str(args.review_policy_path or "").strip(),
        "execution_policy_path": str(args.execution_policy_path or "").strip(),
        "include_recommendations": (
            include_recommendations_override
            if include_recommendations_override is not None
            else "policy-default"
        ),
        "repo_filter": ",".join(repo_filters),
        "roadmap_filter": ",".join(roadmap_filters),
        "item_prefix_filter": ",".join(item_prefix_filters),
        "limit": int(args.limit),
        "resume_from": str(args.resume_from or "").strip(),
    }
    report_json, report_md = _write_ticket_sync_report(
        core_dir=core_dir,
        mode=f"external-review-{mode}",
        summary=summary,
        actions=actions,
    )
    append_event(
        core_dir,
        "external_review.gate",
        f"External review gate completed ({mode})",
        {
            **summary,
            "command": command_name,
            "compatibility_alias": command_name == "review",
            "report_json": str(report_json),
            "report_md": str(report_md),
        },
    )
    print(
        f"External review ({mode}): work_items={item_count} repo_targets={target_count} "
        f"passed={passed} failed={failed} escalated={escalated} skipped={skipped} would_review={planned}"
    )
    _print_operator_summary(
        stage="external-review",
        mode=mode,
        summary=summary,
        report_json=report_json,
        report_md=report_md,
        elapsed_sec=time.monotonic() - started,
        next_action=(
            "address failed/escalated review findings before integration"
            if failed or escalated
            else "continue to integration validation when external review passes"
        ),
        risks=["paid/API review was explicitly allowed"] if bool(args.allow_paid_review) else [],
    )
    if args.verbose:
        for a in actions:
            print(
                f"[{a.get('action','')}] repo={a.get('repo','')} "
                f"item={a.get('item_id','')} note={a.get('note','')} url={a.get('url','')}"
            )
    if command_name == "review":
        print("- note: `review` is a compatibility alias; prefer `external-review` for PR-stage API review.")
    return 0


def cmd_local_review(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    state = load_state(core_dir)
    started = time.monotonic()
    mode = "apply" if args.apply else "dry-run"
    progress = bool(args.progress or args.apply)
    repo_filters = list(args.repo or [])
    roadmap_filters = list(args.roadmap or [])
    item_prefix_filters = list(args.item_prefix or [])
    if args.apply:
        blocked = _check_automation(args, repos=repo_filters, required_level="review")
        if blocked:
            return blocked
    try:
        provider_config = load_provider_config(_provider_config_path(str(args.provider_config or "")))
        reviewer_provider = provider_for_role(
            provider_config,
            "reviewer",
            name=str(args.reviewer_provider or "").strip(),
            allow_paid=False,
        )
    except (KeyError, PermissionError, ValueError) as exc:
        print(f"Local reviewer provider error: {exc}")
        return 2
    if reviewer_provider.kind != "local_command":
        print(
            f"Local review provider `{reviewer_provider.name}` has kind "
            f"`{reviewer_provider.kind}`. Use `review --allow-paid-review` for API reviewers."
        )
        return 2
    review_commands = list(args.command or [])
    if not review_commands and reviewer_provider.command:
        review_commands = [reviewer_provider.command]

    if progress:
        print(
            f"[local-review] starting mode={mode} "
            f"provider={reviewer_provider.name} "
            f"repo_filter={','.join(repo_filters) or 'all'} "
            f"roadmap_filter={','.join(roadmap_filters) or 'all'} "
            f"item_prefix_filter={','.join(item_prefix_filters) or 'all'} "
            f"limit={int(args.limit)} resume_from={args.resume_from or 'none'} "
            f"commands={len(review_commands)}",
            flush=True,
        )

    passed, failed, skipped, planned, item_count, target_count, actions = run_local_review_items(
        core_dir=core_dir,
        state=state,
        apply_changes=bool(args.apply),
        workspace_dir=str(args.workspace_dir),
        repo_path_override=str(args.repo_path or "").strip(),
        worktree_path_override=str(args.worktree_path or "").strip(),
        commands=review_commands,
        timeout_sec=int(args.timeout_sec or reviewer_provider.timeout_sec),
        repo_filters=repo_filters,
        roadmap_filters=roadmap_filters,
        item_prefix_filters=item_prefix_filters,
        limit=int(args.limit),
        resume_from=str(args.resume_from or "").strip(),
        progress=progress,
    )
    if args.apply:
        save_state(core_dir, state)

    summary = {
        "mode": mode,
        "work_items": item_count,
        "repo_targets": target_count,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "would_review": planned,
        "reviewer_provider": reviewer_provider.name,
        "reviewer_kind": reviewer_provider.kind,
        "repo_filter": ",".join(repo_filters),
        "roadmap_filter": ",".join(roadmap_filters),
        "item_prefix_filter": ",".join(item_prefix_filters),
        "limit": int(args.limit),
        "resume_from": str(args.resume_from or "").strip(),
        "commands": len(review_commands),
    }
    report_json, report_md = _write_ticket_sync_report(
        core_dir=core_dir,
        mode=f"local-review-{mode}",
        summary=summary,
        actions=actions,
    )
    append_event(
        core_dir,
        "local_review.gate",
        f"Local review gate completed ({mode})",
        {**summary, "report_json": str(report_json), "report_md": str(report_md)},
    )
    print(
        f"Local review ({mode}): work_items={item_count} repo_targets={target_count} "
        f"passed={passed} failed={failed} skipped={skipped} would_review={planned}"
    )
    _print_operator_summary(
        stage="local-review",
        mode=mode,
        summary=summary,
        report_json=report_json,
        report_md=report_md,
        elapsed_sec=time.monotonic() - started,
        next_action=(
            "operator may open/update a PR only after items reach pr_ready"
            if failed == 0
            else "inspect local review reports and rerun after fixes"
        ),
        risks=["local review failures present"] if failed else [],
    )
    if args.verbose:
        for a in actions:
            print(
                f"[{a.get('action','')}] repo={a.get('repo','')} "
                f"item={a.get('item_id','')} note={a.get('note','')} url={a.get('url','')}"
            )
    return 0 if failed == 0 else 1


def cmd_tickets_reset(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    state = load_state(core_dir)
    started = time.monotonic()
    mode = "apply" if args.apply else "dry-run"
    progress = bool(args.progress or args.apply)
    if progress:
        print(
            f"[ticket-reset] starting mode={mode} owner={args.owner} "
            f"scope={args.scope} scan_open={bool(args.scan_open)} "
            f"batch_id={args.batch_id or 'none'}",
            flush=True,
        )
    closed, planned, skipped, actions = reset_repo_issues(
        core_dir=core_dir,
        state=state,
        owner=args.owner,
        apply_changes=args.apply,
        batch_id=str(args.batch_id or "").strip(),
        scope=args.scope,
        scan_open=bool(args.scan_open),
        progress=progress,
    )
    if args.apply:
        save_state(core_dir, state)
    summary = {
        "mode": mode,
        "scope": args.scope,
        "scan_open": bool(args.scan_open),
        "batch_id": str(args.batch_id or "").strip(),
        "closed": closed,
        "would_close": planned,
        "skipped": skipped,
    }
    report_json, report_md = _write_ticket_sync_report(
        core_dir=core_dir,
        mode=f"reset-{mode}",
        summary=summary,
        actions=actions,
    )
    append_event(
        core_dir,
        "ticket.reset",
        f"Ticket reset completed ({mode})",
        {**summary, "report_json": str(report_json), "report_md": str(report_md)},
    )
    print(
        f"Ticket reset ({mode}): scope={args.scope} scan_open={bool(args.scan_open)} "
        f"batch_id={args.batch_id or 'none'} "
        f"closed={closed} would_close={planned} skipped={skipped}"
    )
    _print_operator_summary(
        stage="tickets-reset",
        mode=mode,
        summary=summary,
        report_json=report_json,
        report_md=report_md,
        elapsed_sec=time.monotonic() - started,
        next_action="verify reset scope and reports before continuing ticket sync",
        risks=["destructive issue close operation"] if mode == "apply" else [],
    )
    if args.verbose:
        for a in actions:
            print(
                f"[{a.get('action','')}] repo={a.get('repo','')} "
                f"item={a.get('item_id','')} note={a.get('note','')} url={a.get('url','')}"
            )
    return 0


def cmd_run_once(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    state = load_state(core_dir)

    try:
        with run_lock(core_dir):
            iteration = run_once(
                core_dir=core_dir,
                state=state,
                max_review_loops=args.max_review_loops,
                agent_retries=args.agent_retries,
                auto_push=not args.no_auto_push,
            )
    except FileExistsError:
        print("Another orchestrator run is active (lock file exists).")
        return 2

    state.setdefault("iterations", []).append(iteration.to_dict())
    save_state(core_dir, state)

    print(f"Iteration {iteration.iteration_id} finished: {iteration.status}")
    print(f"Summary: {iteration.summary_file}")
    if iteration.errors:
        print("Errors:")
        for err in iteration.errors:
            print(f"- {err}")
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    state = load_state(core_dir)
    state_path = paths(core_dir)["state"]
    work_items = state.get("work_items", [])
    iterations = state.get("iterations", [])
    agent_runs = state.get("agent_runs", [])
    planning = state.get("planning", {})
    idea_runs = planning.get("idea_runs", []) if isinstance(planning, dict) else []
    intent_queue = planning.get("intent_queue", []) if isinstance(planning, dict) else []
    execute_runs = [
        x for x in agent_runs if isinstance(x, dict) and str(x.get("run_type", "")).strip() == "execute_handoff"
    ] if isinstance(agent_runs, list) else []
    schema = state.get("schema", {})
    idempotency = state.get("idempotency", {})
    idem_keys = idempotency.get("keys", {}) if isinstance(idempotency, dict) else {}
    print(f"State file: {state_path}")
    print(
        f"Schema: {schema.get('name', 'unknown')} "
        f"v{schema.get('version', state.get('version', '?'))}"
    )
    print(f"Work items: {len(work_items)}")
    print(f"Idea runs: {len(idea_runs) if isinstance(idea_runs, list) else 0}")
    print(f"Intent queue records: {len(intent_queue) if isinstance(intent_queue, list) else 0}")
    print(f"Iterations: {len(iterations)}")
    print(f"Agent runs: {len(agent_runs)}")
    print(f"Execution handoffs: {len(execute_runs)}")
    print(f"Idempotency keys: {len(idem_keys) if isinstance(idem_keys, dict) else 0}")
    print()
    print(f"{'ITEM ID':<42} {'STATUS':<12} {'STAGE':<14} {'REPOS':<30} TITLE")
    print(f"{'-'*42:<42} {'-'*12:<12} {'-'*14:<14} {'-'*30:<30} {'-'*40}")
    for item in work_items[-20:]:
        repos = ",".join(item.get("repos", []))[:30]
        print(
            f"{item.get('item_id','')[:42]:<42} "
            f"{item.get('status','')[:12]:<12} "
            f"{str(item.get('stage',''))[:14]:<14} "
            f"{repos:<30} {item.get('title','')}"
        )
    if iterations:
        last = iterations[-1]
        print()
        print("Last iteration:")
        print(json.dumps(last, indent=2))
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    events = read_events(core_dir, limit=args.limit)
    for e in events:
        print(f"{e.get('ts','')} [{e.get('event_type','')}] {e.get('message','')}")
        ctx = e.get("context", {})
        if ctx:
            print(json.dumps(ctx, indent=2))
    return 0


def cmd_prepare_worktree(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    repo_path = Path(args.repo_path).expanduser()
    if not repo_path.is_absolute():
        repo_path = Path(args.workspace_dir).expanduser().resolve() / repo_path
    plan = build_worktree_plan(
        repo=str(args.repo),
        repo_path=repo_path,
        item_id=str(args.item_id),
        worktree_root=Path(args.worktree_root),
        branch=str(args.branch or ""),
        base_ref=str(args.base_ref or "HEAD"),
        preview_port=int(args.preview_port or 0),
        preview_command=str(args.preview_command or ""),
        apply=bool(args.apply),
    )
    created, error, report_path = prepare_worktree(core_dir, plan)
    mode = "apply" if args.apply else "dry-run"
    print(f"Worktree prepare ({mode}): repo={plan.repo} item={plan.item_id}")
    print(f"- branch: {plan.branch}")
    print(f"- worktree: {plan.worktree_path}")
    print(f"- base_ref: {plan.base_ref}")
    if plan.preview_port:
        print(f"- preview_port: {plan.preview_port}")
        print(f"- preview_url: {plan.preview_url}")
    if plan.preview_command:
        print(f"- preview_command: {plan.preview_command}")
    print(f"- report: {report_path}")
    if args.apply:
        print(f"- created: {created}")
    else:
        print("- created: false (dry-run)")
    if error:
        print(f"- error: {error}")
        return 1
    print(f"- cleanup: git -C {plan.repo_path} worktree remove {plan.worktree_path}")
    return 0


def cmd_launch_preview(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    repo_path = Path(args.repo_path).expanduser()
    if not repo_path.is_absolute():
        repo_path = Path(args.workspace_dir).expanduser().resolve() / repo_path
    worktree_path = Path(args.worktree_path).expanduser().resolve()
    plan = build_worktree_plan(
        repo=str(args.repo),
        repo_path=repo_path,
        item_id=str(args.item_id),
        worktree_root=worktree_path.parent.parent,
        branch=str(args.branch or ""),
        base_ref=str(args.base_ref or "HEAD"),
        preview_port=int(args.preview_port or 0),
        preview_command=str(args.preview_command or ""),
        apply=False,
    )
    object.__setattr__(plan, "worktree_path", worktree_path)
    pid, healthy, report_path = launch_preview(
        core_dir,
        plan,
        health_url=str(args.health_url or ""),
        wait_sec=float(args.wait_sec),
    )
    print(f"Preview launched: repo={plan.repo} item={plan.item_id}")
    print(f"- pid: {pid}")
    print(f"- preview_url: {plan.preview_url}")
    if args.health_url:
        print(f"- health_url: {args.health_url}")
    print(f"- healthy: {healthy}")
    print(f"- report: {report_path}")
    print(f"- stop: kill {pid}")
    return 0 if healthy or not args.require_healthy else 1


def cmd_validate_worktree(args: argparse.Namespace) -> int:
    core_dir = Path(args.core_dir).resolve()
    repo_path = Path(args.repo_path).expanduser()
    if not repo_path.is_absolute():
        repo_path = Path(args.workspace_dir).expanduser().resolve() / repo_path
    worktree_path = Path(args.worktree_path).expanduser().resolve()
    plan = build_worktree_plan(
        repo=str(args.repo),
        repo_path=repo_path,
        item_id=str(args.item_id),
        worktree_root=worktree_path.parent.parent,
        branch=str(args.branch or ""),
        base_ref=str(args.base_ref or "HEAD"),
        preview_port=int(args.preview_port or 0),
        preview_command="",
        apply=False,
    )
    object.__setattr__(plan, "worktree_path", worktree_path)
    commands = [str(x).strip() for x in args.command if str(x).strip()]
    passed, report_path = run_validation(
        core_dir,
        plan,
        commands=commands,
        timeout_sec=int(args.timeout_sec),
    )
    print(f"Worktree validation: repo={plan.repo} item={plan.item_id}")
    print(f"- worktree: {plan.worktree_path}")
    print(f"- commands: {len(commands)}")
    print(f"- passed: {passed}")
    print(f"- report: {report_path}")
    print("- next: inspect logs in the report before requesting review or merge.")
    return 0 if passed else 1


def cmd_state_migration_check(args: argparse.Namespace) -> int:
    summary = compare_state_roots(
        Path(args.legacy_state_root).expanduser().resolve(),
        Path(args.active_state_root).expanduser().resolve(),
    )
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print("State migration check:")
    print(f"- legacy_root: {summary['legacy_root']}")
    print(f"- active_root: {summary['active_root']}")
    print(f"- legacy_exists: {summary['legacy_exists']}")
    print(f"- active_exists: {summary['active_exists']}")
    print(f"- same_digest: {summary['same_digest']}")
    print(f"- legacy_work_items: {summary['legacy_work_items']}")
    print(f"- active_work_items: {summary['active_work_items']}")
    print(f"- legacy_agent_runs: {summary['legacy_agent_runs']}")
    print(f"- active_agent_runs: {summary['active_agent_runs']}")
    if summary["only_legacy_item_ids"]:
        print(f"- only_legacy_item_ids: {', '.join(summary['only_legacy_item_ids'])}")
    if summary["only_active_item_ids"]:
        print(f"- only_active_item_ids: {', '.join(summary['only_active_item_ids'])}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project Conductor CLI (single-run mode).")
    parser.add_argument("--source-dir", dest="core_dir", help="Path to source/governance repo root.")
    parser.add_argument(
        "--core-dir",
        help="Deprecated alias for --source-dir.",
    )
    parser.set_defaults(core_dir=str(_default_core_dir()))
    parser.add_argument(
        "--workspace-dir",
        default=str(_default_workspace_dir()),
        help="Path to workspace root containing target repos.",
    )
    parser.add_argument(
        "--conductor-dir",
        default=str(_default_conductor_dir()),
        help="Path to tv7-conductor repository root.",
    )
    parser.add_argument(
        "--state-dir",
        default=str(_default_state_dir()),
        help="Path to conductor state root (default: tv7-conductor/docs/.conductor).",
    )
    parser.add_argument(
        "--workspace-config",
        default=str(_default_workspace_config()),
        help="Path to workspace/repo automation config.",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize orchestrator state directories.")
    p_init.set_defaults(func=cmd_init)

    p_doctor = sub.add_parser("doctor", help="Check local Conductor prerequisites and optional dependencies.")
    p_doctor.add_argument(
        "--provider-config",
        default="",
        help="Optional provider config TOML path (default: tooling/orchestrator/provider_config.toml).",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_plan = sub.add_parser("plan", help="Sync roadmap tasks into orchestrator work items.")
    p_plan.add_argument(
        "--roadmap",
        default=str(_default_core_dir() / "docs" / "roadmaps" / "core" / "agent-orchestration-roadmap.md"),
        help="Roadmap markdown path to parse.",
    )
    p_plan.add_argument(
        "--default-repos",
        default="",
        help="Optional comma-separated repos applied when task text has no repo hints. If omitted, uses roadmap-file mapping.",
    )
    p_plan.add_argument(
        "--all-roadmaps",
        action="store_true",
        default=True,
        help="Parse all repo roadmap files (default: true).",
    )
    p_plan.add_argument(
        "--single-roadmap",
        dest="all_roadmaps",
        action="store_false",
        help="Parse only --roadmap instead of all roadmap files.",
    )
    p_plan.add_argument(
        "--replace-roadmap-items",
        action="store_true",
        default=True,
        help="Replace existing roadmap-derived work items before merging (default: true).",
    )
    p_plan.add_argument(
        "--no-replace-roadmap-items",
        dest="replace_roadmap_items",
        action="store_false",
        help="Do not remove old roadmap-derived work items before merge.",
    )
    p_plan.set_defaults(func=cmd_plan)

    p_graph = sub.add_parser(
        "graph-plan",
        help="Run idea ingestion -> queue emission -> optional expand handoff pipeline.",
    )
    p_graph.add_argument(
        "--idea-file",
        default=str(_default_core_dir() / "IDEA.md"),
        help="Path to IDEA markdown input file.",
    )
    p_graph.add_argument(
        "--default-repo",
        default="tv7-core",
        help="Default repo hint when IDEA lines do not include repo targets.",
    )
    p_graph.add_argument(
        "--roadmap-code",
        default="",
        help="Optional repo code prefix for generated item IDs (auto from --default-repo if omitted).",
    )
    p_graph.add_argument(
        "--batch-code",
        default=_default_graph_batch_code(),
        help="Batch code for generated item IDs (default: project-conductor roadmap batch).",
    )
    p_graph.add_argument(
        "--default-epic",
        default="01",
        help="Default epic code when IDEA line does not specify epic hint.",
    )
    p_graph.add_argument("--owner", default="TV7-Clip-Studio", help="GitHub org/user owner.")
    p_graph.add_argument(
        "--apply",
        action="store_true",
        help="Persist state changes and apply expand handoff updates.",
    )
    p_graph.add_argument(
        "--handoff-expand",
        action="store_true",
        default=True,
        help="Trigger expand-stage handoff after queue emission (default: true).",
    )
    p_graph.add_argument(
        "--no-handoff-expand",
        dest="handoff_expand",
        action="store_false",
        help="Skip expand-stage handoff and stop after queue emission.",
    )
    p_graph.add_argument(
        "--strict-expand",
        action="store_true",
        default=False,
        help="Fail handoff run if expansion payload validation fails.",
    )
    p_graph.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N expand handoff repo targets (0 means no limit).",
    )
    p_graph.add_argument(
        "--resume-from",
        default="",
        help="Resume expand handoff from this item id/prefix in sorted order (inclusive).",
    )
    p_graph.add_argument(
        "--dry-run-diff",
        action="store_true",
        default=False,
        help="Write expand dry-run diff artifacts into docs/.conductor/reports/expand-diff-*/.",
    )
    p_graph.add_argument(
        "--batch-id",
        default=_default_batch_id(_default_core_dir()),
        help="Batch id used for expand handoff idempotency/labels (default: inferred from core branch).",
    )
    p_graph.add_argument(
        "--progress",
        action="store_true",
        default=False,
        help="Print real-time pipeline progress (apply mode enables this automatically).",
    )
    p_graph.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-action details in stdout in addition to summary/report files.",
    )
    p_graph.set_defaults(func=cmd_graph_plan)

    p_tickets = sub.add_parser("tickets", help="Create/link GitHub issues for work items by repo.")
    p_tickets.add_argument("--owner", default="TV7-Clip-Studio", help="GitHub org/user owner.")
    p_tickets.add_argument("--apply", action="store_true", help="Actually create issues (default is dry-run).")
    p_tickets.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit sync to one or more repos (repeat flag or use comma-separated values).",
    )
    p_tickets.add_argument(
        "--roadmap",
        action="append",
        default=[],
        help="Limit sync to roadmap source file(s) by basename or path (repeat/comma-separated supported).",
    )
    p_tickets.add_argument(
        "--item-prefix",
        action="append",
        default=[],
        help="Limit sync to item id prefix(es), e.g. ST-A01 or PY-B01-02.",
    )
    p_tickets.add_argument(
        "--agent-profile",
        default="local_auto_medium",
        help="Assessment profile name (default: local_auto_medium).",
    )
    p_tickets.add_argument(
        "--provider-config",
        default="",
        help="Optional provider config TOML path (default: tooling/orchestrator/provider_config.toml).",
    )
    p_tickets.add_argument(
        "--planner-provider",
        default="",
        help="Planner provider name used for issue assessment defaults.",
    )
    p_tickets.add_argument(
        "--list-agent-profiles",
        action="store_true",
        default=False,
        help="List available agent profiles and exit.",
    )
    p_tickets.add_argument(
        "--assess-agent",
        choices=["auto", "gemini", "antigravity", "codex", "none"],
        default="",
        help="Override assessment provider. Defaults to the selected agent profile.",
    )
    p_tickets.add_argument(
        "--model",
        default="",
        help="Override model from selected agent profile.",
    )
    p_tickets.add_argument(
        "--reasoning",
        default="",
        help='Override reasoning level from selected agent profile (e.g. "medium").',
    )
    p_tickets.add_argument(
        "--personality",
        default="",
        help="Override personality from selected agent profile.",
    )
    p_tickets.add_argument(
        "--assess",
        action="store_true",
        default=True,
        help="Run assessment before creating issues (apply mode only).",
    )
    p_tickets.add_argument(
        "--no-assess",
        dest="assess",
        action="store_false",
        help="Disable pre-ticket assessment.",
    )
    p_tickets.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-action details in stdout in addition to summary/report files.",
    )
    p_tickets.add_argument(
        "--progress",
        action="store_true",
        default=False,
        help="Print real-time progress updates during ticket sync (apply mode enables this automatically).",
    )
    p_tickets.add_argument(
        "--strict-assess",
        action="store_true",
        default=False,
        help="Fail ticket sync if assessment fails. Default is fallback mode.",
    )
    p_tickets.add_argument(
        "--codex-model",
        default="",
        help="Legacy alias for --model (kept for backward compatibility).",
    )
    p_tickets.add_argument(
        "--codex-reasoning",
        default="",
        help='Legacy alias for --reasoning (kept for backward compatibility).',
    )
    p_tickets.add_argument(
        "--codex-personality",
        default="",
        help="Legacy alias for --personality (kept for backward compatibility).",
    )
    p_tickets.add_argument(
        "--assess-timeout-sec",
        type=int,
        default=300,
        help="Timeout in seconds for each Codex assessment call.",
    )
    p_tickets.add_argument(
        "--assess-log-max-chars",
        type=int,
        default=4000,
        help="Max stdout/stderr characters to keep in assessment log excerpts.",
    )
    p_tickets.add_argument(
        "--batch-id",
        default=_default_batch_id(_default_core_dir()),
        help="Batch id written into created issue bodies/metadata (default: inferred from core branch).",
    )
    p_tickets.set_defaults(func=cmd_tickets)

    p_expand = sub.add_parser("expand", help="Enrich seeded issues with structured expansion sections.")
    p_expand.add_argument("--owner", default="TV7-Clip-Studio", help="GitHub org/user owner.")
    p_expand.add_argument("--apply", action="store_true", help="Actually update issue bodies/labels (default dry-run).")
    p_expand.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit expand to one or more repos (repeat flag or use comma-separated values).",
    )
    p_expand.add_argument(
        "--roadmap",
        action="append",
        default=[],
        help="Limit expand to roadmap source file(s) by basename or path (repeat/comma-separated supported).",
    )
    p_expand.add_argument(
        "--item-prefix",
        action="append",
        default=[],
        help="Limit expand to item id prefix(es), e.g. CO-C01-04.",
    )
    p_expand.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N repo targets (0 means no limit).",
    )
    p_expand.add_argument(
        "--resume-from",
        default="",
        help="Resume from this item id/prefix in sorted order (inclusive).",
    )
    p_expand.add_argument(
        "--dry-run-diff",
        action="store_true",
        default=False,
        help="Write unified diff artifacts during dry-run into docs/.conductor/reports/expand-diff-*/.",
    )
    p_expand.add_argument(
        "--strict-expand",
        action="store_true",
        default=False,
        help="Fail expand run if structured payload validation fails; default is fallback mode.",
    )
    p_expand.add_argument(
        "--expand-agent",
        choices=["auto", "gemini", "antigravity", "codex", "none"],
        default="",
        help="Expansion provider strategy (default: selected planner provider).",
    )
    p_expand.add_argument(
        "--provider-config",
        default="",
        help="Optional provider config TOML path (default: tooling/orchestrator/provider_config.toml).",
    )
    p_expand.add_argument(
        "--planner-provider",
        default="",
        help="Planner provider name used for expansion defaults.",
    )
    p_expand.add_argument(
        "--expand-model",
        default="",
        help="Optional model override for expansion provider.",
    )
    p_expand.add_argument(
        "--expand-reasoning",
        default="",
        help='Optional reasoning level for expansion provider (e.g. "medium").',
    )
    p_expand.add_argument(
        "--expand-personality",
        default="",
        help="Optional personality override for expansion provider.",
    )
    p_expand.add_argument(
        "--expand-timeout-sec",
        type=int,
        default=300,
        help="Timeout in seconds for each Codex expansion call.",
    )
    p_expand.add_argument(
        "--expand-log-max-chars",
        type=int,
        default=4000,
        help="Max stdout/stderr characters to keep in expansion log excerpts.",
    )
    p_expand.add_argument(
        "--batch-id",
        default=_default_batch_id(_default_core_dir()),
        help="Batch id used for idempotency key/labels (default: inferred from core branch).",
    )
    p_expand.add_argument(
        "--progress",
        action="store_true",
        default=False,
        help="Print real-time progress updates during expand (apply mode enables this automatically).",
    )
    p_expand.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-action details in stdout in addition to summary/report files.",
    )
    p_expand.set_defaults(func=cmd_expand)

    p_expand_label = sub.add_parser(
        "expand-label-check",
        help="Check/fix stage label consistency for seeded/expanded work items.",
    )
    p_expand_label.add_argument("--owner", default="TV7-Clip-Studio", help="GitHub org/user owner.")
    p_expand_label.add_argument(
        "--apply",
        action="store_true",
        help="Actually fix mismatched stage labels (default dry-run).",
    )
    p_expand_label.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit check to one or more repos (repeat flag or comma-separated values).",
    )
    p_expand_label.add_argument(
        "--item-prefix",
        action="append",
        default=[],
        help="Limit check to item id prefix(es).",
    )
    p_expand_label.add_argument(
        "--progress",
        action="store_true",
        default=False,
        help="Print real-time progress updates during label check (apply mode enables this automatically).",
    )
    p_expand_label.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-action details in stdout in addition to summary/report files.",
    )
    p_expand_label.set_defaults(func=cmd_expand_label_check)

    p_execute = sub.add_parser(
        "execute",
        help="Bridge expanded work items into execution runs (`ao spawn <repo> <item_id>`).",
    )
    p_execute.add_argument("--apply", action="store_true", help="Actually run execution handoff (default dry-run).")
    p_execute.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit execute to one or more repos (repeat flag or comma-separated values).",
    )
    p_execute.add_argument(
        "--roadmap",
        action="append",
        default=[],
        help="Limit execute to roadmap source file(s) by basename or path (repeat/comma-separated supported).",
    )
    p_execute.add_argument(
        "--item-prefix",
        action="append",
        default=[],
        help="Limit execute to item id prefix(es), e.g. CO-C01-06.",
    )
    p_execute.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N repo targets (0 means no limit).",
    )
    p_execute.add_argument(
        "--resume-from",
        default="",
        help="Resume execute handoff from this item id/prefix in sorted order (inclusive).",
    )
    p_execute.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Ignore execute-stage idempotency key and re-run handoff for matching targets.",
    )
    p_execute.add_argument(
        "--provider-config",
        default="",
        help="Optional provider config TOML path (default: tooling/orchestrator/provider_config.toml).",
    )
    p_execute.add_argument(
        "--executor-provider",
        default="",
        help="Executor provider name (default: provider config executor default).",
    )
    p_execute.add_argument(
        "--ao-command",
        default="",
        help="Override AO command name. Defaults to the selected executor provider command.",
    )
    p_execute.add_argument(
        "--policy-path",
        default="",
        help="Optional execution policy TOML path (default: tooling/orchestrator/execution_policy.toml).",
    )
    p_execute.add_argument(
        "--batch-id",
        default=_default_batch_id(_default_core_dir()),
        help="Batch id used for execute idempotency keys (default: inferred from core branch).",
    )
    p_execute.add_argument(
        "--progress",
        action="store_true",
        default=False,
        help="Print real-time progress updates during execute handoff (apply mode enables this automatically).",
    )
    p_execute.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-action details in stdout in addition to summary/report files.",
    )
    p_execute.set_defaults(func=cmd_execute)

    p_review = sub.add_parser(
        "external-review",
        aliases=["review"],
        help="Evaluate PR-stage external API/GitHub review signals and gate lifecycle transitions.",
    )
    p_review.add_argument("--owner", default="TV7-Clip-Studio", help="GitHub org/user owner.")
    p_review.add_argument("--apply", action="store_true", help="Persist review verdicts/stage transitions (default dry-run).")
    p_review.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit external review gate to one or more repos (repeat flag or comma-separated values).",
    )
    p_review.add_argument(
        "--roadmap",
        action="append",
        default=[],
        help="Limit external review gate to roadmap source file(s) by basename or path (repeat/comma-separated supported).",
    )
    p_review.add_argument(
        "--item-prefix",
        action="append",
        default=[],
        help="Limit external review gate to item id prefix(es), e.g. CO-C01-07.",
    )
    p_review.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N repo targets (0 means no limit).",
    )
    p_review.add_argument(
        "--resume-from",
        default="",
        help="Resume external review gate from this item id/prefix in sorted order (inclusive).",
    )
    p_review.add_argument(
        "--max-loops",
        type=int,
        default=0,
        help="Override max review loops per repo target (0 uses review policy config).",
    )
    p_review.add_argument(
        "--provider-config",
        default="",
        help="Optional provider config TOML path (default: tooling/orchestrator/provider_config.toml).",
    )
    p_review.add_argument(
        "--reviewer-provider",
        default="deepseek_api_reviewer",
        help="External reviewer provider name (default: deepseek_api_reviewer).",
    )
    p_review.add_argument(
        "--allow-paid-review",
        action="store_true",
        default=False,
        help="Allow paid/API reviewer providers for this external review run.",
    )
    p_review.add_argument(
        "--review-policy-path",
        default="",
        help="Optional review policy TOML path (default: tooling/orchestrator/review_policy.toml).",
    )
    p_review.add_argument(
        "--execution-policy-path",
        default="",
        help="Optional execution policy TOML path for task-class detection.",
    )
    p_review.add_argument(
        "--include-recommendations",
        action="store_true",
        default=False,
        help="Treat recommendation-style comments as actionable for this run.",
    )
    p_review.add_argument(
        "--hard-only",
        action="store_true",
        default=False,
        help="Ignore recommendations and only gate on hard/explicit blocking findings.",
    )
    p_review.add_argument(
        "--progress",
        action="store_true",
        default=False,
        help="Print real-time progress updates during external review gate (apply mode enables this automatically).",
    )
    p_review.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-action details in stdout in addition to summary/report files.",
    )
    p_review.set_defaults(func=cmd_review)

    p_local_review = sub.add_parser(
        "local-review",
        help="Run local validation/review for executing items before operator PR action.",
    )
    p_local_review.add_argument("--apply", action="store_true", help="Persist local review verdicts/stage transitions.")
    p_local_review.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit local review to one or more repos (repeat flag or comma-separated values).",
    )
    p_local_review.add_argument(
        "--roadmap",
        action="append",
        default=[],
        help="Limit local review to roadmap source file(s) by basename or path.",
    )
    p_local_review.add_argument(
        "--item-prefix",
        action="append",
        default=[],
        help="Limit local review to item id prefix(es).",
    )
    p_local_review.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N repo targets (0 means no limit).",
    )
    p_local_review.add_argument(
        "--resume-from",
        default="",
        help="Resume local review from this item id/prefix in sorted order.",
    )
    p_local_review.add_argument(
        "--repo-path",
        default="",
        help="Optional repo path override. Relative paths resolve against --workspace-dir.",
    )
    p_local_review.add_argument(
        "--worktree-path",
        default="",
        help="Optional isolated worktree path to review instead of the repo checkout.",
    )
    p_local_review.add_argument(
        "--provider-config",
        default="",
        help="Optional provider config TOML path (default: tooling/orchestrator/provider_config.toml).",
    )
    p_local_review.add_argument(
        "--reviewer-provider",
        default="",
        help="Local reviewer provider name (default: provider config reviewer default).",
    )
    p_local_review.add_argument(
        "--command",
        action="append",
        default=[],
        help="Local review/validation command to run. Repeat for multiple commands.",
    )
    p_local_review.add_argument("--timeout-sec", type=int, default=600, help="Timeout per local review command.")
    p_local_review.add_argument(
        "--progress",
        action="store_true",
        default=False,
        help="Print real-time progress updates during local review.",
    )
    p_local_review.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-action details in stdout in addition to summary/report files.",
    )
    p_local_review.set_defaults(func=cmd_local_review)

    p_tickets_reset = sub.add_parser(
        "tickets-reset",
        help="Close orchestrator-linked issues from state (batch-scoped or all linked).",
    )
    p_tickets_reset.add_argument("--owner", default="TV7-Clip-Studio", help="GitHub org/user owner.")
    p_tickets_reset.add_argument("--apply", action="store_true", help="Actually close issues (default is dry-run).")
    p_tickets_reset.add_argument(
        "--scope",
        choices=["batch", "all-linked"],
        default="batch",
        help="Reset scope. 'batch' closes only issues tagged with --batch-id metadata.",
    )
    p_tickets_reset.add_argument(
        "--scan-open",
        action="store_true",
        default=False,
        help="Scan GitHub open issues by orchestrator body marker instead of state-linked issues.",
    )
    p_tickets_reset.add_argument(
        "--batch-id",
        default=_default_batch_id(_default_core_dir()),
        help="Batch id for scope=batch (default: inferred from core branch).",
    )
    p_tickets_reset.add_argument(
        "--progress",
        action="store_true",
        default=False,
        help="Print real-time progress updates during ticket reset (apply mode enables this automatically).",
    )
    p_tickets_reset.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-action details in stdout in addition to summary/report files.",
    )
    p_tickets_reset.set_defaults(func=cmd_tickets_reset)

    p_run = sub.add_parser("run-once", help="Run one full orchestration iteration.")
    p_run.add_argument("--max-review-loops", type=int, default=2, help="Maximum DeepSeek fix loops.")
    p_run.add_argument("--agent-retries", type=int, default=1, help="Retries per repo agent run.")
    p_run.add_argument("--no-auto-push", action="store_true", help="Disable auto-push flag in state metadata.")
    p_run.set_defaults(func=cmd_run_once)

    p_status = sub.add_parser("status", help="Show current orchestrator state.")
    p_status.set_defaults(func=cmd_status)

    p_ops_status = sub.add_parser(
        "ops-status",
        help="Show AO spawned-agent sessions with actionable operator hints.",
    )
    p_ops_status.add_argument(
        "--ao-command",
        default="ao",
        help="AO CLI command name (default: ao).",
    )
    p_ops_status.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Filter sessions by project repo id (repeat flag or comma-separated values).",
    )
    p_ops_status.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit JSON instead of table output.",
    )
    p_ops_status.set_defaults(func=cmd_ops_status)

    p_events = sub.add_parser("events", help="Show recent orchestrator events.")
    p_events.add_argument("--limit", type=int, default=30, help="Number of recent events.")
    p_events.set_defaults(func=cmd_events)

    p_worktree = sub.add_parser(
        "prepare-worktree",
        help="Plan or create an isolated worktree for human testing.",
    )
    p_worktree.add_argument("--repo", required=True, help="Repo id/name for report metadata.")
    p_worktree.add_argument("--repo-path", required=True, help="Repo path, absolute or relative to --workspace-dir.")
    p_worktree.add_argument("--item-id", required=True, help="Conductor item/task id.")
    p_worktree.add_argument(
        "--worktree-root",
        default=str(_default_conductor_dir() / "docs" / ".conductor" / "worktrees"),
        help="Directory where isolated worktrees should be placed.",
    )
    p_worktree.add_argument("--branch", default="", help="Branch name to create (default: conductor/<repo>/<item>).")
    p_worktree.add_argument("--base-ref", default="HEAD", help="Base ref for git worktree add.")
    p_worktree.add_argument("--preview-port", type=int, default=0, help="Optional preview port to report.")
    p_worktree.add_argument("--preview-command", default="", help="Optional preview command to report.")
    p_worktree.add_argument("--apply", action="store_true", help="Actually create the git worktree.")
    p_worktree.set_defaults(func=cmd_prepare_worktree)

    p_preview = sub.add_parser(
        "launch-preview",
        help="Launch a configured preview command inside an isolated worktree.",
    )
    p_preview.add_argument("--repo", required=True, help="Repo id/name for report metadata.")
    p_preview.add_argument("--repo-path", required=True, help="Repo path, absolute or relative to --workspace-dir.")
    p_preview.add_argument("--item-id", required=True, help="Conductor item/task id.")
    p_preview.add_argument("--worktree-path", required=True, help="Existing isolated worktree path.")
    p_preview.add_argument("--branch", default="", help="Branch name for metadata.")
    p_preview.add_argument("--base-ref", default="HEAD", help="Base ref for metadata.")
    p_preview.add_argument("--preview-port", type=int, required=True, help="Preview port to expose.")
    p_preview.add_argument("--preview-command", required=True, help="Command to run in the worktree.")
    p_preview.add_argument("--health-url", default="", help="Optional health URL to poll.")
    p_preview.add_argument("--wait-sec", type=float, default=10.0, help="Seconds to wait for health.")
    p_preview.add_argument("--require-healthy", action="store_true", help="Exit non-zero if health does not pass.")
    p_preview.set_defaults(func=cmd_launch_preview)

    p_validate = sub.add_parser(
        "validate-worktree",
        help="Run validation commands inside an isolated worktree and write a report.",
    )
    p_validate.add_argument("--repo", required=True, help="Repo id/name for report metadata.")
    p_validate.add_argument("--repo-path", required=True, help="Repo path, absolute or relative to --workspace-dir.")
    p_validate.add_argument("--item-id", required=True, help="Conductor item/task id.")
    p_validate.add_argument("--worktree-path", required=True, help="Existing isolated worktree path.")
    p_validate.add_argument("--branch", default="", help="Branch name for metadata.")
    p_validate.add_argument("--base-ref", default="HEAD", help="Base ref for metadata.")
    p_validate.add_argument("--preview-port", type=int, default=0, help="Optional preview port exposed to commands.")
    p_validate.add_argument(
        "--command",
        action="append",
        required=True,
        help="Validation command to run inside worktree. Repeat for multiple commands.",
    )
    p_validate.add_argument("--timeout-sec", type=int, default=600, help="Timeout per validation command.")
    p_validate.set_defaults(func=cmd_validate_worktree)

    p_state_check = sub.add_parser(
        "state-migration-check",
        help="Compare historical and active conductor state roots without modifying either.",
    )
    p_state_check.add_argument(
        "--legacy-state-root",
        default=str(_default_workspace_dir() / "tv7-core" / "docs" / ".conductor"),
        help="Historical state root to compare.",
    )
    p_state_check.add_argument(
        "--active-state-root",
        default=str(_default_state_dir()),
        help="Active conductor state root to compare.",
    )
    p_state_check.add_argument("--json", action="store_true", help="Emit JSON summary.")
    p_state_check.set_defaults(func=cmd_state_migration_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    import os

    prev_state_dir = os.environ.get("TV7_CONDUCTOR_STATE_DIR")
    prev_conductor_dir = os.environ.get("TV7_CONDUCTOR_DIR")
    prev_workspace_dir = os.environ.get("TV7_WORKSPACE_DIR")
    if getattr(args, "state_dir", ""):
        os.environ["TV7_CONDUCTOR_STATE_DIR"] = str(Path(args.state_dir).expanduser().resolve())
    if getattr(args, "conductor_dir", ""):
        os.environ["TV7_CONDUCTOR_DIR"] = str(Path(args.conductor_dir).expanduser().resolve())
    if getattr(args, "workspace_dir", ""):
        os.environ["TV7_WORKSPACE_DIR"] = str(Path(args.workspace_dir).expanduser().resolve())
    try:
        return int(args.func(args))
    finally:
        for key, previous in (
            ("TV7_CONDUCTOR_STATE_DIR", prev_state_dir),
            ("TV7_CONDUCTOR_DIR", prev_conductor_dir),
            ("TV7_WORKSPACE_DIR", prev_workspace_dir),
        ):
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
