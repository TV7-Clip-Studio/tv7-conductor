from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import importlib.util
import json
import os
import random
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

COLOR_SUPPORT = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
C_GREEN = "\033[0;32m"
C_RED = "\033[0;31m"
C_YELLOW = "\033[0;33m"
C_CYAN = "\033[0;36m"
C_MAGENTA = "\033[0;35m"
C_BOLD = "\033[1m"
C_RESET = "\033[0m"

def color(text: str, code: str) -> str:
    if COLOR_SUPPORT:
        return f"{code}{text}{C_RESET}"
    return text


def colorize_markdown(text: str) -> str:
    if not COLOR_SUPPORT:
        return text

    text = re.sub(r"\*\*(.*?)\*\*", lambda m: f"{C_BOLD}{C_GREEN}{m.group(1)}{C_RESET}", text)
    text = re.sub(r"`(.*?)`", lambda m: f"{C_CYAN}{m.group(1)}{C_RESET}", text)

    def colorize_bullet(m):
        return f"{m.group(1)}{C_GREEN}{m.group(2)}{C_RESET} "

    text = re.sub(r"^(\s*)([\*\-])\s+", colorize_bullet, text, flags=re.MULTILINE)
    return text


def _gemini_capacity_error(output: str) -> bool:
    lowered = output.lower()
    return any(
        marker in lowered
        for marker in ["capacity", "exhausted", "quota", "429", "rate limit", "too many requests"]
    )


def _extract_json_object_from_text(text: str) -> dict[str, object]:
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("no JSON object found")


def _gemini_command(model: str) -> list[str]:
    return ["gemini", "-m", model, "-p", "-", "-o", "json"]


