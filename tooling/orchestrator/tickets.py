from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
import re
from typing import Callable
from typing import Any

from .idempotency import has_idempotency_key, make_idempotency_key, record_idempotency_key
from .lifecycle import WorkStage, transition_item_stage
from .models import now_iso
from .repo_defaults import RepoDefaults, repo_defaults_for
from .state_store import paths
from .state_store import append_event

ASSESSMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "validation_commands": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "acceptance_criteria", "validation_commands", "risks", "notes"],
}

EXPANSION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "context": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "validation_commands": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "out_of_scope": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["context", "acceptance_criteria", "validation_commands", "risks", "out_of_scope"],
}

BASE_ISSUE_LABELS = ["conductor", "type:task", "source:roadmap"]

LABEL_COLORS: dict[str, str] = {
    "conductor": "1D76DB",
    "type:task": "0E8A16",
    "source:roadmap": "5319E7",
    "stage:seed": "FBCA04",
}

_LABEL_READY_BY_REPO: dict[str, set[str]] = {}


def _gh_json(args: list[str]) -> Any:
    result = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip() or "unknown gh error"
        raise RuntimeError(f"gh {' '.join(args)} failed: {stderr}")
    return json.loads(result.stdout)


def _gh_text(args: list[str]) -> str:
    result = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip() or "unknown gh error"
        raise RuntimeError(f"gh {' '.join(args)} failed: {stderr}")
    return result.stdout.strip()


def _issue_labels(
    repo: str,
    batch_id: str,
    stage: str = "seed",
    *,
    extra_labels: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    labels = [*BASE_ISSUE_LABELS, f"repo:{repo}", f"stage:{stage}"]
    if batch_id:
        labels.append(f"batch:{batch_id}")
    for label in extra_labels or []:
        labels.append(str(label))
    out: list[str] = []
    seen: set[str] = set()
    for label in labels:
        normalized = str(label).strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _ensure_labels(repo_slug: str, labels: list[str]) -> None:
    ensured = _LABEL_READY_BY_REPO.setdefault(repo_slug, set())
    for label in labels:
        if label in ensured:
            continue
        color = LABEL_COLORS.get(label, "BFD4F2")
        # Keep label ensure best-effort; ticket creation should not fail on label create quirks.
        subprocess.run(
            [
                "gh",
                "label",
                "create",
                label,
                "--repo",
                repo_slug,
                "--color",
                color,
                "--description",
                "Managed by Project Conductor",
                "--force",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        ensured.add(label)


def _apply_issue_labels(repo_slug: str, issue_ref: str, labels: list[str]) -> None:
    if not labels:
        return
    _ensure_labels(repo_slug, labels)
    args = ["gh", "issue", "edit", issue_ref, "--repo", repo_slug]
    for label in labels:
        args.extend(["--add-label", label])
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip() or "unknown gh label edit error"
        raise RuntimeError(f"gh issue edit labels failed: {stderr}")


def _issue_title(item: dict[str, Any]) -> str:
    return f"[{item['item_id']}] {item['title']}"


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _truncate_text(value: Any, max_chars: int) -> str:
    text = _coerce_text(value)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars < 40:
        return text[:max_chars]
    head = max_chars // 2
    tail = max_chars - head
    return f"{text[:head]}\n...[truncated]...\n{text[-tail:]}"


def _assessment_prompt(item: dict[str, Any], repo: str) -> str:
    tasks = "\n".join(f"- {t}" for t in item.get("tasks", []))
    return (
        f"You are assessing a work item before opening a GitHub issue in {repo}.\n"
        "Generate concise implementation guidance for this repo only.\n\n"
        f"Work item id: {item.get('item_id','')}\n"
        f"Title: {item.get('title','')}\n"
        f"Source: {item.get('source_file','')}:{item.get('source_line',0)}\n\n"
        "Task list:\n"
        f"{tasks}\n\n"
        "Return JSON matching schema fields only. Keep each list item short."
    )


def _sanitize_assessment(payload: dict[str, Any]) -> dict[str, Any]:
    def _list(name: str) -> list[str]:
        raw = payload.get(name, [])
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if str(x).strip()]

    return {
        "summary": str(payload.get("summary", "")).strip(),
        "acceptance_criteria": _list("acceptance_criteria"),
        "validation_commands": _list("validation_commands"),
        "risks": _list("risks"),
        "notes": _list("notes"),
    }


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-").lower()[:120]


def _emit(progress_cb: Callable[[str], None] | None, msg: str) -> None:
    if progress_cb is not None:
        progress_cb(msg)


def _extract_json_object_from_text(text: str) -> dict[str, Any]:
    candidate = str(text or "").strip()
    if not candidate:
        raise ValueError("empty JSON response")

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        parsed = json.loads(fenced.group(1))
        if isinstance(parsed, dict):
            return parsed

    first = candidate.find("{")
    last = candidate.rfind("}")
    if first >= 0 and last > first:
        parsed = json.loads(candidate[first : last + 1])
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("response does not contain a valid JSON object")


def _run_gemini_json_prompt(
    *,
    core_dir: Path,
    repo: str,
    prompt: str,
    model: str,
    timeout_sec: int,
    log_max_chars: int,
    report_subdir: str,
    log_suffix: str,
    progress_cb: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], str, float]:
    if shutil.which("gemini") is None:
        raise RuntimeError("Missing required command: gemini")

    repo_path = core_dir.parent / repo
    if not repo_path.exists():
        raise RuntimeError(f"Repo path not found for gemini run: {repo_path}")

    cmd = ["gemini", "--prompt", prompt, "--output-format", "json", "--approval-mode", "yolo"]
    if model:
        cmd.extend(["--model", model])

    reports_dir = paths(core_dir)["root"] / "reports" / report_subdir
    reports_dir.mkdir(parents=True, exist_ok=True)
    log_path = reports_dir / f"{_slug(repo)}-{_slug(log_suffix)}.log"

    _emit(progress_cb, f"[gemini] start repo={repo} task={log_suffix} timeout={timeout_sec}s")
    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_sec,
            cwd=repo_path,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        log_payload = {
            "command": cmd,
            "status": "timeout",
            "timeout_sec": timeout_sec,
            "elapsed_sec": round(elapsed, 1),
            "stdout_excerpt": _truncate_text(exc.stdout, log_max_chars),
            "stderr_excerpt": _truncate_text(exc.stderr, log_max_chars),
        }
        log_path.write_text(json.dumps(log_payload, indent=2) + "\n")
        raise RuntimeError(
            f"gemini run timed out for {repo} after {timeout_sec}s "
            f"(log: {log_path})"
        ) from exc

    elapsed = time.monotonic() - started
    log_payload = {
        "command": cmd,
        "status": "ok" if result.returncode == 0 else "error",
        "exit_code": result.returncode,
        "elapsed_sec": round(elapsed, 1),
        "stdout_excerpt": _truncate_text(result.stdout, log_max_chars),
        "stderr_excerpt": _truncate_text(result.stderr, log_max_chars),
    }
    log_path.write_text(json.dumps(log_payload, indent=2) + "\n")
    if result.returncode != 0:
        stderr = result.stderr.strip() or "gemini execution failed"
        raise RuntimeError(f"gemini run failed for {repo}: {stderr} (log: {log_path})")

    response_envelope = _extract_json_object_from_text(result.stdout)
    response_text = str(response_envelope.get("response", "")).strip()
    if not response_text:
        raise RuntimeError(f"gemini run returned empty response for {repo} (log: {log_path})")

    payload = _extract_json_object_from_text(response_text)
    _emit(progress_cb, f"[gemini] done repo={repo} task={log_suffix} elapsed={elapsed:.1f}s")
    return payload, str(log_path), elapsed


def _assess_with_gemini(
    *,
    core_dir: Path,
    repo: str,
    item: dict[str, Any],
    model: str = "",
    timeout_sec: int = 300,
    log_max_chars: int = 4000,
    progress_cb: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], str, float]:
    prompt = (
        _assessment_prompt(item, repo)
        + "\n\nReturn only a JSON object with keys: summary, acceptance_criteria, validation_commands, risks, notes."
    )
    raw, log_path, elapsed = _run_gemini_json_prompt(
        core_dir=core_dir,
        repo=repo,
        prompt=prompt,
        model=model,
        timeout_sec=timeout_sec,
        log_max_chars=log_max_chars,
        report_subdir="assessments",
        log_suffix=f"{item.get('item_id','')}-gemini-assess",
        progress_cb=progress_cb,
    )
    return _sanitize_assessment(raw), log_path, elapsed


