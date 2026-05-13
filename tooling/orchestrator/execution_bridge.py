from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any

from .execution_policy import load_execution_policy
from .idempotency import has_idempotency_key, make_idempotency_key, record_idempotency_key
from .lifecycle import WorkStage, transition_item_stage
from .models import now_iso
from .repo_defaults import repo_defaults_for
from .state_store import append_event, paths

RE_PR_URL = re.compile(r"https://github\.com/[^\s]+/pull/\d+")
RE_ISSUE_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/issues/(\d+)")
RE_SESSION_ID = re.compile(r"SESSION=([a-zA-Z0-9._-]+)")


def _env_truthy(name: str, default: str = "1") -> bool:
    raw = str(os.environ.get(name, default)).strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def _split_values(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        for part in str(raw).split(","):
            token = part.strip()
            if token:
                out.append(token)
    return out


def _issue_url(item: dict[str, Any], repo: str) -> str:
    issues = item.get("issues", {})
    if not isinstance(issues, dict):
        return ""
    payload = issues.get(repo, {})
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("url", "")).strip()


def _spawn_issue_ref(item: dict[str, Any], repo: str, fallback: str) -> str:
    issues = item.get("issues", {})
    if not isinstance(issues, dict):
        return fallback
    payload = issues.get(repo, {})
    if not isinstance(payload, dict):
        return fallback
    number = payload.get("number")
    if isinstance(number, int) and number > 0:
        return str(number)
    number_str = str(number or "").strip()
    if number_str.isdigit():
        return number_str
    url = str(payload.get("url", "")).strip()
    if url:
        match = RE_ISSUE_URL.search(url)
        if match:
            return str(match.group(1))
    return fallback