def _run_gemini_model(prompt: str, cwd: str, model: str) -> tuple[subprocess.CompletedProcess, float]:
    start_time = time.time()
    result = subprocess.run(
        _gemini_command(model),
        cwd=cwd,
        input=prompt,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, time.time() - start_time


def _unwrap_gemini_stdout(result: subprocess.CompletedProcess, elapsed: float) -> None:
    if result.returncode != 0:
        return
    try:
        data = _extract_json_object_from_text(result.stdout)
    except ValueError:
        return

    response_text = data.get("response")
    if isinstance(response_text, str):
        result.stdout = response_text

    stats = data.get("stats", {})
    models = stats.get("models", {}) if isinstance(stats, dict) else {}
    if not isinstance(models, dict) or not models:
        return

    parts = []
    for model_name, model_data in models.items():
        tokens = model_data.get("tokens", {}) if isinstance(model_data, dict) else {}
        total = tokens.get("total", 0) if isinstance(tokens, dict) else 0
        cached = tokens.get("cached", 0) if isinstance(tokens, dict) else 0
        token_str = f"{total:,}"
        if cached:
            token_str += f" ({cached:,} cached)"
        parts.append(f"model: {color(str(model_name), C_CYAN)}, tokens: {color(token_str, C_YELLOW)}")
    parts.append(f"time: {color(f'{elapsed:.1f}s', C_CYAN)}")
    print(color(f"  [gemini metrics: {', '.join(parts)}]", C_RESET), file=sys.stderr)


def run_gemini_cli(prompt: str, cwd: str) -> subprocess.CompletedProcess:
    model = os.environ.get("TV7_GEMINI_MODEL", "gemini-2.5-flash").strip()
    result, elapsed = _run_gemini_model(prompt, cwd, model)

    if result.returncode != 0 and model == "gemini-2.5-flash":
        err_msg = (result.stderr or result.stdout or "").strip()
        if _gemini_capacity_error(err_msg):
            fallback_model = "gemini-2.5-pro"
            print(color(f"  ⚠ Capacity limit reached for {model}. Retrying with {fallback_model}...", C_YELLOW), file=sys.stderr)
            result, elapsed = _run_gemini_model(prompt, cwd, fallback_model)

    _unwrap_gemini_stdout(result, elapsed)
    return result

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
    if "tv7-backend" in name:
        return ["tv7-backend"]
    if "tv7-studio" in name:
        return ["tv7-studio"]
    if "tv7-site" in name:
        return ["tv7-site"]
    if "tv7-engine" in name:
        return ["tv7-engine"]
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


def cmd_suggest_branch(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).expanduser()
    if not repo_path.is_absolute():
        repo_path = Path(args.workspace_dir).expanduser().resolve() / repo_path

    if not repo_path.is_dir():
        print(f"Error: Directory {repo_path} does not exist.")
        return 1

    # Get short status or diff
    res = subprocess.run(
        ["git", "status", "-s"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    diff_content = res.stdout.strip()
    if not diff_content:
        # Fallback to status
        res_status = subprocess.run(
            ["git", "status"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        diff_content = res_status.stdout.strip()

    context = str(args.context or "").strip() or "No active ticket"

    prompt = f"""You are a git assistant. Suggest 3 short, hyphenated git branch names based on the following context and diff:

Active roadmap context:
{context}

Git diff/status:
{diff_content}

CRITICAL INSTRUCTION:
Look at the "Active roadmap context" above and check if there is a roadmap ticket ID/code (e.g. CO-M01-02-03, ST-A01-02-05, CO-01-20, etc.).
If you find a ticket ID, you MUST start each suggested branch name with the ticket ID prefix without brackets, converted to lowercase, with underscores replaced by hyphens (e.g. co-m01-02-03-descriptive-name or co-01-20-descriptive-name).
If no ticket ID is present, use normal prefix keywords like 'feat-', 'fix-', or 'refactor-'.

Output ONLY the 3 suggested branch names, one per line. No comments, no formatting, no quotes, no markdown, and no explanation."""

    res_gemini = run_gemini_cli(prompt, str(repo_path))

    if res_gemini.returncode != 0:
        print(f"Error calling Gemini CLI: {res_gemini.stderr.strip()}")
        return res_gemini.returncode

    # Strip Loaded cached credentials and print only the model's suggestions
    output_lines = []
    for line in res_gemini.stdout.splitlines():
        line_strip = line.strip()
        if line_strip == "Loaded cached credentials.":
            continue
        output_lines.append(line)

    print("\n".join(output_lines).strip())
    return 0


def cmd_suggest_commit(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).expanduser()
    if not repo_path.is_absolute():
        repo_path = Path(args.workspace_dir).expanduser().resolve() / repo_path

    if not repo_path.is_dir():
        print(f"Error: Directory {repo_path} does not exist.")
        return 1

    # Get diff
    res = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    diff_content = res.stdout.strip()
    if not diff_content:
        # Fallback to status
        res_status = subprocess.run(
            ["git", "status"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        diff_content = res_status.stdout.strip()

    prompt = f"""You are a git assistant. Generate a concise, single-line commit message in the imperative mood (e.g. "add database index", "fix auth layout alignment", "refactor test runner") describing the following git diff. Do not include any prefix or branch name in the message.

Git diff:
{diff_content}

Output ONLY the raw commit message. No quotes, no markdown, and no explanation."""

    res_gemini = run_gemini_cli(prompt, str(repo_path))

    if res_gemini.returncode != 0:
        print(f"Error calling Gemini CLI: {res_gemini.stderr.strip()}")
        return res_gemini.returncode

    # Strip Loaded cached credentials and print only the model's suggestions
    output_lines = []
    for line in res_gemini.stdout.splitlines():
        line_strip = line.strip()
        if line_strip == "Loaded cached credentials.":
            continue
        output_lines.append(line)

    print("\n".join(output_lines).strip())
    return 0


def cmd_describe_changes(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).expanduser()
    if not repo_path.is_absolute():
        repo_path = Path(args.workspace_dir).expanduser().resolve() / repo_path

    if not repo_path.is_dir():
        print(f"Error: Directory {repo_path} does not exist.")
        return 1

    # Get diff
    res = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    diff_content = res.stdout.strip()
    if not diff_content:
        # Fallback to status
        res_status = subprocess.run(
            ["git", "status"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        diff_content = res_status.stdout.strip()

    prompt = f"""You are a git assistant. Generate a brief, clear bulleted summary (like a PR description, maximum 3-5 bullet points) of the following git diff. Focus on high-level achievements, not line-by-line details.

Git diff:
{diff_content}

Output ONLY the bulleted summary. Do not include introductory text, quotes, or markdown headers."""

    res_gemini = run_gemini_cli(prompt, str(repo_path))

    if res_gemini.returncode != 0:
        print(f"Error calling Gemini CLI: {res_gemini.stderr.strip()}")
        return res_gemini.returncode

    # Strip Loaded cached credentials and print only the model's suggestions
    output_lines = []
    for line in res_gemini.stdout.splitlines():
        line_strip = line.strip()
        if line_strip == "Loaded cached credentials.":
            continue
        output_lines.append(line)

    print(colorize_markdown("\n".join(output_lines).strip()))
    return 0


def cmd_create_pr(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).expanduser()
    if not repo_path.is_absolute():
        repo_path = Path(args.workspace_dir).expanduser().resolve() / repo_path

    if not repo_path.is_dir():
        print(f"Error: Directory {repo_path} does not exist.")
        return 1

    # 1. Determine current branch name
    res_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    branch_name = res_branch.stdout.strip()
    if not branch_name or branch_name == "main":
        print("Error: Cannot create a Pull Request from 'main' branch.")
        return 1

    # 2. Get commit history relative to origin/main
    res_log = subprocess.run(
        ["git", "log", "origin/main..HEAD", "--oneline"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    history = res_log.stdout.strip()
    if not history:
        # Fallback to status if no commits found relative to origin/main
        res_status = subprocess.run(
            ["git", "status", "-s"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        history = res_status.stdout.strip()

    print("Generating PR title and description using Gemini...")
    prompt = f"""You are a git assistant. Generate a pull request title and description based on the following commit/status history.

Commit history:
{history}

Your output must follow this format EXACTLY. Do not include markdown block wrappers or markdown wrapper formatting around the whole response.
TITLE: [Brief imperative title, e.g. "feat-auth: Add login page layout"]
BODY:
- [Brief bullet point 1]
- [Brief bullet point 2]
"""

    res_gemini = run_gemini_cli(prompt, str(repo_path))
    if res_gemini.returncode != 0:
        print(f"Error calling Gemini CLI: {res_gemini.stderr.strip()}")
        return res_gemini.returncode

    # Parse gemini output
    output = res_gemini.stdout.strip()
    title = ""
    body_lines = []
    in_body = False
    for line in output.splitlines():
        line_strip = line.strip()
        if line_strip == "Loaded cached credentials.":
            continue
        if line_strip.startswith("TITLE:"):
            title = line_strip[len("TITLE:"):].strip().strip('"').strip("'")
        elif line_strip.startswith("BODY:"):
            in_body = True
        elif in_body:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()
    if not title:
        title = f"PR: {branch_name}"
    if not body:
        body = f"Automated Pull Request for branch {branch_name}."

    print(f"\nCreating Pull Request:")
    print(f"  Title: {title}")
    print(f"  Body:\n{body}\n")

    # 3. Create the PR using gh CLI
    cmd = ["gh", "pr", "create", "--title", title, "--body", body]
    res_pr = subprocess.run(
        cmd,
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if res_pr.returncode != 0:
        # Check if PR already exists
        if "already exists" in res_pr.stderr or "a pull request for branch" in res_pr.stderr:
            print("A Pull Request for this branch already exists.")
            # Retrieve existing PR url
            res_view = subprocess.run(
                ["gh", "pr", "view", "--json", "url"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if res_view.returncode == 0:
                try:
                    pr_info = json.loads(res_view.stdout)
                    print(f"Existing PR: {pr_info.get('url')}")
                    return 0
                except:
                    pass
            return 0
        print(f"Error creating PR: {res_pr.stderr.strip()}")
        return res_pr.returncode

    pr_url = res_pr.stdout.strip()
    print(f"Success! Pull Request created: {pr_url}")
    return 0


def cmd_resolve_pr_review(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).expanduser()
    if not repo_path.is_absolute():
        repo_path = Path(args.workspace_dir).expanduser().resolve() / repo_path

    if not repo_path.is_dir():
        print(f"Error: Directory {repo_path} does not exist.")
        return 1

    def artifact_path(name: str) -> Path:
        path = repo_path / ".git" / name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            temp_dir = Path(tempfile.mkdtemp(prefix="tv7-conductor-pr-review-"))
            return temp_dir / name

    def display_artifact_path(path: Path) -> Path:
        try:
            return path.relative_to(repo_path)
        except ValueError:
            return path

    def write_artifact(name: str, content: str) -> Path:
        path = artifact_path(name)
        path.write_text(content)
        return display_artifact_path(path)

    def extract_verdict_and_confidence(review_text: str) -> tuple[str, str]:
        verdict = "UNKNOWN"
        confidence = "N/A"

        verdict_match = re.search(r"Final verdict:\s*(\w+)", review_text, re.IGNORECASE)
        if not verdict_match:
            verdict_match = re.search(r"FINAL_VERDICT:\s*(?:[^\w]+)?(\w+)", review_text, re.IGNORECASE)
        if verdict_match:
            verdict = verdict_match.group(1).upper()

        conf_match = re.search(r"Confidence:\s*(\d+%)", review_text, re.IGNORECASE)
        if not conf_match:
            conf_match = re.search(r"FINAL_VERDICT:\s*\w+\s*\((\d+%)\)", review_text, re.IGNORECASE)
        if conf_match:
            confidence = conf_match.group(1)

        return verdict, confidence

    def collect_section_bullets(text: str, heading: str, limit: int = 3) -> list[str]:
        bullets: list[str] = []
        in_section = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("### "):
                in_section = heading.lower() in stripped.lower()
                continue
            if not in_section:
                continue
            if stripped.startswith(("- ", "* ")):
                bullets.append(stripped[2:].strip())
            elif stripped and not bullets:
                bullets.append(stripped)
            if len(bullets) >= limit:
                break
        return bullets

    def colored_verdict(v: str) -> str:
        v_upper = v.upper()
        if "APPROVED" in v_upper:
            return color(v, C_GREEN)
        elif "REJECTED" in v_upper:
            return color(v, C_RED)
        return color(v, C_YELLOW)

    def print_review_summary(review_text: str, full_path: Path) -> tuple[str, str]:
        verdict, confidence = extract_verdict_and_confidence(review_text)
        confidence_str = color(confidence, C_CYAN) if confidence else ""
        verdict_str = colored_verdict(verdict)
        suffix = f" ({confidence_str})" if confidence_str else ""
        print(f"\nDeepSeek review: {verdict_str}{suffix}")
        print(f"Full review saved to: {color(display_artifact_path(full_path), C_MAGENTA)}")

        findings = collect_section_bullets(review_text, "Findings")
        recommendations = collect_section_bullets(review_text, "Recommendations")
        summary = collect_section_bullets(review_text, "Summary", limit=2)

        lines = findings or recommendations or summary
        if lines:
            print(color("Highlights:", C_BOLD))
            for line in lines[:3]:
                print(f"  {color('-', C_CYAN)} {line}")
        else:
            print("Highlights: no specific findings parsed from the review.")
        return verdict, confidence

    def print_gemini_summary(gem_text: str, files_to_modify: list[str], full_path: Path) -> None:
        print(f"\nGemini light analysis saved to: {color(display_artifact_path(full_path), C_MAGENTA)}")
        bullets = collect_section_bullets(gem_text, "Summary of Findings")
        if bullets:
            print(color("Gemini summary:", C_BOLD))
            for bullet in bullets[:3]:
                print(f"  {color('-', C_CYAN)} {bullet}")
        else:
            print("Gemini summary: no concise findings parsed.")
        if files_to_modify:
            colored_files = [color(f, C_CYAN) for f in files_to_modify[:6]]
            print(f"Target files: {', '.join(colored_files)}")
            if len(files_to_modify) > 6:
                print(f"  ...and {len(files_to_modify) - 6} more")
        else:
            print("Target files: none")

    def merge_pr_and_delete_branch(target_branch: str) -> int:
        res_state = subprocess.run(
            ["gh", "pr", "view", "--json", "mergeable,state"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if res_state.returncode != 0:
            print(color("Error checking PR mergeability. Please resolve on GitHub.", C_RED))
            return 1

        try:
            pr_state = json.loads(res_state.stdout)
        except json.JSONDecodeError as e:
            print(color(f"Error parsing PR mergeability status: {e}", C_RED))
            return 1

        state = str(pr_state.get("state", "")).upper()
        mergeable = str(pr_state.get("mergeable", "")).upper()
        if state != "OPEN":
            print(color(f"Cannot merge PR because its state is {state or 'UNKNOWN'}.", C_RED))
            return 1
        if mergeable not in {"MERGEABLE", "TRUE"}:
            print(color(f"Cannot merge PR because mergeability is {mergeable or 'UNKNOWN'}.", C_RED))
            print("Resolve conflicts and required checks on GitHub, then rerun this command.")
            return 1

        print(color("Merging Pull Request...", C_CYAN))
        res_merge = subprocess.run(
            ["gh", "pr", "merge", "--merge", "--delete-branch"],
            cwd=str(repo_path),
            check=False,
        )
        if res_merge.returncode != 0:
            print(color("Error merging PR. Please resolve on GitHub.", C_RED))
            return 1

        print(color("PR merged and remote branch deleted successfully.", C_GREEN))
        print(color("Switching back to main and pulling updates...", C_CYAN))

        res_checkout = subprocess.run(
            ["git", "checkout", "main"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if res_checkout.returncode != 0:
            print(color(f"Error checking out main: {(res_checkout.stderr or res_checkout.stdout or '').strip()}", C_RED))
            return res_checkout.returncode
        print(f"  {color('✔', C_GREEN)} Checked out branch main.")

        res_pull = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if res_pull.returncode != 0:
            print(color("  ⚠ Warning: Failed to pull main updates automatically. Reconcile divergent branches manually.", C_YELLOW))
            detail = (res_pull.stderr or res_pull.stdout or "").strip()
            if detail:
                lines = [line.strip() for line in detail.splitlines() if line.strip()]
                print(f"    Detail: {'; '.join(lines[:3])}")
        else:
            print(f"  {color('✔', C_GREEN)} Pulled latest updates for main.")

        if target_branch and target_branch != "main":
            res_delete = subprocess.run(
                ["git", "branch", "-D", target_branch],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if res_delete.returncode != 0:
                detail = (res_delete.stderr or res_delete.stdout or "").strip()
                if "not found" in detail.lower():
                    print(f"  {color('✔', C_GREEN)} Local branch {color(target_branch, C_CYAN)} not found (already deleted).")
                else:
                    print(color(f"Error deleting local branch {target_branch}: {detail}", C_RED))
                    return res_delete.returncode
            else:
                print(f"  {color('✔', C_GREEN)} Local branch {color(target_branch, C_CYAN)} deleted.")
        return 0

    def codex_command(read_only: bool) -> list[str]:
        cmd = ["codex", "exec", "--full-auto", "-c", "reasoning_effort=low"]
        model = os.environ.get("TV7_CODEX_MODEL", "").strip()
        if model:
            cmd.extend(["-m", model])
        if read_only:
            cmd.extend(["-s", "read-only"])
        cmd.extend(["-C", str(repo_path), "-"])
        return cmd

    def run_codex_quiet(prompt: str, *, log_name: str, read_only: bool, label: str) -> tuple[int, Path]:
        log_path = artifact_path(log_name)
        print(f"\n{color(label + '...', C_CYAN)}")
        print(f"Full Codex log: {color(display_artifact_path(log_path), C_MAGENTA)}")

        start_time = time.time()
        with log_path.open("w") as log_file:
            process = subprocess.Popen(
                codex_command(read_only),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(prompt)
            process.stdin.close()

            last_action = ""
            for line in process.stdout:
                log_file.write(line)
                log_file.flush()
                stripped = line.strip()
                if not stripped:
                    continue
                lowered = stripped.lower()
                if stripped.startswith("edit:") or stripped.startswith("write:"):
                    last_action = "editing files"
                elif stripped.startswith("read:"):
                    last_action = "reading files"
                elif "exec" in lowered or "/bin/" in stripped:
                    last_action = "running commands"

            process.wait()
        elapsed_time = time.time() - start_time

        # Parse log file for metrics
        tokens_used = None
        model_used = None
        reasoning_effort = None
        try:
            log_content = log_path.read_text()
            model_match = re.search(r"(?m)^model:\s*(.*)$", log_content)
            if model_match:
                model_used = model_match.group(1).strip()
            reasoning_match = re.search(r"(?m)^reasoning effort:\s*(.*)$", log_content)
            if reasoning_match:
                reasoning_effort = reasoning_match.group(1).strip()
            tokens_match = re.search(r"tokens used\s*\n\s*([0-9,]+)", log_content)
            if tokens_match:
                tokens_used = tokens_match.group(1).strip()
        except OSError:
            pass

        metrics = []
        if model_used:
            metrics.append(f"model: {color(model_used, C_CYAN)}")
        if reasoning_effort:
            metrics.append(f"reasoning: {color(reasoning_effort, C_CYAN)}")
        if tokens_used:
            metrics.append(f"tokens: {color(tokens_used, C_YELLOW)}")
        metrics.append(f"time: {color(f'{elapsed_time:.1f}s', C_CYAN)}")
        metrics_str = f" [{', '.join(metrics)}]"

        if process.returncode == 0:
            suffix = f" ({last_action})" if last_action else ""
            print(f"{color(label + ' complete', C_GREEN)}{suffix}{metrics_str}.")
        else:
            print(f"{color(label + ' failed', C_RED)} with exit code {process.returncode}{metrics_str}.")
            try:
                lines = log_path.read_text().splitlines()
                tail = [line for line in lines[-20:] if line.strip()]
                if tail:
                    print("Last log lines:")
                    for line in tail[-8:]:
                        print(f"  {line}")
            except OSError:
                pass
        return process.returncode, log_path

    def print_codex_plan_summary(plan_path: Path) -> None:
        lines = []
        try:
            for raw in plan_path.read_text().splitlines():
                line = raw.strip()
                if not line:
                    continue
                if line in {"codex", "tokens used"}:
                    continue
                if line.startswith(("-", "*")):
                    candidate = line.lstrip("-* ").strip()
                    if candidate:
                        lines.append(candidate)
                elif line.lower().startswith(("files", "changes", "plan")):
                    lines.append(line)
                if len(lines) >= 6:
                    break
        except OSError:
            lines = []

        print(f"Codex technical plan saved to: {color(display_artifact_path(plan_path), C_MAGENTA)}")
        if lines:
            print(color("Codex plan summary:", C_BOLD))
            for line in lines[:4]:
                print(f"  {color('-', C_CYAN)} {line}")
        else:
            print("Codex plan summary: see saved log for details.")

    def extract_codex_final_message(log_path: Path, *, limit: int = 8) -> list[str]:
        try:
            text = log_path.read_text()
        except OSError:
            return []

        sections = re.split(r"(?m)^codex\s*$", text)
        section = sections[-1] if sections else text
        section = re.split(r"(?m)^tokens used\s*$", section)[0]
        section = re.split(r"(?m)^diff --git ", section)[0]

        lines: list[str] = []
        for raw in section.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith(("warning:", "OpenAI Codex", "--------", "workdir:", "model:", "provider:", "approval:", "sandbox:", "reasoning", "session id:", "user")):
                continue
            if line.startswith(("exec", "/bin/", "succeeded in", "failed in")):
                continue
            if line in {"codex"}:
                continue
            lines.append(line)
            if len(lines) >= limit:
                break
        return lines

    def print_codex_message(title: str, log_path: Path) -> None:
        lines = extract_codex_final_message(log_path)
        print(f"\n{color(title + ':', C_BOLD)}")
        if not lines:
            print(f"  See saved log: {color(display_artifact_path(log_path), C_MAGENTA)}")
            return
        for line in lines:
            if line.startswith("###"):
                print(f"  {color(line, C_YELLOW + C_BOLD)}")
            elif line.startswith(("-", "*")):
                print(f"  {color('-', C_CYAN)} {line[1:].strip()}")
            else:
                print(f"  {line}")

    def changed_files_from_status(status_output: str) -> list[str]:
        files: list[str] = []
        for line in status_output.splitlines():
            if not line:
                continue
            status_part = line[:2]
            file_part = line[3:].strip()
            if " -> " in file_part:
                file_part = file_part.split(" -> ", 1)[1].strip()
            if status_part == "??" or any(ch in status_part for ch in "MADRC"):
                files.append(file_part)
        return files

    def unmerged_files() -> list[str]:
        res = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def stage_pr_review_files(files: list[str]) -> bool:
        unresolved = unmerged_files()
        if unresolved:
            print("Cannot stage review fixes while merge conflicts remain:")
            for path in unresolved:
                print(f"  - {path}")
            return False

        for f in files:
            print(f"Staging changed file: {f}")
            subprocess.run(
                ["git", "add", "--", f],
                cwd=str(repo_path),
                check=False,
            )
        return True

    def parse_github_timestamp(value: object) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def get_item_timestamp(item) -> datetime | None:
        for key in ["submittedAt", "createdAt", "submitted_at", "created_at"]:
            val = item.get(key)
            if val:
                parsed = parse_github_timestamp(val)
                if parsed is not None:
                    return parsed
        return None

    def next_poll_delay(attempt: int, remaining: float) -> float:
        base_delay = min(30.0, 5.0 * (2 ** max(attempt - 1, 0)))
        jitter = random.uniform(0.0, min(3.0, base_delay * 0.25))
        return max(0.0, min(remaining, base_delay + jitter))

    # 1. Fetch current open PR comments
    print(color("Retrieving PR details from GitHub...", C_CYAN))
    res_pr = subprocess.run(
        ["gh", "pr", "view", "--json", "number,url,comments,reviews"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if res_pr.returncode != 0:
        print(color("No open Pull Request found for this branch. Please create a PR first.", C_RED))
        return 1

    try:
        pr_data = json.loads(res_pr.stdout)
    except Exception as e:
        print(color(f"Error parsing PR JSON data: {e}", C_RED))
        return 1

    pr_number = pr_data.get("number")
    pr_url = pr_data.get("url")
    print(f"Found active PR #{pr_number}: {color(pr_url, C_CYAN)}")

    # Determine current branch name
    res_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    branch_name = res_branch.stdout.strip()

    # Loop for automatic review resolution
    last_processed_timestamp: datetime | None = None
    timeout = 600  # 10 minutes
    max_resolution_cycles = 3
    resolution_cycles = 0

    while True:
        # Polling for DeepSeek review comments
        deepseek_review_text = ""
        deepseek_review_time: datetime | None = None
        start_time = time.time()
        poll_attempt = 0

        print(f"\n{color('Checking for DeepSeek review comment on GitHub...', C_CYAN)}")
        while True:
            # Fetch comments/reviews again
            res_view = subprocess.run(
                ["gh", "pr", "view", "--json", "comments,reviews"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if res_view.returncode == 0:
                try:
                    view_data = json.loads(res_view.stdout)
                    items = []
                    for r in view_data.get("reviews", []):
                        items.append(r)
                    for c in view_data.get("comments", []):
                        items.append(c)

                    # Sort items by timestamp descending to get the most recent first
                    items.sort(
                        key=lambda item: get_item_timestamp(item) or datetime.min.replace(tzinfo=timezone.utc),
                        reverse=True,
                    )

                    # Search for DeepSeek bot reviews
                    for item in items:
                        body = str(item.get("body", ""))
                        if "DeepSeek review" in body:
                            timestamp = get_item_timestamp(item)
                            # If we have a last_processed_timestamp, make sure this one is newer!
                            if timestamp is not None and (
                                last_processed_timestamp is None or timestamp > last_processed_timestamp
                            ):
                                deepseek_review_text = body
                                deepseek_review_time = timestamp
                                break
                except Exception as e:
                    print(color(f"Parsing error during poll: {e}", C_RED))

            if deepseek_review_text:
                print(f"\n{color('Found DeepSeek review comment!', C_GREEN)}")
                break

            elapsed = time.time() - start_time
            if elapsed >= timeout:
                print(color(f"\nTimeout: DeepSeek review comment not found after {timeout} seconds.", C_RED))
                if last_processed_timestamp is not None:
                    print("No new review posted after our push. You can rerun this command once the review is posted.")
                return 1

            poll_attempt += 1
            delay = next_poll_delay(poll_attempt, timeout - elapsed)
            print(
                f"Waiting for DeepSeek review... ({int(elapsed)}s elapsed, next check in {delay:.1f}s)",
                end="\r",
                flush=True,
            )
            time.sleep(delay)

        review_display_path = write_artifact(
            f"deepseek-review-pr-{pr_number}.md",
            deepseek_review_text,
        )
        verdict, confidence = print_review_summary(deepseek_review_text, review_display_path)

        print(f"\n{color('What would you like to do?', C_BOLD)}")
        print(f"  {color('1)', C_GREEN)} Review findings with Gemini")
        print(f"  {color('2)', C_GREEN)} Merge Pull Request and delete branch")
        print(f"  {color('3)', C_GREEN)} Exit (keep PR open)")
        choice = input("Select an option [1-3, default: 3]: ").strip() or "3"

        if choice == "3":
            print("Exiting.")
            return 0
        if choice == "2":
            return merge_pr_and_delete_branch(branch_name)
        if choice != "1":
            print(color("Invalid option. Exiting.", C_RED))
            return 1

        # Light analysis / summary using Gemini
        print("\nRunning Gemini light analysis...")
        summary_prompt = f"""You are a code review assistant. Analyze the following DeepSeek review comment and produce a structured response.

Your response MUST follow this exact format:

### Summary of Findings
<Provide a concise summary of the findings, maximum 3 bullet points, for the developer.>

### Files to Modify
<List the files in the repository that need to be modified or inspected to address the findings. Use the format:
- filepath: brief description of required change
If no files need modification, list:
- none
>

### Additional Technical Context
<Provide key technical context, constraints, or hints that will help an automated coder (Codex) resolve these findings successfully.>

DeepSeek review comment:
{deepseek_review_text}"""
        res_gem = run_gemini_cli(summary_prompt, str(repo_path))
        gem_text = ""
        files_to_modify = []
        if res_gem.returncode == 0:
            gem_output = []
            for line in res_gem.stdout.splitlines():
                if line.strip() == "Loaded cached credentials.":
                    continue
                gem_output.append(line)
            gem_text = "\n".join(gem_output).strip()

            # Parse files to modify
            in_files_section = False
            for line in gem_output:
                line_strip = line.strip()
                if "### Files to Modify" in line:
                    in_files_section = True
                    continue
                elif line_strip.startswith("###"):
                    in_files_section = False

                if in_files_section:
                    if line_strip.startswith("- ") or line_strip.startswith("* "):
                        path_part = line_strip[2:].split(":")[0].strip()
                        path_part = path_part.strip("`'\"")
                        if path_part and path_part.lower() != "none":
                            files_to_modify.append(path_part)
            gem_display_path = write_artifact(
                f"gemini-review-analysis-pr-{pr_number}.md",
                gem_text,
            )
            print_gemini_summary(gem_text, files_to_modify, gem_display_path)
        else:
            gem_display_path = write_artifact(
                f"gemini-review-analysis-pr-{pr_number}.log",
                (res_gem.stdout or "") + (res_gem.stderr or ""),
            )
            print(color(f"Gemini analysis failed. Output saved to: {gem_display_path}", C_RED))

        verdict_str = colored_verdict(verdict)
        confidence_str = color(confidence, C_CYAN) if confidence else ""
        suffix = f" (Confidence: {confidence_str})" if confidence_str else ""
        print(f"\nDeepSeek Review Verdict: {verdict_str}{suffix}")
        print(color("What would you like to do next?", C_BOLD))
        print(f"  {color('1)', C_GREEN)} Run Codex technical plan only")
        print(f"  {color('2)', C_GREEN)} Resolve findings using Codex")
        print(f"  {color('3)', C_GREEN)} Merge Pull Request and delete branch")
        print(f"  {color('4)', C_GREEN)} Exit (keep PR open)")
        choice = input("Select an option [1-4, default: 4]: ").strip() or "4"

        if choice == "4":
            print("Exiting.")
            return 0
        if choice == "3":
            return merge_pr_and_delete_branch(branch_name)
        if choice not in {"1", "2"}:
            print(color("Invalid option. Exiting.", C_RED))
            return 1

        analysis_prompt = f"""Analyze the code review findings listed in the following DeepSeek review comment against the current repository state. Do NOT modify any files.

For each finding, classify it as exactly one of:
- valid: current code still has the issue and needs a change.
- stale: review describes older code that is already fixed in the current checkout.
- false_positive: review claim is incorrect; cite current code or command evidence.
- needs_human: insufficient evidence to decide safely.

Only list implementation changes for valid findings. Keep the response concise and technical.

Required response format:
### Valid Findings
- <finding>: <evidence and intended change>

### Stale Or False Positive Findings
- <finding>: <why no change should be made>

### Files To Modify
- <path or none>: <reason>

Gemini Analysis:
{gem_text}

DeepSeek review findings:
{deepseek_review_text}"""

        rc_plan, plan_path = run_codex_quiet(
            analysis_prompt,
            log_name=f"codex-plan-pr-{pr_number}.md",
            read_only=True,
            label="Running Codex technical plan",
        )
        print_codex_plan_summary(plan_path)
        print_codex_message("Codex plan approach", plan_path)
        if rc_plan != 0:
            return rc_plan

        if choice == "1":
            return 0

        if choice == "2":
            # Formulate prompt for Codex to resolve findings
            metadata_lines = []
            if files_to_modify:
                metadata_lines.append("Target files to inspect and modify:")
                for f in files_to_modify:
                    metadata_lines.append(f"  - {f}")
                metadata_lines.append("")

            metadata_text = "\n".join(metadata_lines)

            prompt = f"""Resolve only the valid code review findings listed below. Before changing code, compare each DeepSeek finding against the current repository state and the Codex Technical Plan.

Rules:
- Implement only findings classified as valid in the plan.
- Do not modify code for stale findings or false positives.
- If all findings are stale or false positives, exit without modifying files and explain why.
- If a finding concerns Codex CLI flags or model names, verify against `codex exec --help` or current environment behavior before changing code.
- Ensure the code compiles and targeted tests pass.

If you analyze the findings and determine that no code changes are necessary (e.g. because the issues are already resolved, or the findings are false positives/warnings that don't apply, or the PR is fully approved without issues), exit immediately without making any modifications.

{metadata_text}Gemini Analysis Details:
{gem_text}

Codex Technical Plan:
{plan_path.read_text() if plan_path.exists() else ""}

DeepSeek review findings:
{deepseek_review_text}"""

            rc_resolve, log_path = run_codex_quiet(
                prompt,
                log_name=f"codex-resolve-pr-{pr_number}.log",
                read_only=False,
                label="Resolving findings with Codex",
            )
            if rc_resolve != 0:
                return rc_resolve
            print_codex_message("Codex resolution", log_path)

            # Check if changes were made
            res_status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
            modified_files = changed_files_from_status(res_status.stdout)

            if not modified_files:
                print(color("\nNo code changes were made by Codex.", C_YELLOW))

                print(color("\nWould you like to merge this Pull Request and delete the branch?", C_BOLD))
                print(f"  {color('1)', C_GREEN)} Merge PR and delete branch")
                print(f"  {color('2)', C_GREEN)} Keep PR open and exit")
                choice_merge = input("Select an option [1-2, default: 2]: ").strip() or "2"
                if choice_merge == "1":
                    return merge_pr_and_delete_branch(branch_name)
                else:
                    return 0

            # Staging resolved files
            print(color("\nCodex completed successfully. Staging modified files...", C_GREEN))
            if not stage_pr_review_files(modified_files):
                print(color("Review fixes were left unstaged. Resolve conflicts and rerun the PR review flow.", C_RED))
                return 1

            res_verify = subprocess.run(
                ["git", "diff", "--name-only"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
            remaining = [line.strip() for line in res_verify.stdout.splitlines() if line.strip()]
            if remaining:
                print(color("Some changes are still unstaged after staging review fixes:", C_RED))
                for path in remaining:
                    print(f"  - {path}")
                print(color("Review the listed files, then rerun the PR review flow.", C_RED))
                return 1

            # Commit and push changes
            commit_msg = "chore: resolve DeepSeek review findings"
            print(f"\n{color('Creating commit:', C_CYAN)} '{color(commit_msg, C_GREEN)}'")
            res_commit = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=str(repo_path),
                check=False,
            )
            if res_commit.returncode != 0:
                print(color("Error committing. Exiting.", C_RED))
                return res_commit.returncode

            print(color("Pushing commits to remote to trigger a new DeepSeek review...", C_CYAN))
            push_env = {**os.environ, "TV7_ALLOW_PUSH": "1"}
            res_push = subprocess.run(
                ["git", "push"],
                cwd=str(repo_path),
                env=push_env,
                check=False,
            )
            if res_push.returncode != 0:
                print(color("Error pushing to remote. Exiting.", C_RED))
                return res_push.returncode

            if deepseek_review_time is None:
                print(color("Error: DeepSeek review timestamp was not available after processing.", C_RED))
                return 1

            last_processed_timestamp = deepseek_review_time
            resolution_cycles += 1
            if resolution_cycles >= max_resolution_cycles:
                print(
                    f"\nReached maximum automatic review resolution cycles "
                    f"({max_resolution_cycles}). Rerun this command to continue."
                )
                return 1
            print("\nWaiting for the new DeepSeek review comment to be posted...")


def cmd_resolve_conflicts(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).expanduser()
    if not repo_path.is_absolute():
        repo_path = Path(args.workspace_dir).expanduser().resolve() / repo_path

    if not repo_path.is_dir():
        print(f"Error: Directory {repo_path} does not exist.")
        return 1

    # Scan for unmerged conflict files
    res = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    unmerged_files = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    if not unmerged_files:
        print(f"No unmerged conflict files found in {repo_path}.")
        return 0

    print(f"Detected unmerged conflict files in {repo_path}:")
    for f in unmerged_files:
        print(f"  - {f}")

    prompt_lines = [
        "Resolve the merge conflicts in this repository. Focus on files with conflict markers.",
        "Ensure all conflict markers are cleared, and verify code is syntactically correct.",
        "",
        "Unmerged conflict files to resolve:",
    ]
    for f in unmerged_files:
        prompt_lines.append(f"- {f}")

    prompt_text = "\n".join(prompt_lines)

    log_path = repo_path / ".git" / "codex-resolve-conflicts.log"
    log_file = None
    try:
        log_file = open(log_path, "w")
        display_path = log_path.relative_to(repo_path)
    except OSError:
        temp_file = tempfile.NamedTemporaryFile(prefix="codex-resolve-conflicts-", suffix=".log", delete=False)
        log_path = Path(temp_file.name)
        log_file = open(log_path, "w")
        display_path = log_path

    print(f"\nLaunching Codex CLI on {repo_path} to resolve conflicts...")
    print(f"Streaming logs to: {display_path}")
    cmd = ["codex", "exec", "--full-auto", "-C", str(repo_path), "-"]

    try:
        p = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        p.stdin.write(prompt_text)
        p.stdin.close()

        curr_exec_cmd = None
        for line in p.stdout:
            log_file.write(line)
            log_file.flush()

            line_strip = line.strip()
            if not line_strip:
                continue

            if line_strip.startswith("/bin/zsh -lc '") or line_strip.startswith("/bin/bash -c '"):
                parts = line_strip.split("'")
                if len(parts) > 1:
                    curr_exec_cmd = parts[1]
                    print(f"  ➜ Running: {curr_exec_cmd}")
            elif "succeeded in" in line_strip:
                if curr_exec_cmd:
                    print(f"    ✔ Succeeded ({line_strip.split()[-1]})")
                    curr_exec_cmd = None
            elif "failed in" in line_strip:
                if curr_exec_cmd:
                    print(f"    ✘ Failed ({line_strip.split()[-1]})")
                    curr_exec_cmd = None
            elif line_strip.startswith("edit:") or line_strip.startswith("write:") or line_strip.startswith("read:"):
                print(f"  ➜ {line_strip}")
            elif "tool call" in line_strip.lower() or "calling" in line_strip.lower():
                print(f"  ➜ {line_strip}")

        p.wait()
    finally:
        if log_file:
            log_file.close()

    if p.returncode != 0:
        print(f"\nCodex execution failed with exit code {p.returncode}.")
        print("Showing last 20 lines of logs:")
        try:
            log_lines = log_path.read_text().splitlines()
            for line in log_lines[-20:]:
                print(f"  {line}")
        except Exception:
            pass
        return p.returncode

    if p.returncode == 0:
        print(f"\nCodex completed successfully. Log retained at: {display_path}")
        print("Checking if conflicts are resolved...")
        # Auto-stage files that no longer contain conflict markers
        for f in unmerged_files:
            file_path = repo_path / f
            if file_path.is_file():
                try:
                    content = file_path.read_text()
                    if "<<<<<<<" not in content and "=======" not in content and ">>>>>>>" not in content:
                        print(f"Staging resolved file: {f}")
                        subprocess.run(
                            ["git", "add", f],
                            cwd=str(repo_path),
                            check=False,
                        )
                except Exception as e:
                    print(f"Error reading/staging {f}: {e}")

        res_verify = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        remaining = [line.strip() for line in res_verify.stdout.splitlines() if line.strip()]
        if remaining:
            print("Warning: Some conflicts might still remain:")
            for f in remaining:
                print(f"  - {f}")
            return 1
        else:
            print("Success! All conflicts resolved.")
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
        help="Limit sync to item id prefix(es), e.g. SI-A01 or BE-B01-02.",
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

    p_resolve = sub.add_parser(
        "resolve-conflicts",
        help="Scan for and automatically resolve merge conflicts in a repo.",
    )
    p_resolve.add_argument(
        "--repo",
        required=True,
        help="Name or path of the repository to check.",
    )
    p_resolve.set_defaults(func=cmd_resolve_conflicts)

    p_suggest_branch = sub.add_parser(
        "suggest-branch",
        help="Query Gemini to suggest branch names based on current changes and roadmap context.",
    )
    p_suggest_branch.add_argument(
        "--repo",
        required=True,
        help="Name or path of the repository.",
    )
    p_suggest_branch.add_argument(
        "--context",
        default="No active ticket",
        help="Optional roadmap ticket context.",
    )
    p_suggest_branch.set_defaults(func=cmd_suggest_branch)

    p_suggest_commit = sub.add_parser(
        "suggest-commit",
        help="Query Gemini to suggest a commit message based on current changes.",
    )
    p_suggest_commit.add_argument(
        "--repo",
        required=True,
        help="Name or path of the repository.",
    )
    p_suggest_commit.set_defaults(func=cmd_suggest_commit)

    p_describe = sub.add_parser(
        "describe-changes",
        help="Query Gemini to generate a brief bulleted summary of pending changes.",
    )
    p_describe.add_argument(
        "--repo",
        required=True,
        help="Name or path of the repository.",
    )
    p_describe.set_defaults(func=cmd_describe_changes)

    p_create_pr = sub.add_parser(
        "create-pr",
        help="Generate PR title and description and create it on GitHub.",
    )
    p_create_pr.add_argument(
        "--repo",
        required=True,
        help="Name or path of the repository.",
    )
    p_create_pr.set_defaults(func=cmd_create_pr)

    p_resolve_pr_review = sub.add_parser(
        "resolve-pr-review",
        help="Poll for DeepSeek review comment and invoke Codex to automatically resolve findings.",
    )
    p_resolve_pr_review.add_argument(
        "--repo",
        required=True,
        help="Name or path of the repository.",
    )
    p_resolve_pr_review.set_defaults(func=cmd_resolve_pr_review)

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