def _expand_with_gemini(
    *,
    core_dir: Path,
    repo: str,
    item: dict[str, Any],
    defaults: RepoDefaults,
    model: str = "",
    timeout_sec: int = 300,
    log_max_chars: int = 4000,
    progress_cb: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], str, float]:
    prompt = (
        _expansion_prompt(item, repo, defaults)
        + "\n\nReturn only a JSON object with keys: context, acceptance_criteria, validation_commands, risks, out_of_scope."
    )
    return _run_gemini_json_prompt(
        core_dir=core_dir,
        repo=repo,
        prompt=prompt,
        model=model,
        timeout_sec=timeout_sec,
        log_max_chars=log_max_chars,
        report_subdir="expansions",
        log_suffix=f"{item.get('item_id','')}-gemini-expand",
        progress_cb=progress_cb,
    )


def _resolve_assess_backend(*, assess_agent: str, model: str = "") -> str:
    requested = str(assess_agent or "").strip().lower()
    model_hint = str(model or "").strip().lower()
    preferred = str(os.environ.get("TV7_ASSESS_PROVIDER", "")).strip().lower()

    def _available(name: str) -> bool:
        return shutil.which(name) is not None

    if requested in {"none"}:
        return "none"

    if requested in {"", "auto"}:
        if preferred == "gemini" and _available("gemini"):
            return "gemini_queue"
        if preferred == "codex" and _available("codex"):
            return "codex"
        # Default local automation path: codex first.
        if _available("codex"):
            return "codex"
        if _available("gemini"):
            return "gemini_queue"
        return "queue"

    if requested == "codex":
        return "codex"

    if requested == "gemini":
        if _available("gemini"):
            return "gemini_queue"
        # If gemini is unavailable but codex exists, keep workflow moving.
        if _available("codex"):
            return "codex"
        return "queue"

    if requested in {"antigravity", "queue"}:
        if "gemini" in model_hint and _available("gemini"):
            return "gemini_queue"
        return "queue"

    raise RuntimeError(f"Unsupported assess_agent: {assess_agent}")


def _resolve_expand_backend(*, expand_agent: str, model: str = "") -> str:
    requested = str(expand_agent or "").strip().lower()
    model_hint = str(model or "").strip().lower()
    preferred = str(os.environ.get("TV7_EXPAND_PROVIDER", "")).strip().lower()

    def _available(name: str) -> bool:
        return shutil.which(name) is not None

    if requested in {"none"}:
        return "none"

    if requested in {"", "auto"}:
        if preferred == "gemini" and _available("gemini"):
            return "gemini_queue"
        if preferred == "codex" and _available("codex"):
            return "codex"
        # Default local automation path: codex first.
        if _available("codex"):
            return "codex"
        if _available("gemini"):
            return "gemini_queue"
        return "queue"

    if requested == "codex":
        return "codex"

    if requested == "gemini":
        if _available("gemini"):
            return "gemini_queue"
        # If gemini is unavailable but codex exists, keep workflow moving.
        if _available("codex"):
            return "codex"
        return "queue"

    if requested in {"antigravity", "queue"}:
        if "gemini" in model_hint and _available("gemini"):
            return "gemini_queue"
        return "queue"

    raise RuntimeError(f"Unsupported expand_agent: {expand_agent}")


def _assess_with_codex(
    *,
    core_dir: Path,
    repo: str,
    item: dict[str, Any],
    model: str = "",
    reasoning: str = "",
    personality: str = "",
    timeout_sec: int = 300,
    log_max_chars: int = 4000,
    progress_cb: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], str, float]:
    if shutil.which("codex") is None:
        raise RuntimeError("Missing required command: codex")

    repo_path = core_dir.parent / repo
    if not repo_path.exists():
        raise RuntimeError(f"Repo path not found for assessment: {repo_path}")

    prompt = _assessment_prompt(item, repo)
    with tempfile.TemporaryDirectory(prefix="tv7-orch-assess-") as td:
        td_path = Path(td)
        schema_file = td_path / "schema.json"
        out_file = td_path / "assessment.json"
        schema_file.write_text(json.dumps(ASSESSMENT_SCHEMA))

        cmd = [
            "codex",
            "exec",
            "--full-auto",
            "-C",
            str(repo_path),
            "--output-schema",
            str(schema_file),
            "--output-last-message",
            str(out_file),
        ]
        if model:
            cmd.extend(["-m", model])
        if reasoning:
            cmd.extend(["-c", f'model_reasoning_effort="{reasoning}"'])
        if personality:
            cmd.extend(["-c", f'personality="{personality}"'])
        cmd.append("-")

        reports_dir = paths(core_dir)["root"] / "reports" / "assessments"
        reports_dir.mkdir(parents=True, exist_ok=True)
        log_path = reports_dir / f"{_slug(repo)}-{_slug(item.get('item_id',''))}.log"

        _emit(progress_cb, f"[assess] start repo={repo} item={item.get('item_id','')} timeout={timeout_sec}s")
        started = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                text=True,
                input=prompt,
                capture_output=True,
                check=False,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            log_payload = {
                "command": cmd,
                "status": "timeout",
                "timeout_sec": timeout_sec,
                "elapsed_sec": round(elapsed, 1),
                "stdout_excerpt": _truncate_text(exc.stdout, log_max_chars),
                "stderr_excerpt": _truncate_text(exc.stderr, log_max_chars),
            }
            log_path.write_text(json.dumps(log_payload, indent=2) + "\n")
            raise RuntimeError(
                f"codex assess timed out for {repo} after {timeout_sec}s "
                f"(log: {log_path})"
            ) from exc
        elapsed = time.monotonic() - started
        log_payload = {
            "command": cmd,
            "status": "ok" if result.returncode == 0 else "error",
            "exit_code": result.returncode,
            "elapsed_sec": round(elapsed, 1),
            "stdout_excerpt": _truncate_text(result.stdout, log_max_chars),
            "stderr_excerpt": _truncate_text(result.stderr, log_max_chars),
        }
        log_path.write_text(json.dumps(log_payload, indent=2) + "\n")
        if result.returncode != 0:
            stderr = result.stderr.strip() or "codex assessment failed"
            raise RuntimeError(f"codex assess failed for {repo}: {stderr} (log: {log_path})")
        if not out_file.exists():
            raise RuntimeError(f"codex assess did not produce output file for {repo} (log: {log_path})")
        raw = json.loads(out_file.read_text())
        if not isinstance(raw, dict):
            raise RuntimeError(f"codex assess returned invalid JSON object for {repo}")
        _emit(progress_cb, f"[assess] done repo={repo} item={item.get('item_id','')} elapsed={elapsed:.1f}s")
        return _sanitize_assessment(raw), str(log_path), elapsed


def _assess_with_cli_queue(
    *,
    core_dir: Path,
    repo: str,
    item: dict[str, Any],
    provider: str,
    profile: str,
    model: str,
    reasoning: str,
    personality: str,
) -> tuple[dict[str, Any], str, float]:
    reports_dir = paths(core_dir)["root"] / "reports" / "assessments"
    reports_dir.mkdir(parents=True, exist_ok=True)
    pending_path = reports_dir / f"{_slug(repo)}-{_slug(item.get('item_id',''))}.pending.json"
    prompt = _assessment_prompt(item, repo)
    provider_name = str(provider or "local-cli").strip()
    payload = {
        "provider": provider_name,
        "profile": profile,
        "model": model,
        "reasoning": reasoning,
        "personality": personality,
        "repo": repo,
        "item_id": item.get("item_id", ""),
        "title": item.get("title", ""),
        "source": f"{item.get('source_file','')}:{item.get('source_line',0)}",
        "schema": ASSESSMENT_SCHEMA,
        "prompt": prompt,
        "instructions": [
            f"Run assessment with local CLI provider `{provider_name}` in the target repo.",
            "Use Gemini CLI (`gemini`) or Codex CLI (`codex`) whichever is available.",
            "Return JSON matching the schema exactly.",
            "Copy resulting JSON back into this file under key: result_json.",
        ],
        "result_json": {},
    }
    pending_path.write_text(json.dumps(payload, indent=2) + "\n")
    assessment = {
        "summary": (
            f"Assessment queued for local CLI provider `{provider_name}`. "
            f"Complete pending file: {pending_path}"
        ),
        "acceptance_criteria": [],
        "validation_commands": [],
        "risks": [],
        "notes": [],
    }
    return assessment, str(pending_path), 0.0


def _seed_tasks(item: dict[str, Any], *, max_items: int = 6) -> list[str]:
    raw_tasks = [str(t).strip() for t in item.get("tasks", []) if str(t).strip()]
    if not raw_tasks:
        title = str(item.get("title", "")).strip()
        return [title] if title else []
    filtered: list[str] = []
    seen: set[str] = set()
    for task in raw_tasks:
        # Keep seed body compact and avoid roadmap metadata lines.
        if task.startswith("**"):
            continue
        key = task.lower()
        if key in seen:
            continue
        seen.add(key)
        filtered.append(task)
        if len(filtered) >= max_items:
            break
    return filtered or [raw_tasks[0]]