def _safe_git(repo_path: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return str(result.stdout).strip()


def _extract_json_list_from_text(raw: str) -> list[dict[str, Any]]:
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


def _extract_session_id(stdout: str) -> str:
    match = RE_SESSION_ID.search(str(stdout or ""))
    if not match:
        return ""
    return str(match.group(1)).strip()


def _ao_state_root() -> Path:
    configured = str(os.environ.get("TV7_AO_STATE_DIR", "")).strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".agent-orchestrator"


def _session_meta_file(*, project: str, session_id: str) -> Path | None:
    if not project or not session_id:
        return None
    root = _ao_state_root()
    if not root.is_dir():
        return None
    pattern = f"*-{project}/sessions/{session_id}"
    candidates = sorted(path for path in root.glob(pattern) if path.is_file())
    if not candidates:
        return None
    return candidates[0]


def _session_tmux_name(*, project: str, session_id: str) -> str:
    meta_file = _session_meta_file(project=project, session_id=session_id)
    if not meta_file:
        return ""
    try:
        raw = meta_file.read_text()
    except Exception:
        return ""
    for line in raw.splitlines():
        if not line.startswith("tmuxName="):
            continue
        return line.split("=", 1)[1].strip()
    return ""


def _capture_tmux_pane(*, tmux_name: str, lines: int = 600) -> str:
    if not tmux_name:
        return ""
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-S", f"-{max(20, int(lines))}", "-t", tmux_name],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return str(result.stdout or "")


def _send_tmux_approval(*, tmux_name: str) -> bool:
    if not tmux_name:
        return False
    result = subprocess.run(
        ["tmux", "send-keys", "-t", tmux_name, "1", "Enter"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _is_codex_workspace_trust_prompt(raw: str) -> bool:
    text = str(raw or "").lower()
    if not text:
        return False
    return (
        "you are running codex in" in text
        and "allow codex to work in this folder without asking for approval" in text
        and "press enter to continue" in text
    )


def _attempt_codex_workspace_trust_unblock(
    *,
    ao_command: str,
    repo_path: Path,
    project: str,
    session_id: str,
    health_hint: str,
) -> str:
    if not _env_truthy("TV7_EXEC_AUTO_APPROVE_CODEX_TRUST", "1"):
        return "disabled"
    if not session_id:
        return "no_session"

    def _send_approval(reason: str) -> str:
        result = subprocess.run(
            [ao_command, "send", "--no-wait", session_id, "1"],
            cwd=repo_path,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return f"{reason}_send_failed"
        settle = max(0.0, float(os.environ.get("TV7_EXEC_TRUST_APPROVE_SETTLE_SEC", "0.6")))
        if settle > 0:
            time.sleep(settle)
        return reason

    allow_fallback = _env_truthy("TV7_EXEC_AUTO_APPROVE_CODEX_TRUST_FALLBACK", "1")
    health_text = str(health_hint or "").strip().lower()
    should_try_fallback = allow_fallback and health_text.startswith("spawning/")

    tmux_name = _session_tmux_name(project=project, session_id=session_id)
    if not tmux_name:
        if should_try_fallback:
            return _send_approval("blind_approve_sent")
        return "tmux_unresolved"
    pane = _capture_tmux_pane(tmux_name=tmux_name)
    if not _is_codex_workspace_trust_prompt(pane):
        if should_try_fallback:
            if _send_tmux_approval(tmux_name=tmux_name):
                settle = max(0.0, float(os.environ.get("TV7_EXEC_TRUST_APPROVE_SETTLE_SEC", "0.6")))
                if settle > 0:
                    time.sleep(settle)
                return "blind_tmux_keys_sent"
            return _send_approval("blind_approve_sent")
        return "prompt_not_detected"
    if _send_tmux_approval(tmux_name=tmux_name):
        settle = max(0.0, float(os.environ.get("TV7_EXEC_TRUST_APPROVE_SETTLE_SEC", "0.6")))
        if settle > 0:
            time.sleep(settle)
        return "codex_workspace_trust_approved_tmux"
    return _send_approval("codex_workspace_trust_approved")


def _query_ao_session_status(*, ao_command: str, repo_path: Path, project: str, session_id: str) -> tuple[str, str]:
    if not session_id:
        return "", ""
    result = subprocess.run(
        [ao_command, "status", "--json"],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return "", ""
    rows = _extract_json_list_from_text(result.stdout)
    for row in rows:
        if str(row.get("name", "")).strip() != session_id:
            continue
        if str(row.get("project", "")).strip() != str(project).strip():
            continue
        status = str(row.get("status", "")).strip().lower()
        activity = str(row.get("activity", "")).strip().lower()
        return status, activity
    return "", ""


def _spawn_health_check(
    *,
    ao_command: str,
    repo_path: Path,
    project: str,
    session_id: str,
) -> tuple[bool, str]:
    if not session_id:
        return True, "unknown"
    polls = max(1, int(os.environ.get("TV7_EXEC_SPAWN_HEALTH_POLLS", "12")))
    poll_sleep = max(0.0, float(os.environ.get("TV7_EXEC_SPAWN_HEALTH_POLL_SEC", "1.0")))
    last_seen = "unknown"
    saw_spawning = False
    for attempt in range(polls):
        status, activity = _query_ao_session_status(
            ao_command=ao_command,
            repo_path=repo_path,
            project=project,
            session_id=session_id,
        )
        if status or activity:
            last_seen = f"{status or 'unknown'}/{activity or 'unknown'}"
            if status in {"killed", "failed", "exited"} or activity in {"exited"}:
                return False, last_seen
            if status in {"working", "pr_open", "review_pending", "approved", "mergeable", "merged"}:
                return True, last_seen
            if status == "spawning":
                saw_spawning = True
        if attempt + 1 < polls and poll_sleep > 0:
            time.sleep(poll_sleep)
    if saw_spawning:
        return False, last_seen
    return True, last_seen


def _write_log(
    *,
    log_file: Path,
    cmd: list[str],
    env_keys: dict[str, str],
    result: subprocess.CompletedProcess[str],
) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"$ {' '.join(cmd)}",
        f"exit={result.returncode}",
        "",
        "--- env ---",
    ]
    for key, value in sorted(env_keys.items()):
        lines.append(f"{key}={value}")
    lines.extend(
        [
            "",
            "--- stdout ---",
            str(result.stdout),
            "",
            "--- stderr ---",
            str(result.stderr),
        ]
    )
    log_file.write_text("\n".join(lines) + "\n")


def execute_repo_handoffs(
    *,
    core_dir: Path,
    state: dict[str, Any],
    apply_changes: bool,
    ao_command: str,
    policy_path: str,
    batch_id: str,
    progress: bool,
    repo_filters: list[str] | None = None,
    roadmap_filters: list[str] | None = None,
    item_prefix_filters: list[str] | None = None,
    limit: int = 0,
    resume_from: str = "",
    force: bool = False,
) -> tuple[int, int, int, int, int, int, list[dict[str, Any]]]:
    if apply_changes and shutil.which(ao_command) is None:
        raise RuntimeError(f"Missing required command: {ao_command}")

    policy = load_execution_policy(policy_path or None)
    repo_filter_set = set(_split_values(repo_filters))
    roadmap_filter_values = _split_values(roadmap_filters)
    item_prefixes = [x.upper() for x in _split_values(item_prefix_filters)]
    roadmap_filter_names = {Path(x).name for x in roadmap_filter_values}
    roadmap_filter_paths = {str(Path(x).resolve()) for x in roadmap_filter_values if x}
    eligible_stages = {
        WorkStage.EXPANDED.value,
        WorkStage.REVIEW_FAILED.value,
        WorkStage.NEEDS_HUMAN.value,
    }

    def _progress(msg: str) -> None:
        if progress:
            print(msg, flush=True)

    def _matches_item(item: dict[str, Any]) -> bool:
        stage = str(item.get("stage", WorkStage.SEEDED.value)).strip().lower()
        if stage not in eligible_stages:
            if not (force and stage == WorkStage.EXECUTING.value):
                return False
        if stage == WorkStage.DONE.value:
            return False
        item_id = str(item.get("item_id", "")).upper()
        if item_prefixes and not any(item_id.startswith(prefix) for prefix in item_prefixes):
            return False
        if roadmap_filter_values:
            source_file = str(item.get("source_file", ""))
            if not source_file:
                return False
            source_name = Path(source_file).name
            source_abs = str(Path(source_file).resolve())
            if source_name not in roadmap_filter_names and source_abs not in roadmap_filter_paths:
                return False
        return True

    def _target_repos(item: dict[str, Any]) -> list[str]:
        repos = [str(x) for x in item.get("repos", []) if str(x).strip()]
        if not repo_filter_set:
            return repos
        return [repo for repo in repos if repo in repo_filter_set]

    filtered_items: list[dict[str, Any]] = []
    for it in state.get("work_items", []):
        if isinstance(it, dict) and _matches_item(it):
            filtered_items.append(it)

    targets: list[tuple[dict[str, Any], str]] = []
    for item in filtered_items:
        for repo in _target_repos(item):
            targets.append((item, repo))
    targets.sort(key=lambda pair: (str(pair[0].get("item_id", "")), pair[1]))

    stamp = now_iso().replace(":", "").replace("-", "").replace("+00:00", "z")
    run_dir = paths(core_dir)["runs"] / f"execute-{stamp}"
    logs_dir = run_dir / "logs"
    meta_dir = run_dir / "payloads"

    resume_token = str(resume_from).strip().upper()
    started = not bool(resume_token)
    max_targets = max(0, int(limit))
    processed = 0

    spawned = 0
    failed = 0
    skipped = 0
    planned = 0
    actions: list[dict[str, Any]] = []

    for item, repo in targets:
        item_id = str(item.get("item_id", "")).strip()
        if not started:
            probe = item_id.upper()
            if probe == resume_token or probe.startswith(resume_token):
                started = True
            else:
                continue
        if max_targets > 0 and processed >= max_targets:
            break
        processed += 1
        _progress(f"[execute] target {processed}/{len(targets)} repo={repo} item={item_id}")

        mode = policy.sandbox_mode_for(item=item, repo=repo)
        timeout_sec = policy.timeout_for_mode(mode)
        key = make_idempotency_key(
            item_id=item_id,
            repo=repo,
            batch_id=batch_id.strip(),
            stage=WorkStage.EXECUTING.value,
        )
        defaults = repo_defaults_for(repo)
        issue_url = _issue_url(item, repo)
        issue_ref = _spawn_issue_ref(item, repo, item_id)
        command = [ao_command, "spawn", repo, issue_ref]
        command_text = " ".join(command)

        if has_idempotency_key(state, key) and not force:
            skipped += 1
            actions.append(
                {
                    "action": "skip_idempotent",
                    "item_id": item_id,
                    "repo": repo,
                    "title": item.get("title", ""),
                    "url": issue_url,
                    "note": f"sandbox={mode}; key={key}",
                }
            )
            continue

        if not apply_changes:
            planned += 1
            actions.append(
                {
                    "action": "would_spawn",
                    "item_id": item_id,
                    "repo": repo,
                    "title": item.get("title", ""),
                    "url": issue_url,
                    "note": f"sandbox={mode}; timeout={timeout_sec}; cmd={command_text}",
                }
            )
            continue

        repo_path = core_dir.parent / repo
        if not repo_path.is_dir():
            failed += 1
            err = f"missing repo path: {repo_path}"
            actions.append(
                {
                    "action": "spawn_failed",
                    "item_id": item_id,
                    "repo": repo,
                    "title": item.get("title", ""),
                    "url": issue_url,
                    "note": err,
                }
            )
            item.setdefault("errors", []).append(err)
            item["last_error"] = err
            item["updated_at"] = now_iso()
            continue

        payload = {
            "item_id": item_id,
            "repo": repo,
            "title": str(item.get("title", "")),
            "issue_url": issue_url,
            "issue_ref": issue_ref,
            "acceptance_criteria": [str(x) for x in item.get("tasks", []) if str(x).strip()],
            "validation_commands": list(defaults.validation_commands),
            "batch_id": str(batch_id or "").strip(),
            "sandbox_mode": mode,
            "source_file": str(item.get("source_file", "")),
            "source_line": int(item.get("source_line", 0) or 0),
        }
        payload_file = meta_dir / f"{item_id.lower()}-{repo}.json"
        payload_file.parent.mkdir(parents=True, exist_ok=True)
        payload_file.write_text(json.dumps(payload, indent=2) + "\n")

        env = dict(os.environ)
        env["TV7_EXEC_CONTEXT"] = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        env["TV7_EXEC_SANDBOX_MODE"] = mode
        env["TV7_EXEC_CONTEXT_FILE"] = str(payload_file)
        env["TV7_BATCH_ID"] = str(batch_id or "").strip()

        started_at = now_iso()
        try:
            result = subprocess.run(
                command,
                cwd=repo_path,
                text=True,
                capture_output=True,
                env=env,
                check=False,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            result = subprocess.CompletedProcess(
                args=command,
                returncode=124,
                stdout=str(exc.stdout or ""),
                stderr=f"timeout after {timeout_sec}s",
            )
        ended_at = now_iso()

        log_file = logs_dir / f"{item_id.lower()}-{repo}.log"
        _write_log(
            log_file=log_file,
            cmd=command,
            env_keys={
                "TV7_EXEC_SANDBOX_MODE": mode,
                "TV7_EXEC_CONTEXT_FILE": str(payload_file),
                "TV7_BATCH_ID": str(batch_id or "").strip(),
            },
            result=result,
        )

        branch = _safe_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
        commit = _safe_git(repo_path, ["rev-parse", "HEAD"])
        out_text = f"{result.stdout}\n{result.stderr}"
        pr_match = RE_PR_URL.search(out_text)
        pr_url = pr_match.group(0) if pr_match else ""
        session_id = _extract_session_id(result.stdout)
        spawn_healthy = True
        spawn_health = "unknown"
        spawn_bootstrap = "not_attempted"
        if result.returncode == 0:
            spawn_healthy, spawn_health = _spawn_health_check(
                ao_command=ao_command,
                repo_path=repo_path,
                project=repo,
                session_id=session_id,
            )
            if session_id and (not spawn_healthy or str(spawn_health).startswith("spawning/")):
                spawn_bootstrap = _attempt_codex_workspace_trust_unblock(
                    ao_command=ao_command,
                    repo_path=repo_path,
                    project=repo,
                    session_id=session_id,
                    health_hint=spawn_health,
                )
                if spawn_bootstrap in {
                    "codex_workspace_trust_approved",
                    "codex_workspace_trust_approved_tmux",
                    "blind_approve_sent",
                    "blind_tmux_keys_sent",
                }:
                    spawn_healthy, spawn_health = _spawn_health_check(
                        ao_command=ao_command,
                        repo_path=repo_path,
                        project=repo,
                        session_id=session_id,
                    )
            else:
                spawn_bootstrap = "not_needed"

        status = "SUCCESS" if (result.returncode == 0 and spawn_healthy) else "FAILED"
        if result.returncode == 0 and spawn_healthy:
            spawned += 1
            transition_item_stage(item, WorkStage.EXECUTING.value, reason="execute handoff spawned")
            if pr_url:
                linked_prs = item.setdefault("linked_prs", {})
                if isinstance(linked_prs, dict):
                    linked_prs[repo] = pr_url
            record_idempotency_key(
                state,
                key=key,
                item_id=item_id,
                repo=repo,
                batch_id=str(batch_id or "").strip(),
                stage=WorkStage.EXECUTING.value,
                action="spawned",
                note=f"sandbox={mode}",
            )
            action = "spawned"
            note = (
                f"sandbox={mode}; branch={branch}; commit={commit[:12]}; "
                f"session={session_id or 'unknown'}; health={spawn_health}; "
                f"bootstrap={spawn_bootstrap}"
            )
        else:
            failed += 1
            err = (
                f"ao spawn failed rc={result.returncode}; "
                f"session={session_id or 'unknown'}; health={spawn_health}; "
                f"bootstrap={spawn_bootstrap}"
            )
            item.setdefault("errors", []).append(err)
            item["last_error"] = err
            action = "spawn_failed"
            note = (
                f"sandbox={mode}; rc={result.returncode}; "
                f"session={session_id or 'unknown'}; health={spawn_health}; "
                f"bootstrap={spawn_bootstrap}"
            )

        item["last_iteration_id"] = stamp
        item["updated_at"] = now_iso()

        state.setdefault("agent_runs", []).append(
            {
                "iteration_id": stamp,
                "run_type": "execute_handoff",
                "item_id": item_id,
                "repo": repo,
                "issue_url": issue_url,
                "sandbox_mode": mode,
                "sandbox_required": mode != "none",
                "command": command_text,
                "payload_file": str(payload_file),
                "log_file": str(log_file),
                "status": status,
                "exit_status": int(result.returncode),
                "branch": branch,
                "commit": commit,
                "pr_url": pr_url,
                "session_id": session_id,
                "spawn_health": spawn_health,
                "spawn_bootstrap": spawn_bootstrap,
                "started_at": started_at,
                "ended_at": ended_at,
                "ts": now_iso(),
            }
        )
        append_event(
            core_dir,
            "execute.spawn",
            f"Execution handoff {action} for {repo} and item {item_id}",
            {
                "item_id": item_id,
                "repo": repo,
                "action": action,
                "status": status,
                "sandbox_mode": mode,
                "exit_status": int(result.returncode),
                "log_file": str(log_file),
                "payload_file": str(payload_file),
                "pr_url": pr_url,
                "session_id": session_id,
                "spawn_health": spawn_health,
                "spawn_bootstrap": spawn_bootstrap,
            },
        )
        actions.append(
            {
                "action": action,
                "item_id": item_id,
                "repo": repo,
                "title": item.get("title", ""),
                "url": pr_url or issue_url,
                "note": note,
            }
        )

    return spawned, failed, skipped, planned, len(filtered_items), len(targets), actions