def _issue_body(
    item: dict[str, Any],
    *,
    repo: str,
    repo_defaults: RepoDefaults | None = None,
    batch_id: str = "",
) -> str:
    defaults = repo_defaults or repo_defaults_for(repo)
    lines = [
        "## Orchestrator Work Item",
        "",
        f"- Item ID: `{item['item_id']}`",
        f"- Source: `{item.get('source_file', '')}:{item.get('source_line', 0)}`",
    ]
    if batch_id:
        lines.append(f"- Batch ID: `{batch_id}`")
    if defaults.owner_hints:
        lines.append(f"- Owner Hints: `{', '.join(defaults.owner_hints)}`")

    lines.extend(["", "## Tasks"])
    for t in _seed_tasks(item):
        lines.append(f"- {t}")

    validations = list(defaults.validation_commands)
    if validations:
        lines.extend(["", "## Validation Commands"])
        for cmd in validations:
            lines.append(f"- `{cmd}`")

    return "\n".join(lines)


def _extract_log_path(error_msg: str) -> str:
    match = re.search(r"\(log:\s*([^)]+)\)", error_msg)
    if not match:
        return ""
    return str(match.group(1)).strip()


def _expansion_prompt(item: dict[str, Any], repo: str, defaults: RepoDefaults) -> str:
    tasks = "\n".join(f"- {t}" for t in _seed_tasks(item))
    validation_commands = "\n".join(f"- `{cmd}`" for cmd in defaults.validation_commands)
    return (
        f"You are expanding a seeded GitHub issue for {repo}.\n"
        "Generate repository-scoped implementation guidance and return structured JSON only.\n\n"
        f"Work item id: {item.get('item_id','')}\n"
        f"Title: {item.get('title','')}\n"
        f"Source: {item.get('source_file','')}:{item.get('source_line',0)}\n\n"
        "Seeded tasks:\n"
        f"{tasks}\n\n"
        "Repository default validation commands:\n"
        f"{validation_commands}\n\n"
        "Rules:\n"
        "- Keep scope strictly inside this repository.\n"
        "- Return JSON with keys: context, acceptance_criteria, validation_commands, risks, out_of_scope.\n"
        "- Keep entries concise and actionable.\n"
    )


def _expand_with_codex(
    *,
    core_dir: Path,
    repo: str,
    item: dict[str, Any],
    defaults: RepoDefaults,
    model: str = "",
    reasoning: str = "",
    personality: str = "",
    timeout_sec: int = 300,
    log_max_chars: int = 4000,
    progress_cb: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], str, float]:
    if shutil.which("codex") is None:
        raise RuntimeError("Missing required command: codex")

    repo_path = core_dir.parent / repo
    if not repo_path.exists():
        raise RuntimeError(f"Repo path not found for expansion: {repo_path}")

    prompt = _expansion_prompt(item, repo, defaults)
    with tempfile.TemporaryDirectory(prefix="tv7-orch-expand-") as td:
        td_path = Path(td)
        schema_file = td_path / "schema.json"
        out_file = td_path / "expansion.json"
        schema_file.write_text(json.dumps(EXPANSION_SCHEMA))

        cmd = [
            "codex",
            "exec",
            "--full-auto",
            "-C",
            str(repo_path),
            "--output-schema",
            str(schema_file),
            "--output-last-message",
            str(out_file),
        ]
        if model:
            cmd.extend(["-m", model])
        if reasoning:
            cmd.extend(["-c", f'model_reasoning_effort="{reasoning}"'])
        if personality:
            cmd.extend(["-c", f'personality="{personality}"'])
        cmd.append("-")

        reports_dir = paths(core_dir)["root"] / "reports" / "expansions"
        reports_dir.mkdir(parents=True, exist_ok=True)
        log_path = reports_dir / f"{_slug(repo)}-{_slug(item.get('item_id',''))}.log"

        _emit(progress_cb, f"[expand-agent] start repo={repo} item={item.get('item_id','')} timeout={timeout_sec}s")
        started = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                text=True,
                input=prompt,
                capture_output=True,
                check=False,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            log_payload = {
                "command": cmd,
                "status": "timeout",
                "timeout_sec": timeout_sec,
                "elapsed_sec": round(elapsed, 1),
                "stdout_excerpt": _truncate_text(exc.stdout, log_max_chars),
                "stderr_excerpt": _truncate_text(exc.stderr, log_max_chars),
            }
            log_path.write_text(json.dumps(log_payload, indent=2) + "\n")
            raise RuntimeError(
                f"codex expand timed out for {repo} after {timeout_sec}s "
                f"(log: {log_path})"
            ) from exc
        elapsed = time.monotonic() - started
        log_payload = {
            "command": cmd,
            "status": "ok" if result.returncode == 0 else "error",
            "exit_code": result.returncode,
            "elapsed_sec": round(elapsed, 1),
            "stdout_excerpt": _truncate_text(result.stdout, log_max_chars),
            "stderr_excerpt": _truncate_text(result.stderr, log_max_chars),
        }
        log_path.write_text(json.dumps(log_payload, indent=2) + "\n")
        if result.returncode != 0:
            stderr = result.stderr.strip() or "codex expansion failed"
            raise RuntimeError(f"codex expand failed for {repo}: {stderr} (log: {log_path})")
        if not out_file.exists():
            raise RuntimeError(f"codex expand did not produce output file for {repo} (log: {log_path})")
        raw = json.loads(out_file.read_text())
        if not isinstance(raw, dict):
            raise RuntimeError(f"codex expand returned invalid JSON object for {repo}")
        _emit(progress_cb, f"[expand-agent] done repo={repo} item={item.get('item_id','')} elapsed={elapsed:.1f}s")
        return raw, str(log_path), elapsed


def _expand_with_cli_queue(
    *,
    core_dir: Path,
    repo: str,
    item: dict[str, Any],
    defaults: RepoDefaults,
    provider: str,
    model: str,
    reasoning: str,
    personality: str,
) -> tuple[str, float]:
    reports_dir = paths(core_dir)["root"] / "reports" / "expansions"
    reports_dir.mkdir(parents=True, exist_ok=True)
    pending_path = reports_dir / f"{_slug(repo)}-{_slug(item.get('item_id',''))}.pending.json"
    provider_name = str(provider or "local-cli").strip()
    payload = {
        "provider": provider_name,
        "model": model,
        "reasoning": reasoning,
        "personality": personality,
        "repo": repo,
        "item_id": item.get("item_id", ""),
        "title": item.get("title", ""),
        "source": f"{item.get('source_file','')}:{item.get('source_line',0)}",
        "schema": EXPANSION_SCHEMA,
        "prompt": _expansion_prompt(item, repo, defaults),
        "instructions": [
            f"Run expansion with local CLI provider `{provider_name}` in the target repo.",
            "Use Gemini CLI (`gemini`) or Codex CLI (`codex`) whichever is available.",
            "Return JSON matching the schema exactly.",
            "Copy resulting JSON back into this file under key: result_json.",
        ],
        "result_json": {},
    }
    pending_path.write_text(json.dumps(payload, indent=2) + "\n")
    return str(pending_path), 0.0


_EXPANSION_BEGIN = "<!-- CONDUCTOR-EXPANSION:BEGIN -->"
_EXPANSION_END = "<!-- CONDUCTOR-EXPANSION:END -->"
_EXPANSION_SECTION_HEADERS = [
    "## Expansion Context",
    "## Acceptance Criteria",
    "## Validation Commands",
    "## Risks",
    "## Out Of Scope",
]
_LOW_QUALITY_PATTERNS = [
    re.compile(r"\b(i(?:'m| am)|we)\s+(will|going to|plan to|now)\b", re.IGNORECASE),
    re.compile(r"\b(?:inspect|review|check|look at)\b", re.IGNORECASE),
    re.compile(r"\bloaded the roadmap context\b", re.IGNORECASE),
]


def _expansion_lists(payload: dict[str, Any], key: str) -> list[str]:
    raw = payload.get(key, [])
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        low = text.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(text)
    return out


def _normalize_expansion_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "context": str(payload.get("context", "")).strip(),
        "acceptance_criteria": _expansion_lists(payload, "acceptance_criteria"),
        "validation_commands": _expansion_lists(payload, "validation_commands"),
        "risks": _expansion_lists(payload, "risks"),
        "out_of_scope": _expansion_lists(payload, "out_of_scope"),
    }


def _validate_expansion_payload(payload: dict[str, Any]) -> None:
    required = set(EXPANSION_SCHEMA["required"])
    keys = set(payload.keys())
    if keys != required:
        raise ValueError(
            f"expansion payload keys mismatch; expected {sorted(required)} got {sorted(keys)}"
        )
    context = str(payload.get("context", "")).strip()
    if not context:
        raise ValueError("expansion payload `context` cannot be empty")
    for name in ("acceptance_criteria", "validation_commands", "risks", "out_of_scope"):
        if not isinstance(payload.get(name), list):
            raise ValueError(f"expansion payload `{name}` must be a list")


def _default_expansion_payload(item: dict[str, Any], repo: str, defaults: RepoDefaults) -> dict[str, Any]:
    title = str(item.get("title", "")).strip()
    source = f"{item.get('source_file', '')}:{item.get('source_line', 0)}"
    return {
        "context": (
            f"Expanded guidance for `{repo}` from roadmap item `{item.get('item_id', '')}`. "
            f"Source: `{source}`. Title: {title}."
        ),
        "acceptance_criteria": [
            "Implement the scoped task changes for this repository only.",
            "Keep behavior deterministic and aligned with roadmap constraints.",
            "Update linked issue/PR context with the final implementation summary.",
        ],
        "validation_commands": list(defaults.validation_commands),
        "risks": [
            "Scope creep across repositories if repo boundaries are not enforced.",
            "Behavior drift if validation commands are skipped.",
        ],
        "out_of_scope": [
            "Cross-repo refactors outside explicit task scope.",
            "Unrelated architecture changes not required by this item.",
        ],
    }


def _prepare_expansion_payload(
    *,
    item: dict[str, Any],
    repo: str,
    defaults: RepoDefaults,
    raw_payload: dict[str, Any] | None = None,
    strict_expand: bool = False,
) -> tuple[dict[str, Any], str]:
    if raw_payload is None:
        payload = _default_expansion_payload(item, repo, defaults)
        _validate_expansion_payload(payload)
        return payload, "default"

    normalized = _normalize_expansion_payload(raw_payload)
    try:
        _validate_expansion_payload(normalized)
        return normalized, "provided"
    except Exception:
        if strict_expand:
            raise
        payload = _default_expansion_payload(item, repo, defaults)
        _validate_expansion_payload(payload)
        return payload, "fallback"


def _expansion_block(payload: dict[str, Any]) -> str:
    lines = [
        _EXPANSION_BEGIN,
        "## Expansion Context",
        payload["context"],
        "",
        "## Acceptance Criteria",
    ]
    for item in payload["acceptance_criteria"]:
        lines.append(f"- {item}")
    lines.extend(["", "## Validation Commands"])
    for cmd in payload["validation_commands"]:
        lines.append(f"- `{cmd}`")
    lines.extend(["", "## Risks"])
    for risk in payload["risks"]:
        lines.append(f"- {risk}")
    lines.extend(["", "## Out Of Scope"])
    for entry in payload["out_of_scope"]:
        lines.append(f"- {entry}")
    lines.append(_EXPANSION_END)
    return "\n".join(lines)


def _upsert_expansion_block(body: str, payload: dict[str, Any]) -> tuple[str, bool]:
    before = str(body or "").rstrip()
    block = _expansion_block(payload)
    begin = before.find(_EXPANSION_BEGIN)
    end = before.find(_EXPANSION_END)
    if begin >= 0 and end >= begin:
        end = end + len(_EXPANSION_END)
        updated = (before[:begin].rstrip() + "\n\n" + block).strip()
    else:
        updated = (before + "\n\n" + block).strip() if before else block
    return updated + "\n", (updated + "\n") != (before + ("\n" if before else ""))


def _extract_expansion_section(text: str, start_header: str, end_header: str | None) -> str:
    start = text.find(start_header)
    if start < 0:
        return ""
    body_start = start + len(start_header)
    if end_header:
        end = text.find(end_header, body_start)
        if end < 0:
            return ""
        return text[body_start:end].strip()
    return text[body_start:].strip()


def _parse_expansion_payload_from_body(body: str) -> dict[str, Any] | None:
    text = str(body or "")
    begin = text.find(_EXPANSION_BEGIN)
    end = text.find(_EXPANSION_END)
    if begin < 0 or end < begin:
        return None
    block = text[begin + len(_EXPANSION_BEGIN) : end].strip()
    sections: dict[str, str] = {}
    for idx, header in enumerate(_EXPANSION_SECTION_HEADERS):
        next_header = _EXPANSION_SECTION_HEADERS[idx + 1] if idx + 1 < len(_EXPANSION_SECTION_HEADERS) else None
        section = _extract_expansion_section(block, header, next_header)
        if not section:
            return None
        sections[header] = section

    def _bullets(section_text: str, *, strip_backticks: bool = False) -> list[str]:
        out: list[str] = []
        for line in section_text.splitlines():
            token = line.strip()
            if not token.startswith("- "):
                continue
            item = token[2:].strip()
            if strip_backticks and len(item) >= 2 and item[0] == "`" and item[-1] == "`":
                item = item[1:-1].strip()
            if item:
                out.append(item)
        return out

    payload = _normalize_expansion_payload(
        {
            "context": sections["## Expansion Context"],
            "acceptance_criteria": _bullets(sections["## Acceptance Criteria"]),
            "validation_commands": _bullets(sections["## Validation Commands"], strip_backticks=True),
            "risks": _bullets(sections["## Risks"]),
            "out_of_scope": _bullets(sections["## Out Of Scope"]),
        }
    )
    try:
        _validate_expansion_payload(payload)
    except Exception:
        return None
    return payload


def _expansion_quality_score(payload: dict[str, Any]) -> int:
    context = str(payload.get("context", "")).strip()
    acceptance = _expansion_lists(payload, "acceptance_criteria")
    validation = _expansion_lists(payload, "validation_commands")
    risks = _expansion_lists(payload, "risks")
    out_of_scope = _expansion_lists(payload, "out_of_scope")
    detail = acceptance + risks + out_of_scope

    score = 0
    score += min(5, len(context) // 60)
    score += min(6, len(acceptance))
    score += min(4, len(validation))
    score += min(4, len(risks))
    score += min(3, len(out_of_scope))
    avg_detail_len = (sum(len(x) for x in detail) // len(detail)) if detail else 0
    if avg_detail_len >= 50:
        score += 2
    elif avg_detail_len >= 24:
        score += 1

    aggregate_text = " ".join([context, *detail])
    for pattern in _LOW_QUALITY_PATTERNS:
        if pattern.search(aggregate_text):
            score -= 2
    return score


def _is_low_quality_expansion(payload: dict[str, Any]) -> bool:
    context = str(payload.get("context", "")).strip()
    acceptance = _expansion_lists(payload, "acceptance_criteria")
    validation = _expansion_lists(payload, "validation_commands")
    risks = _expansion_lists(payload, "risks")
    out_of_scope = _expansion_lists(payload, "out_of_scope")

    if len(context) < 70:
        return True
    if len(acceptance) < 2 or len(validation) < 1 or len(risks) < 1 or len(out_of_scope) < 1:
        return True

    aggregate_text = " ".join([context, *acceptance, *risks, *out_of_scope])
    return any(pattern.search(aggregate_text) for pattern in _LOW_QUALITY_PATTERNS)


def _strip_legacy_issue_metadata(body: str) -> str:
    lines = str(body or "").splitlines()
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^- Stage:\s*`[^`]*`\s*$", stripped):
            continue
        if re.match(r"^- Assessment:\s*`[^`]*`\s*$", stripped):
            continue
        cleaned.append(line.rstrip())
    compact = "\n".join(cleaned)
    compact = re.sub(r"\n{3,}", "\n\n", compact)
    return compact.strip()


def _set_issue_stage_label(
    *,
    repo_slug: str,
    issue_ref: str,
    repo: str,
    batch_id: str,
    from_stage: str,
    to_stage: str,
    extra_labels: list[str] | tuple[str, ...] | None = None,
) -> None:
    _apply_issue_labels(
        repo_slug,
        issue_ref,
        _issue_labels(
            repo=repo,
            batch_id=batch_id,
            stage=to_stage,
            extra_labels=extra_labels,
        ),
    )
    if from_stage == to_stage:
        return
    args = [
        "gh",
        "issue",
        "edit",
        issue_ref,
        "--repo",
        repo_slug,
        "--remove-label",
        f"stage:{from_stage}",
    ]
    subprocess.run(args, text=True, capture_output=True, check=False)


def _split_values(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        for part in str(raw).split(","):
            token = part.strip()
            if token:
                out.append(token)
    return out


def expand_repo_issues(
    *,
    core_dir: Path,
    state: dict[str, Any],
    owner: str,
    apply_changes: bool,
    strict_expand: bool,
    batch_id: str,
    progress: bool,
    repo_filters: list[str] | None = None,
    roadmap_filters: list[str] | None = None,
    item_prefix_filters: list[str] | None = None,
    limit: int = 0,
    resume_from: str = "",
    dry_run_diff: bool = False,
    expand_agent: str = "auto",
    expand_model: str = "",
    expand_reasoning: str = "",
    expand_personality: str = "",
    expand_timeout_sec: int = 300,
    expand_log_max_chars: int = 4000,
) -> tuple[int, int, int, int, int, int, list[dict[str, Any]]]:
    if shutil.which("gh") is None:
        raise RuntimeError("Missing required command: gh")

    repo_filter_set = set(_split_values(repo_filters))
    roadmap_filter_values = _split_values(roadmap_filters)
    item_prefixes = [x.upper() for x in _split_values(item_prefix_filters)]
    roadmap_filter_names = {Path(x).name for x in roadmap_filter_values}
    roadmap_filter_paths = {str(Path(x).resolve()) for x in roadmap_filter_values if x}

    def _matches_item(item: dict[str, Any]) -> bool:
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

    resume_token = str(resume_from).strip().upper()
    started = not bool(resume_token)
    max_targets = max(0, int(limit))
    processed = 0

    expanded = 0
    unchanged = 0
    skipped = 0
    fallback = 0
    actions: list[dict[str, Any]] = []

    diff_dir: Path | None = None
    if dry_run_diff:
        stamp = now_iso().replace(":", "").replace("-", "").replace("+00:00", "z")
        diff_dir = paths(core_dir)["root"] / "reports" / f"expand-diff-{stamp}"
        diff_dir.mkdir(parents=True, exist_ok=True)

    def _progress(msg: str) -> None:
        if progress:
            print(msg, flush=True)

    for item, repo in targets:
        item_id = str(item.get("item_id", ""))
        if not started:
            probe = item_id.upper()
            if probe == resume_token or probe.startswith(resume_token):
                started = True
            else:
                continue
        if max_targets > 0 and processed >= max_targets:
            break
        processed += 1
        _progress(f"[expand] target {processed}/{len(targets)} repo={repo} item={item_id}")

        repo_slug = f"{owner}/{repo}"
        issues = item.setdefault("issues", {})
        issue_meta = issues.get(repo, {}) if isinstance(issues, dict) else {}
        issue_ref = ""
        if isinstance(issue_meta, dict):
            issue_ref = str(issue_meta.get("url", "") or issue_meta.get("number", "")).strip()
        if not issue_ref:
            skipped += 1
            actions.append(
                {
                    "action": "skip_missing_issue",
                    "item_id": item_id,
                    "repo": repo,
                    "title": item.get("title", ""),
                    "url": "",
                    "note": "no linked issue found in state",
                }
            )
            continue

        view = _gh_json(
            [
                "issue",
                "view",
                issue_ref,
                "--repo",
                repo_slug,
                "--json",
                "number,url,title,state,body",
            ]
        )
        raw_body_before = str(view.get("body", ""))
        body_before = _strip_legacy_issue_metadata(raw_body_before)
        defaults = repo_defaults_for(repo)
        raw_payload: dict[str, Any] | None = None
        resolved_expand_backend = "none"
        expand_log = ""
        if str(expand_agent or "").strip().lower() not in {"none"}:
            try:
                resolved_expand_backend = _resolve_expand_backend(
                    expand_agent=expand_agent,
                    model=expand_model.strip(),
                )
                if resolved_expand_backend == "codex":
                    raw_payload, expand_log, _ = _expand_with_codex(
                        core_dir=core_dir,
                        repo=repo,
                        item=item,
                        defaults=defaults,
                        model=expand_model.strip(),
                        reasoning=expand_reasoning.strip(),
                        personality=expand_personality.strip(),
                        timeout_sec=max(30, int(expand_timeout_sec)),
                        log_max_chars=max(500, int(expand_log_max_chars)),
                        progress_cb=_progress if progress else None,
                    )
                elif resolved_expand_backend == "gemini_queue":
                    try:
                        raw_payload, expand_log, _ = _expand_with_gemini(
                            core_dir=core_dir,
                            repo=repo,
                            item=item,
                            defaults=defaults,
                            model=expand_model.strip(),
                            timeout_sec=max(30, int(expand_timeout_sec)),
                            log_max_chars=max(500, int(expand_log_max_chars)),
                            progress_cb=_progress if progress else None,
                        )
                        resolved_expand_backend = "gemini"
                    except Exception as gemini_exc:
                        if strict_expand:
                            raise
                        queue_log, _ = _expand_with_cli_queue(
                            core_dir=core_dir,
                            repo=repo,
                            item=item,
                            defaults=defaults,
                            provider="gemini",
                            model=expand_model.strip(),
                            reasoning=expand_reasoning.strip(),
                            personality=expand_personality.strip(),
                        )
                        resolved_expand_backend = "gemini_queue_fallback"
                        failed_log = _extract_log_path(str(gemini_exc))
                        logs = [x for x in [failed_log, queue_log] if x]
                        expand_log = "; ".join(logs)
                elif resolved_expand_backend == "queue":
                    expand_log, _ = _expand_with_cli_queue(
                        core_dir=core_dir,
                        repo=repo,
                        item=item,
                        defaults=defaults,
                        provider="local-cli",
                        model=expand_model.strip(),
                        reasoning=expand_reasoning.strip(),
                        personality=expand_personality.strip(),
                    )
                elif resolved_expand_backend == "none":
                    pass
                else:
                    raise RuntimeError(f"Unsupported expand backend: {resolved_expand_backend}")
            except Exception as exc:
                if strict_expand:
                    raise
                resolved_expand_backend = "fallback_default"
                expand_log = _extract_log_path(str(exc))
        payload, payload_source = _prepare_expansion_payload(
            item=item,
            repo=repo,
            defaults=defaults,
            raw_payload=raw_payload,
            strict_expand=strict_expand,
        )
        if payload_source == "fallback":
            fallback += 1

        quality_guarded = False
        existing_payload = _parse_expansion_payload_from_body(raw_body_before)
        if existing_payload is not None:
            existing_score = _expansion_quality_score(existing_payload)
            new_score = _expansion_quality_score(payload)
            regression_margin = 2
            regressed = (new_score + regression_margin) < existing_score
            is_low_quality = _is_low_quality_expansion(payload)
            is_default_regression = payload_source in {"default", "fallback"} and regressed
            if regressed and (is_low_quality or is_default_regression):
                payload = existing_payload
                quality_guarded = True

        body_after, block_changed = _upsert_expansion_block(body_before, payload)
        cleanup_changed = raw_body_before.rstrip() != body_before.rstrip()
        changed = block_changed or cleanup_changed
        note_parts = [payload_source]
        if quality_guarded:
            note_parts.append("quality_guard=kept_existing")
        if resolved_expand_backend not in {"", "none"}:
            note_parts.append(f"backend={resolved_expand_backend}")
        if expand_log:
            note_parts.append(f"log={expand_log}")
        note = "; ".join(note_parts)
        url = str(view.get("url", issue_ref))

        if dry_run_diff and changed and diff_dir is not None:
            diff_lines = list(
                difflib.unified_diff(
                    raw_body_before.splitlines(),
                    body_after.splitlines(),
                    fromfile=f"{item_id}:{repo}:before",
                    tofile=f"{item_id}:{repo}:after",
                    lineterm="",
                )
            )
            diff_file = diff_dir / f"{item_id.lower()}-{repo}.diff"
            diff_file.write_text("\n".join(diff_lines) + "\n")
            note = f"{note}; diff={diff_file}"

        if not apply_changes:
            if quality_guarded and not changed:
                action = "skipped_low_quality_expand"
            else:
                action = "would_expand" if changed else "already_expanded"
            if changed:
                expanded += 1
            else:
                unchanged += 1
            actions.append(
                {
                    "action": action,
                    "item_id": item_id,
                    "repo": repo,
                    "title": item.get("title", ""),
                    "url": url,
                    "note": note,
                }
            )
            continue

        if changed:
            _gh_text(["issue", "edit", issue_ref, "--repo", repo_slug, "--body", body_after])
            expanded += 1
            action = "expanded"
        else:
            unchanged += 1
            action = "skipped_low_quality_expand" if quality_guarded else "already_expanded"

        _set_issue_stage_label(
            repo_slug=repo_slug,
            issue_ref=issue_ref,
            repo=repo,
            batch_id=batch_id.strip(),
            from_stage="seed",
            to_stage="expanded",
            extra_labels=list(defaults.extra_labels),
        )
        current_stage = str(item.get("stage", WorkStage.SEEDED.value)).strip().lower()
        if current_stage in {WorkStage.SEEDED.value, WorkStage.EXPANDED.value}:
            transition_item_stage(item, WorkStage.EXPANDED.value, reason="expand stage issue enrichment")
        else:
            note = f"{note}; stage_preserved={current_stage}"
        record_idempotency_key(
            state,
            key=make_idempotency_key(
                item_id=item_id,
                repo=repo,
                batch_id=batch_id.strip(),
                stage=WorkStage.EXPANDED.value,
            ),
            item_id=item_id,
            repo=repo,
            batch_id=batch_id.strip(),
            stage=WorkStage.EXPANDED.value,
            action=action,
            note=payload_source,
        )
        append_event(
            core_dir,
            "expand.updated",
            f"Expanded issue for {repo} and item {item_id}",
            {
                "repo": repo,
                "item_id": item_id,
                "url": url,
                "changed": changed,
                "payload_source": payload_source,
            },
        )
        actions.append(
            {
                "action": action,
                "item_id": item_id,
                "repo": repo,
                "title": item.get("title", ""),
                "url": url,
                "note": note,
            }
        )
        item["updated_at"] = now_iso()

    return expanded, unchanged, skipped, fallback, len(filtered_items), len(targets), actions


def check_stage_label_consistency(
    *,
    core_dir: Path,
    state: dict[str, Any],
    owner: str,
    apply_changes: bool,
    progress: bool,
    repo_filters: list[str] | None = None,
    item_prefix_filters: list[str] | None = None,
) -> tuple[int, int, int, list[dict[str, Any]]]:
    if shutil.which("gh") is None:
        raise RuntimeError("Missing required command: gh")

    repo_filter_set = set(_split_values(repo_filters))
    prefixes = [x.upper() for x in _split_values(item_prefix_filters)]
    actions: list[dict[str, Any]] = []
    checked = 0
    drift = 0
    fixed = 0
    stage_labels = {"stage:seed", "stage:expanded"}

    def _progress(msg: str) -> None:
        if progress:
            print(msg, flush=True)

    for item in state.get("work_items", []):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("item_id", ""))
        if prefixes and not any(item_id.upper().startswith(prefix) for prefix in prefixes):
            continue
        stage = str(item.get("stage", WorkStage.SEEDED.value)).strip().lower()
        if stage not in (WorkStage.SEEDED.value, WorkStage.EXPANDED.value):
            continue
        expected = "stage:seed" if stage == WorkStage.SEEDED.value else "stage:expanded"
        repos = [str(x) for x in item.get("repos", []) if str(x).strip()]
        for repo in repos:
            if repo_filter_set and repo not in repo_filter_set:
                continue
            issues = item.get("issues", {})
            if not isinstance(issues, dict):
                continue
            issue_meta = issues.get(repo, {})
            if not isinstance(issue_meta, dict):
                continue
            issue_ref = str(issue_meta.get("url", "") or issue_meta.get("number", "")).strip()
            if not issue_ref:
                continue

            checked += 1
            repo_slug = f"{owner}/{repo}"
            view = _gh_json(
                [
                    "issue",
                    "view",
                    issue_ref,
                    "--repo",
                    repo_slug,
                    "--json",
                    "number,url,title,labels",
                ]
            )
            label_entries = view.get("labels", [])
            labels = {
                str(entry.get("name", "")).strip().lower()
                for entry in label_entries
                if isinstance(entry, dict)
            }
            if expected in labels and not (stage_labels - {expected}).intersection(labels):
                actions.append(
                    {
                        "action": "label_consistent",
                        "item_id": item_id,
                        "repo": repo,
                        "title": item.get("title", ""),
                        "url": str(view.get("url", issue_ref)),
                        "note": expected,
                    }
                )
                continue

            drift += 1
            note = f"expected={expected}; current={','.join(sorted(labels & stage_labels)) or 'none'}"
            if apply_changes:
                args = ["gh", "issue", "edit", issue_ref, "--repo", repo_slug, "--add-label", expected]
                for label in sorted((stage_labels - {expected}) & labels):
                    args.extend(["--remove-label", label])
                subprocess.run(args, text=True, capture_output=True, check=False)
                fixed += 1
                _progress(f"[expand-label-check] fixed repo={repo} item={item_id} issue={issue_ref}")
                actions.append(
                    {
                        "action": "label_fixed",
                        "item_id": item_id,
                        "repo": repo,
                        "title": item.get("title", ""),
                        "url": str(view.get("url", issue_ref)),
                        "note": note,
                    }
                )
            else:
                actions.append(
                    {
                        "action": "label_drift",
                        "item_id": item_id,
                        "repo": repo,
                        "title": item.get("title", ""),
                        "url": str(view.get("url", issue_ref)),
                        "note": note,
                    }
                )

    return checked, drift, fixed, actions


def reset_repo_issues(
    *,
    core_dir: Path,
    state: dict[str, Any],
    owner: str,
    apply_changes: bool,
    batch_id: str,
    scope: str,
    scan_open: bool,
    progress: bool,
) -> tuple[int, int, int, list[dict[str, Any]]]:
    if shutil.which("gh") is None:
        raise RuntimeError("Missing required command: gh")

    closed = 0
    planned = 0
    skipped = 0
    actions: list[dict[str, Any]] = []

    def _progress(msg: str) -> None:
        if progress:
            print(msg, flush=True)

    if scan_open:
        repo_names: dict[str, None] = {}
        for item in state.get("work_items", []):
            if not isinstance(item, dict):
                continue
            for repo in item.get("repos", []):
                if str(repo).strip():
                    repo_names[str(repo).strip()] = None
            issues = item.get("issues", {})
            if isinstance(issues, dict):
                for repo in issues.keys():
                    if str(repo).strip():
                        repo_names[str(repo).strip()] = None

        for repo in sorted(repo_names.keys()):
            repo_slug = f"{owner}/{repo}"
            query = '"## Orchestrator Work Item" in:body'
            if scope == "batch":
                if not batch_id:
                    skipped += 1
                    actions.append(
                        {
                            "action": "skip_reset_missing_batch_id",
                            "item_id": "",
                            "repo": repo,
                            "title": "",
                            "url": "",
                            "note": "scope=batch requires batch_id",
                        }
                    )
                    continue
                query += f' "Batch ID: `{batch_id}`" in:body'

            _progress(f"[ticket-reset] scanning repo={repo_slug} query={query}")
            found = _gh_json(
                [
                    "issue",
                    "list",
                    "--repo",
                    repo_slug,
                    "--state",
                    "open",
                    "--search",
                    query,
                    "--json",
                    "number,url,title,state",
                    "--limit",
                    "200",
                ]
            )
            if not isinstance(found, list):
                continue
            for match in found:
                if not isinstance(match, dict):
                    continue
                issue_ref = str(match.get("url") or match.get("number") or "").strip()
                if not issue_ref:
                    continue
                if not apply_changes:
                    planned += 1
                    actions.append(
                        {
                            "action": "would_close_scan",
                            "item_id": "",
                            "repo": repo,
                            "title": str(match.get("title", "")),
                            "url": str(match.get("url", "")),
                            "note": f"scope={scope}",
                        }
                    )
                    _progress(f"[ticket-reset] would_close repo={repo_slug} issue={issue_ref}")
                    continue
                _progress(f"[ticket-reset] closing repo={repo_slug} issue={issue_ref}")
                _gh_text(
                    [
                        "issue",
                        "close",
                        issue_ref,
                        "--repo",
                        repo_slug,
                        "--comment",
                        f"Closed by orchestrator reset (scope={scope}).",
                    ]
                )
                closed += 1
                append_event(
                    core_dir,
                    "ticket.closed",
                    f"Closed issue in {repo} via reset scan",
                    {"repo": repo, "issue_ref": issue_ref, "scope": scope, "batch_id": batch_id},
                )
                actions.append(
                    {
                        "action": "closed_scan",
                        "item_id": "",
                        "repo": repo,
                        "title": str(match.get("title", "")),
                        "url": str(match.get("url", "")),
                        "note": f"scope={scope}",
                    }
                )

        if apply_changes:
            closed_urls = {str(a.get("url", "")) for a in actions if str(a.get("action", "")).startswith("closed")}
            for item in state.get("work_items", []):
                issues = item.get("issues", {})
                if not isinstance(issues, dict):
                    continue
                for repo, issue_meta in list(issues.items()):
                    if not isinstance(issue_meta, dict):
                        continue
                    if str(issue_meta.get("url", "")) in closed_urls:
                        issues.pop(repo, None)
        return closed, planned, skipped, actions

    for item in state.get("work_items", []):
        issues = item.get("issues")
        if not isinstance(issues, dict):
            continue
        for repo, issue_meta in list(issues.items()):
            if not isinstance(issue_meta, dict):
                continue
            issue_batch = str(issue_meta.get("batch_id", "")).strip()
            if scope == "batch":
                if not batch_id:
                    skipped += 1
                    actions.append(
                        {
                            "action": "skip_reset_missing_batch_id",
                            "item_id": item.get("item_id", ""),
                            "repo": repo,
                            "title": item.get("title", ""),
                            "url": str(issue_meta.get("url", "")),
                            "note": "scope=batch requires batch_id",
                        }
                    )
                    continue
                if issue_batch != batch_id:
                    skipped += 1
                    actions.append(
                        {
                            "action": "skip_reset_batch_mismatch",
                            "item_id": item.get("item_id", ""),
                            "repo": repo,
                            "title": item.get("title", ""),
                            "url": str(issue_meta.get("url", "")),
                            "note": f"issue_batch={issue_batch or 'unknown'} expected={batch_id}",
                        }
                    )
                    continue

            issue_ref = str(issue_meta.get("url") or issue_meta.get("number") or "").strip()
            if not issue_ref:
                skipped += 1
                actions.append(
                    {
                        "action": "skip_reset_missing_issue_ref",
                        "item_id": item.get("item_id", ""),
                        "repo": repo,
                        "title": item.get("title", ""),
                        "url": "",
                        "note": "missing issue url/number in state",
                    }
                )
                continue

            repo_slug = f"{owner}/{repo}"
            if not apply_changes:
                planned += 1
                actions.append(
                    {
                        "action": "would_close",
                        "item_id": item.get("item_id", ""),
                        "repo": repo,
                        "title": item.get("title", ""),
                        "url": str(issue_meta.get("url", "")),
                        "note": f"scope={scope} batch={issue_batch or 'unknown'}",
                    }
                )
                _progress(
                    f"[ticket-reset] would_close repo={repo_slug} "
                    f"item={item.get('item_id','')} issue={issue_ref}"
                )
                continue

            _progress(f"[ticket-reset] closing repo={repo_slug} item={item.get('item_id','')} issue={issue_ref}")
            _gh_text(
                [
                    "issue",
                    "close",
                    issue_ref,
                    "--repo",
                    repo_slug,
                    "--comment",
                    f"Closed by orchestrator reset (scope={scope}).",
                ]
            )
            issues.pop(repo, None)
            closed += 1
            append_event(
                core_dir,
                "ticket.closed",
                f"Closed issue for {repo} and item {item.get('item_id','')}",
                {
                    "repo": repo,
                    "item_id": item.get("item_id", ""),
                    "issue_ref": issue_ref,
                    "scope": scope,
                    "batch_id": issue_batch,
                },
            )
            actions.append(
                {
                    "action": "closed",
                    "item_id": item.get("item_id", ""),
                    "repo": repo,
                    "title": item.get("title", ""),
                    "url": str(issue_meta.get("url", "")),
                    "note": f"scope={scope} batch={issue_batch or 'unknown'}",
                }
            )

    return closed, planned, skipped, actions


def sync_repo_issues(
    *,
    core_dir,
    state: dict[str, Any],
    owner: str,
    apply_changes: bool,
    assess_before_create: bool,
    assess_agent: str,
    strict_assess: bool,
    agent_profile: str,
    agent_model: str,
    agent_reasoning: str,
    agent_personality: str,
    assess_timeout_sec: int,
    assess_log_max_chars: int,
    batch_id: str,
    progress: bool,
    repo_filters: list[str] | None = None,
    roadmap_filters: list[str] | None = None,
    item_prefix_filters: list[str] | None = None,
) -> tuple[int, int, int, int, int, int, list[dict[str, Any]]]:
    if shutil.which("gh") is None:
        raise RuntimeError("Missing required command: gh")

    def _split_values(values: list[str] | None) -> list[str]:
        out: list[str] = []
        for raw in values or []:
            for part in str(raw).split(","):
                token = part.strip()
                if token:
                    out.append(token)
        return out

    repo_filter_set = {x for x in _split_values(repo_filters)}
    roadmap_filter_values = _split_values(roadmap_filters)
    item_prefixes = [x.upper() for x in _split_values(item_prefix_filters)]

    roadmap_filter_names = {Path(x).name for x in roadmap_filter_values}
    roadmap_filter_paths = {str(Path(x).resolve()) for x in roadmap_filter_values if x}

    def _matches_item(item: dict[str, Any]) -> bool:
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
        if not isinstance(it, dict):
            continue
        if _matches_item(it):
            filtered_items.append(it)

    created = 0
    linked_existing = 0
    skipped = 0
    planned_create = 0
    work_item_count = 0
    repo_target_count = 0
    actions: list[dict[str, Any]] = []
    targets_total = sum(len(_target_repos(x)) for x in filtered_items)
    target_idx = 0
    create_total_est = 0
    create_idx = 0
    if apply_changes:
        for it in filtered_items:
            repos = _target_repos(it)
            issues = it.get("issues", {})
            if not isinstance(issues, dict):
                issues = {}
            for repo in repos:
                current = issues.get(repo, {})
                if not isinstance(current, dict) or not str(current.get("url", "")).strip():
                    create_total_est += 1

    def _progress(msg: str) -> None:
        if progress:
            print(msg, flush=True)

    for item_idx, item in enumerate(filtered_items, start=1):
        raw_repos = [str(x) for x in item.get("repos", []) if str(x).strip()]
        repos = _target_repos(item)
        item_id = str(item.get("item_id", ""))
        stage = str(item.get("stage", WorkStage.SEEDED.value)).strip().lower()
        if stage == WorkStage.DONE.value:
            actions.append(
                {
                    "action": "skipped_done_stage",
                    "item_id": item_id,
                    "repo": "",
                    "title": item.get("title", ""),
                    "url": "",
                    "note": "work item is already in done stage",
                }
            )
            _progress(f"[ticket] skip item={item_id} reason=done stage")
            continue
        if not repos:
            if repo_filter_set and raw_repos:
                _progress(f"[ticket] filtered item={item.get('item_id','')} reason=repo filter")
                actions.append(
                    {
                        "action": "filtered_out_repo",
                        "item_id": item.get("item_id", ""),
                        "repo": "",
                        "title": item.get("title", ""),
                        "url": "",
                        "note": f"repo filter: {','.join(sorted(repo_filter_set))}",
                    }
                )
                continue

            work_item_count += 1
            skipped += 1
            _progress(f"[ticket] skip item={item.get('item_id','')} reason=no repos after filters")
            actions.append(
                {
                    "action": "skipped_no_repo",
                    "item_id": item.get("item_id", ""),
                    "repo": "",
                    "title": item.get("title", ""),
                    "url": "",
                    "note": "work item has no repos after filters",
                }
            )
            continue

        work_item_count += 1
        issues = item.setdefault("issues", {})
        for repo in repos:
            repo_target_count += 1
            target_idx += 1
            seed_key = make_idempotency_key(
                item_id=item_id,
                repo=repo,
                batch_id=batch_id.strip(),
                stage=WorkStage.SEEDED.value,
            )
            repo_defaults = repo_defaults_for(repo)
            _progress(
                f"[ticket] target {target_idx}/{targets_total} item {item_idx}/{len(filtered_items)} "
                f"repo={repo} item={item.get('item_id','')}"
            )
            repo_slug = f"{owner}/{repo}"
            title = _issue_title(item)
            _progress(f"[ticket] checking existing issues repo={repo_slug}")
            found = _gh_json(
                [
                    "issue",
                    "list",
                    "--repo",
                    repo_slug,
                    "--state",
                    "open",
                    "--search",
                    f'"{item["item_id"]}" in:title',
                    "--json",
                    "number,url,title,state",
                    "--limit",
                    "5",
                ]
            )
            match = None
            if isinstance(found, list):
                for f in found:
                    if not isinstance(f, dict):
                        continue
                    if str(f.get("title", "")).startswith(f"[{item['item_id']}]"):
                        match = f
                        break
                if match is None and found:
                    match = found[0]

            if match is not None:
                existing_issue = issues.get(repo, {})
                if apply_changes and create_total_est > 0:
                    if not isinstance(existing_issue, dict) or not str(existing_issue.get("url", "")).strip():
                        create_total_est -= 1
                issues[repo] = {
                    "repo": repo,
                    "number": match.get("number"),
                    "url": match.get("url", ""),
                    "state": match.get("state", ""),
                    "batch_id": str(existing_issue.get("batch_id", "")) if isinstance(existing_issue, dict) else "",
                    "assess_status": str(existing_issue.get("assess_status", "")) if isinstance(existing_issue, dict) else "",
                    "assess_log": str(existing_issue.get("assess_log", "")) if isinstance(existing_issue, dict) else "",
                    "updated_at": now_iso(),
                }
                linked_existing += 1
                append_event(
                    core_dir,
                    "ticket.linked",
                    f"Linked existing issue for {repo} and item {item['item_id']}",
                    {"repo": repo, "item_id": item["item_id"], "url": match.get("url", "")},
                )
                actions.append(
                    {
                        "action": "linked_existing",
                        "item_id": item.get("item_id", ""),
                        "repo": repo,
                        "title": item.get("title", ""),
                        "url": match.get("url", ""),
                        "note": f"issue #{match.get('number')}",
                    }
                )
                _progress(
                    f"[ticket] linked existing repo={repo} item={item.get('item_id','')} "
                    f"url={match.get('url','')}"
                )
                transition_item_stage(
                    item,
                    WorkStage.SEEDED.value,
                    reason="seed issue linked/created",
                )
                if apply_changes:
                    record_idempotency_key(
                        state,
                        key=seed_key,
                        item_id=item_id,
                        repo=repo,
                        batch_id=batch_id.strip(),
                        stage=WorkStage.SEEDED.value,
                        action="linked_existing",
                        note=f"issue #{match.get('number')}",
                    )
                label_issue_ref = str(match.get("url", "") or match.get("number", "")).strip()
                if apply_changes:
                    try:
                        _apply_issue_labels(
                            repo_slug,
                            label_issue_ref,
                            _issue_labels(
                                repo=repo,
                                batch_id=batch_id.strip(),
                                stage="seed",
                                extra_labels=list(repo_defaults.extra_labels),
                            ),
                        )
                    except Exception as exc:
                        actions.append(
                            {
                                "action": "label_apply_failed",
                                "item_id": item.get("item_id", ""),
                                "repo": repo,
                                "title": item.get("title", ""),
                                "url": label_issue_ref,
                                "note": str(exc)[:220],
                            }
                        )
                        _progress(
                            f"[ticket] label apply failed repo={repo} item={item.get('item_id','')} "
                            f"error={str(exc)[:140]}"
                        )
                continue

            if not apply_changes:
                planned_create += 1
                append_event(
                    core_dir,
                    "ticket.dry_run",
                    f"Would create issue for {repo} and item {item['item_id']}",
                    {"repo": repo, "item_id": item["item_id"]},
                )
                actions.append(
                    {
                        "action": "would_create",
                        "item_id": item.get("item_id", ""),
                        "repo": repo,
                        "title": item.get("title", ""),
                        "url": "",
                        "note": "",
                    }
                )
                _progress(f"[ticket] dry-run would_create repo={repo} item={item.get('item_id','')}")
                continue

            existing_issue_meta = issues.get(repo, {})
            if (
                has_idempotency_key(state, seed_key)
                and isinstance(existing_issue_meta, dict)
                and str(existing_issue_meta.get("url", "")).strip()
            ):
                actions.append(
                    {
                        "action": "idempotency_skip",
                        "item_id": item_id,
                        "repo": repo,
                        "title": item.get("title", ""),
                        "url": str(existing_issue_meta.get("url", "")),
                        "note": "idempotency key already recorded for seed stage",
                    }
                )
                _progress(
                    f"[ticket] idempotency skip repo={repo} item={item_id} "
                    "reason=seed key already recorded"
                )
                continue

            assessment: dict[str, Any] | None = None
            assess_status = "not_run"
            assess_log = ""
            assess_elapsed_sec = 0.0
            if assess_before_create:
                try:
                    resolved_backend = _resolve_assess_backend(
                        assess_agent=assess_agent,
                        model=agent_model.strip(),
                    )
                    if resolved_backend == "codex":
                        assessment, assess_log, assess_elapsed_sec = _assess_with_codex(
                            core_dir=core_dir,
                            repo=repo,
                            item=item,
                            model=agent_model.strip(),
                            reasoning=agent_reasoning.strip(),
                            personality=agent_personality.strip(),
                            timeout_sec=max(30, int(assess_timeout_sec)),
                            log_max_chars=max(500, int(assess_log_max_chars)),
                            progress_cb=_progress if progress else None,
                        )
                        assess_status = "assessed"
                    elif resolved_backend == "gemini_queue":
                        try:
                            assessment, assess_log, assess_elapsed_sec = _assess_with_gemini(
                                core_dir=core_dir,
                                repo=repo,
                                item=item,
                                model=agent_model.strip(),
                                timeout_sec=max(30, int(assess_timeout_sec)),
                                log_max_chars=max(500, int(assess_log_max_chars)),
                                progress_cb=_progress if progress else None,
                            )
                            assess_status = "assessed"
                            resolved_backend = "gemini"
                        except Exception as gemini_exc:
                            if strict_assess:
                                raise
                            assessment, assess_log, assess_elapsed_sec = _assess_with_cli_queue(
                                core_dir=core_dir,
                                repo=repo,
                                item=item,
                                provider="gemini",
                                profile=agent_profile,
                                model=agent_model.strip(),
                                reasoning=agent_reasoning.strip(),
                                personality=agent_personality.strip(),
                            )
                            failed_log = _extract_log_path(str(gemini_exc))
                            if failed_log:
                                assess_log = f"{failed_log}; {assess_log}"
                            assess_status = "queued_cli"
                            resolved_backend = "gemini_queue_fallback"
                    elif resolved_backend == "queue":
                        assessment, assess_log, assess_elapsed_sec = _assess_with_cli_queue(
                            core_dir=core_dir,
                            repo=repo,
                            item=item,
                            provider="local-cli",
                            profile=agent_profile,
                            model=agent_model.strip(),
                            reasoning=agent_reasoning.strip(),
                            personality=agent_personality.strip(),
                        )
                        assess_status = "queued_cli"
                    elif resolved_backend == "none":
                        assess_status = "disabled"
                    else:
                        raise RuntimeError(f"Unsupported assess backend: {resolved_backend}")
                    append_event(
                        core_dir,
                        "ticket.assessed",
                        f"Assessed work item with {resolved_backend} for {repo}",
                        {
                            "repo": repo,
                            "item_id": item["item_id"],
                            "elapsed_sec": round(assess_elapsed_sec, 1),
                            "log": assess_log,
                        },
                    )
                    actions.append(
                        {
                            "action": "assessed",
                            "item_id": item.get("item_id", ""),
                            "repo": repo,
                            "title": item.get("title", ""),
                            "url": "",
                            "note": f"agent={resolved_backend} elapsed={assess_elapsed_sec:.1f}s",
                        }
                    )
                except Exception as exc:
                    if strict_assess:
                        raise
                    assess_status = "fallback"
                    assess_log = _extract_log_path(str(exc))
                    append_event(
                        core_dir,
                        "ticket.assess_failed",
                        f"Assessment failed for {repo}; falling back to default acceptance",
                        {
                            "repo": repo,
                            "item_id": item.get("item_id", ""),
                            "error": str(exc),
                            "strict_assess": strict_assess,
                            "log": assess_log,
                        },
                    )
                    actions.append(
                        {
                            "action": "assess_failed_fallback",
                            "item_id": item.get("item_id", ""),
                            "repo": repo,
                            "title": item.get("title", ""),
                            "url": "",
                            "note": str(exc)[:220],
                        }
                    )
                    _progress(
                        f"[ticket] assess fallback repo={repo} item={item.get('item_id','')} "
                        f"error={str(exc)[:140]}"
                    )
            elif apply_changes:
                assess_status = "disabled"

            create_idx += 1
            create_total_display = create_total_est if create_total_est > 0 else create_idx
            _progress(f"[ticket] creating ticket {create_idx}/{create_total_display} repo={repo_slug}")
            url = _gh_text(
                [
                    "issue",
                    "create",
                    "--repo",
                    repo_slug,
                    "--title",
                    title,
                    "--body",
                    _issue_body(
                        item,
                        repo=repo,
                        repo_defaults=repo_defaults,
                        batch_id=batch_id.strip(),
                    ),
                ]
            )
            if not url:
                raise RuntimeError(f"Empty issue URL when creating issue in {repo_slug}")
            view = _gh_json(["issue", "view", url, "--repo", repo_slug, "--json", "number,url,state,title"])
            issue_ref = str(view.get("url", "") or view.get("number", "")).strip()
            try:
                _apply_issue_labels(
                    repo_slug,
                    issue_ref,
                    _issue_labels(
                        repo=repo,
                        batch_id=batch_id.strip(),
                        stage="seed",
                        extra_labels=list(repo_defaults.extra_labels),
                    ),
                )
            except Exception as exc:
                actions.append(
                    {
                        "action": "label_apply_failed",
                        "item_id": item.get("item_id", ""),
                        "repo": repo,
                        "title": item.get("title", ""),
                        "url": issue_ref,
                        "note": str(exc)[:220],
                    }
                )
                _progress(
                    f"[ticket] label apply failed repo={repo} item={item.get('item_id','')} "
                    f"error={str(exc)[:140]}"
                )
            issues[repo] = {
                "repo": repo,
                "number": view.get("number"),
                "url": view.get("url", url),
                "state": view.get("state", ""),
                "batch_id": batch_id.strip(),
                "assess_status": assess_status,
                "assess_log": assess_log,
                "updated_at": now_iso(),
            }
            created += 1
            append_event(
                core_dir,
                "ticket.created",
                f"Created issue for {repo} and item {item['item_id']}",
                {
                    "repo": repo,
                    "item_id": item["item_id"],
                    "url": view.get("url", url),
                    "batch_id": batch_id.strip(),
                    "assess_status": assess_status,
                    "assess_log": assess_log,
                },
            )
            actions.append(
                {
                    "action": "created",
                    "item_id": item.get("item_id", ""),
                    "repo": repo,
                    "title": item.get("title", ""),
                    "url": view.get("url", url),
                    "note": f"issue #{view.get('number')} assess={assess_status}",
                }
            )
            _progress(f"[ticket] created repo={repo} item={item.get('item_id','')} url={view.get('url', url)}")
            record_idempotency_key(
                state,
                key=seed_key,
                item_id=item_id,
                repo=repo,
                batch_id=batch_id.strip(),
                stage=WorkStage.SEEDED.value,
                action="created",
                note=f"issue #{view.get('number')}",
            )
            transition_item_stage(
                item,
                WorkStage.SEEDED.value,
                reason="seed issue linked/created",
            )
        item["updated_at"] = now_iso()

    _progress(
        f"[ticket] done work_items={work_item_count} repo_targets={repo_target_count} "
        f"created={created} linked_existing={linked_existing} would_create={planned_create} skipped_no_repo={skipped}"
    )
    return created, linked_existing, skipped, planned_create, work_item_count, repo_target_count, actions
