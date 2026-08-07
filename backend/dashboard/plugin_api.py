"""Local backend for the Hermes Agent Dock desktop plugin.

The API is mounted at /api/plugins/hermes-agent-dock by Hermes.  It discovers
real Hermes profiles, launches direct profile-scoped CLI sessions without a
shell, and exposes a privacy-reduced view of the optional achievements plugin.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

try:
    from hermes_constants import get_hermes_home
except ImportError:  # pragma: no cover - only for source-tree linting
    def get_hermes_home() -> Path:
        configured = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(configured) if configured else Path.home() / ".hermes"

try:
    from hermes_constants import VALID_REASONING_EFFORTS
except ImportError:  # pragma: no cover - only for source-tree linting
    VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")

router = APIRouter()

MAX_MESSAGE_CHARS = 12_000
MAX_RESPONSE_CHARS = 120_000
MAX_CONCURRENT_JOBS = 4
JOB_TIMEOUT_SECONDS = 15 * 60
CATALOG_TIMEOUT_SECONDS = 60
CATALOG_CACHE_SECONDS = 15
JOB_RETENTION_SECONDS = 60 * 60
MAX_RETAINED_JOBS = 200
KANBAN_BOARD = "executive-organization"
SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}_[A-Za-z0-9]+$")
SESSION_LINE_RE = re.compile(r"(?:^|\n)session_id:\s*([A-Za-z0-9_-]+)\s*$", re.MULTILINE)
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
REASONING_EFFORTS = ("none", *VALID_REASONING_EFFORTS)

_JOBS: dict[str, dict[str, Any]] = {}
_REQUEST_JOBS: dict[str, str] = {}
_CATALOG_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JOBS_LOCK = threading.RLock()
_CATALOG_LOCK = threading.RLock()


class SendRequest(BaseModel):
    profile: str
    model: str | None = None
    provider: str | None = None
    message: str
    request_id: str | None = None
    session_id: str | None = None
    reasoning_effort: str | None = "none"
    fast: bool = False
    assign_task: bool = False

    @field_validator("fast", "assign_task", mode="before")
    @classmethod
    def require_json_boolean(cls, value: Any) -> bool:
        if type(value) is not bool:
            raise ValueError("must be a JSON boolean")
        return value

    @field_validator("message", mode="before")
    @classmethod
    def normalize_bounded_message(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("message must be a string")
        if len(value) > MAX_MESSAGE_CHARS:
            raise ValueError(f"Message exceeds {MAX_MESSAGE_CHARS} characters")
        normalized = value.strip()
        if not normalized:
            raise ValueError("Message is empty")
        return normalized


def _root_home() -> Path:
    """Return the default Hermes root even when this API runs in a profile."""
    home = Path(get_hermes_home()).resolve()
    if home.parent.name.lower() == "profiles":
        return home.parent.parent
    return home


def _profile_rows() -> list[dict[str, Any]]:
    from hermes_cli.profiles import list_profiles

    rows: list[dict[str, Any]] = []
    for info in list_profiles():
        name = str(getattr(info, "name", "") or "").strip().lower()
        if not PROFILE_RE.fullmatch(name):
            continue
        rows.append(
            {
                "name": name,
                "is_default": bool(getattr(info, "is_default", False)),
                "gateway_running": bool(getattr(info, "gateway_running", False)),
                "model": str(getattr(info, "model", "") or ""),
                "provider": str(getattr(info, "provider", "") or ""),
                "description": str(getattr(info, "description", "") or "")[:280],
            }
        )
    rows.sort(key=lambda row: (not row["is_default"], row["name"]))
    return rows


def _allowed_profiles() -> set[str]:
    return {row["name"] for row in _profile_rows()}


def _normalize_profile(profile: str) -> str:
    normalized = (profile or "").strip().lower()
    if not PROFILE_RE.fullmatch(normalized) or normalized not in _allowed_profiles():
        raise ValueError("Unknown Hermes profile")
    return normalized


def _profile_row(profile: str) -> dict[str, Any]:
    normalized = _normalize_profile(profile)
    selected = next((row for row in _profile_rows() if row["name"] == normalized), None)
    if selected is None:  # pragma: no cover - guarded by _normalize_profile
        raise ValueError("Unknown Hermes profile")
    return selected


def _profile_home(profile: str) -> Path:
    """Return the profile's isolated Hermes home for a child process."""
    selected = _profile_row(profile)
    if selected["is_default"]:
        return _root_home()
    # The profile name has already passed PROFILE_RE and came from the
    # installed profile inventory, so it cannot escape the profiles directory.
    return _root_home() / "profiles" / selected["name"]


def _runner_path() -> Path:
    return Path(__file__).resolve().with_name("dock_runner.py")


def _kanban_module():
    from hermes_cli import kanban_db

    return kanban_db


def _kanban_workspace() -> Path:
    metadata_path = _root_home() / "kanban" / "boards" / KANBAN_BOARD / "board.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        workspace = Path(str(metadata.get("default_workdir") or "")).resolve()
    except Exception as exc:
        raise RuntimeError(f"Kanban board {KANBAN_BOARD!r} is unavailable") from exc
    if not workspace.is_dir():
        raise RuntimeError(f"Kanban board {KANBAN_BOARD!r} has no valid workspace")
    return workspace


def _task_title(message: str) -> str:
    first_line = next((line.strip() for line in message.splitlines() if line.strip()), "Assigned task")
    first_line = re.sub(r"^(?:task|assign(?:ment)?)\s*[:\-]\s*", "", first_line, flags=re.IGNORECASE)
    return first_line[:160] or "Assigned task"


def _create_kanban_task(request: SendRequest, job_id: str) -> str | None:
    if not request.assign_task:
        return None
    kb = _kanban_module()
    conn = kb.connect(board=KANBAN_BOARD)
    try:
        return kb.create_task(
            conn,
            title=_task_title(request.message),
            body=(
                "Assigned by Dad through Hermes Agent Dock.\n\n"
                f"Accountable profile: {request.profile.strip().lower()}\n"
                f"Agent Dock job: {job_id}\n\n"
                f"{request.message.strip()}"
            ),
            assignee=request.profile.strip().lower(),
            created_by="default",
            workspace_kind="dir",
            workspace_path=str(_kanban_workspace()),
            priority=50,
            idempotency_key=f"agent-dock:{request.profile.strip().lower()}:{request.request_id or job_id}",
            model_override=(request.model or "").strip() or None,
            provider_override=(request.provider or "").strip() or None,
            reasoning_effort=request.reasoning_effort or "none",
            initial_status="running",
            session_id=request.session_id,
            board=KANBAN_BOARD,
        )
    finally:
        conn.close()


def _settle_kanban_task(task_id: str | None, status: str, detail: str, job_id: str) -> None:
    if not task_id:
        return
    kb = _kanban_module()
    conn = kb.connect(board=KANBAN_BOARD)
    try:
        if status == "done":
            kb.add_comment(
                conn, task_id, "agent-dock", f"Agent response for job {job_id}:\n\n{detail[:12000]}"
            )
            kb.block_task(
                conn, task_id,
                reason="Agent Dock response captured; awaiting Dad/CEO verification before completion.",
                kind="needs_input",
            )
            return
        kb.add_comment(
            conn, task_id, "agent-dock", f"Agent Dock {status} for job {job_id}: {detail[:12000]}"
        )
        kb.block_task(
            conn,
            task_id,
            reason=detail[:1000],
            kind="needs_input" if status == "cancelled" else "capability",
        )
    finally:
        conn.close()


def _safe_runner_error(stderr: str, returncode: int) -> str:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", stderr or "")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("session_id:")]
    detail = lines[-1] if lines else f"Hermes runner exited with code {returncode}"
    try:
        from agent.redact import redact_sensitive_text

        detail = redact_sensitive_text(detail)
    except Exception:
        detail = "Hermes runner failed; inspect local Hermes logs"
    return f"Agent session failed: {detail}"[:500]


def _settle_job_kanban(job_id: str, status: str, detail: str) -> None:
    with _JOBS_LOCK:
        task_id = _JOBS.get(job_id, {}).get("kanban_task_id")
    if not task_id:
        return
    try:
        _settle_kanban_task(task_id, status, detail, job_id)
    except Exception as exc:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job:
                job["kanban_error"] = f"Kanban update failed: {exc}"[:500]


def _catalog_command() -> list[str]:
    return [sys.executable, str(_runner_path()), "--catalog"]


def _catalog_environment(profile: str) -> dict[str, str]:
    env = _child_env()
    env["HERMES_HOME"] = str(_profile_home(profile))
    return env


def _load_model_catalog(profile: str, *, refresh: bool = False) -> dict[str, Any]:
    normalized = _normalize_profile(profile)
    now = time.monotonic()
    with _CATALOG_LOCK:
        cached = _CATALOG_CACHE.get(normalized)
        if not refresh and cached and now - cached[0] < CATALOG_CACHE_SECONDS:
            return cached[1]

    try:
        result = subprocess.run(
            _catalog_command(),
            cwd=str(_root_home()),
            env=_catalog_environment(normalized),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CATALOG_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Hermes model catalog timed out") from exc
    if result.returncode != 0:
        raise RuntimeError("Hermes model catalog could not be loaded")
    try:
        payload = json.loads(result.stdout or "")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Hermes model catalog returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
        raise RuntimeError("Hermes model catalog returned an invalid payload")

    with _CATALOG_LOCK:
        _CATALOG_CACHE[normalized] = (time.monotonic(), payload)
    return payload


def _provider_row(catalog: dict[str, Any], provider: str) -> dict[str, Any] | None:
    for row in catalog.get("providers", []):
        if isinstance(row, dict) and str(row.get("slug") or "") == provider:
            return row
    return None


def _validate_request(request: SendRequest) -> dict[str, str]:
    profile = _normalize_profile(request.profile)
    message = request.message.strip()
    if not message:
        raise ValueError("Message is empty")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"Message exceeds {MAX_MESSAGE_CHARS} characters")
    if request.session_id and not SESSION_ID_RE.fullmatch(request.session_id):
        raise ValueError("Invalid Hermes session ID")
    if request.request_id and not REQUEST_ID_RE.fullmatch(request.request_id):
        raise ValueError("Invalid request ID")

    effort = "none" if request.reasoning_effort is None else request.reasoning_effort.strip()
    if effort not in REASONING_EFFORTS:
        raise ValueError("Invalid reasoning effort")

    selected_profile = _profile_row(profile)
    catalog = _load_model_catalog(profile)
    provider = (request.provider or "").strip() or selected_profile.get("provider") or str(catalog.get("provider") or "")
    model = (request.model or "").strip() or selected_profile.get("model") or str(catalog.get("model") or "")
    if not provider:
        raise ValueError("Provider is not configured for this profile")
    provider_entry = _provider_row(catalog, provider)
    if provider_entry is None:
        raise ValueError("Provider is not present in the Hermes model catalog")
    if not model:
        if request.fast or effort != "none":
            raise ValueError("A model is required for fast or reasoning mode")
        return {"profile": profile, "provider": provider, "model": "", "reasoning_effort": effort}

    models = provider_entry.get("models")
    if not isinstance(models, list) or model not in models:
        raise ValueError("Model is not configured for the selected provider")
    capabilities = provider_entry.get("capabilities")
    model_capabilities = capabilities.get(model, {}) if isinstance(capabilities, dict) else {}
    if not isinstance(model_capabilities, dict):
        model_capabilities = {}
    if effort != "none" and not bool(model_capabilities.get("reasoning")):
        raise ValueError("Selected model does not support reasoning")
    if request.fast and not bool(model_capabilities.get("fast")):
        raise ValueError("Selected model does not support fast mode")
    return {"profile": profile, "provider": provider, "model": model, "reasoning_effort": effort}


def _build_command(request: SendRequest) -> list[str]:
    _validate_request(request)
    return [sys.executable, str(_runner_path()), "--chat"]


def _child_env() -> dict[str, str]:
    """Use Hermes' audited model-driving subprocess environment policy."""
    try:
        from tools.environments.local import hermes_subprocess_env

        return hermes_subprocess_env(inherit_credentials=True)
    except Exception:
        env = os.environ.copy()
        # Provider credentials may be required by the selected profile.  Strip
        # gateway/account controls that a direct model session never needs.
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "DISCORD_BOT_TOKEN",
            "SLACK_BOT_TOKEN",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "HERMES_DASHBOARD_SESSION_TOKEN",
            "GATEWAY_RELAY_SECRET",
            "GATEWAY_RELAY_DELIVERY_KEY",
        ):
            env.pop(key, None)
        env.setdefault("PYTHONUTF8", "1")
        return env


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if not key.startswith("_")}


def _evict_completed_jobs(now: int | None = None) -> None:
    """Bound terminal-job memory while preserving a one-hour retry window."""
    current = int(time.time()) if now is None else now
    with _JOBS_LOCK:
        terminal = []
        remove: set[str] = set()
        for job_id, job in _JOBS.items():
            if job.get("status") not in {"done", "error", "cancelled"}:
                continue
            finished = int(job.get("finished_at") or job.get("created_at") or current)
            terminal.append((finished, job_id))
            if finished < current - JOB_RETENTION_SECONDS:
                remove.add(job_id)
        retained = sorted(item for item in terminal if item[1] not in remove)
        if len(retained) > MAX_RETAINED_JOBS:
            remove.update(job_id for _, job_id in retained[: len(retained) - MAX_RETAINED_JOBS])
        for job_id in remove:
            _JOBS.pop(job_id, None)
        for request_key, job_id in list(_REQUEST_JOBS.items()):
            if job_id not in _JOBS:
                _REQUEST_JOBS.pop(request_key, None)


def _trim(value: str, limit: int = MAX_RESPONSE_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n\n[response truncated by Agent Dock]"


def _runner_payload(request: SendRequest) -> str:
    payload = {
        "message": request.message,
        "model": (request.model or "").strip() or None,
        "provider": (request.provider or "").strip() or None,
        "session_id": request.session_id,
        "reasoning_effort": request.reasoning_effort or "none",
        "fast": bool(request.fast),
    }
    return json.dumps(payload, ensure_ascii=False)


def _begin_job_finalization(job_id: str, response: str, session_id: str | None) -> bool:
    """Atomically win the cancellation race before linked-card settlement."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job or job.get("status") == "cancelled":
            return False
        job.update(
            {
                "status": "finalizing",
                "response": response,
                "session_id": session_id,
            }
        )
        return True


def _publish_job_success(job_id: str) -> bool:
    """Expose terminal success only after optional Kanban settlement finishes."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job or job.get("status") != "finalizing":
            return False
        job.update({"status": "done", "finished_at": int(time.time())})
        return True


def _terminate_process(process: subprocess.Popen[str] | None, platform: str | None = None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        if (platform or os.name) == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            process.wait(timeout=5)
        else:
            process_group = os.getpgid(process.pid)
            os.killpg(process_group, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process_group, signal.SIGKILL)
                process.wait(timeout=5)
    except Exception:
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        except Exception:
            pass


def _run_job(job_id: str, request: SendRequest) -> None:
    process: subprocess.Popen[str] | None = None
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job or job["status"] == "cancelled":
            return
        job["status"] = "running"
        job["started_at"] = int(time.time())

    try:
        command = _build_command(request)
        child_env = _catalog_environment(request.profile)
        popen_kwargs: dict[str, Any] = {
            "args": command,
            "cwd": str(_root_home()),
            "env": child_env,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "shell": False,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if not job or job["status"] == "cancelled":
                return
        process = subprocess.Popen(**popen_kwargs)
        attached = False
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job and job["status"] != "cancelled":
                job["_process"] = process
                attached = True
        if not attached:
            _terminate_process(process)
            return

        try:
            stdout, stderr = process.communicate(input=_runner_payload(request), timeout=JOB_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _terminate_process(process)
            raise RuntimeError("Agent session exceeded the 15 minute timeout")

        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if not job or job["status"] == "cancelled":
                return

        match = SESSION_LINE_RE.search(stderr or "")
        session_id = match.group(1) if match and SESSION_ID_RE.fullmatch(match.group(1)) else request.session_id
        if process.returncode != 0:
            raise RuntimeError(_safe_runner_error(stderr or "", process.returncode))

        response = _trim((stdout or "").strip())
        if not _begin_job_finalization(job_id, response, session_id):
            return
        _settle_job_kanban(job_id, "done", response or "Agent returned no response text")
        _publish_job_success(job_id)
    except Exception as exc:
        message = str(exc)
        if "Agent session failed:" not in message and "timeout" not in message.lower():
            message = "Agent session could not start. Check the selected profile and Hermes logs."
        with _JOBS_LOCK:
            settle_error = bool(_JOBS.get(job_id) and _JOBS[job_id]["status"] != "cancelled")
        if settle_error:
            _settle_job_kanban(job_id, "error", message)
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job and job["status"] != "cancelled":
                job.update({"status": "error", "error": message[:500], "finished_at": int(time.time())})
    finally:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job:
                job.pop("_process", None)


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "hermes-agent-dock", "version": "0.1.0"}


@router.get("/profiles")
async def profiles() -> dict[str, Any]:
    try:
        rows = _profile_rows()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Hermes profiles are unavailable") from exc
    return {
        "profiles": rows,
        "count": len(rows),
        "capabilities": {
            "idempotent_submit": True,
            "model_override": True,
            "reasoning": True,
            "fast": True,
            "model_catalog": True,
            "kanban_assignment": True,
            "kanban_board": KANBAN_BOARD,
        },
    }


@router.get("/models/{profile}")
def models(profile: str) -> dict[str, Any]:
    try:
        normalized = _normalize_profile(profile)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return _load_model_catalog(normalized)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Hermes model catalog is unavailable") from exc


@router.post("/jobs", status_code=202)
def create_job(request: SendRequest) -> dict[str, Any]:
    _evict_completed_jobs()
    request_id = (request.request_id or "").strip()
    if request.assign_task and not request_id:
        raise HTTPException(status_code=400, detail="request_id is required for Kanban assignment")
    request_key = f"{request.profile.strip().lower()}:{request_id}" if request_id else None

    # A transport retry must resolve from the reservation ledger even when
    # provider discovery is temporarily unavailable. Re-check after validation
    # as well so concurrent first submissions still reserve exactly one job.
    if request_key:
        with _JOBS_LOCK:
            existing_id = _REQUEST_JOBS.get(request_key)
            existing = _JOBS.get(existing_id or "")
            if existing:
                return _public_job(existing)

    try:
        _build_command(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    with _JOBS_LOCK:
        if request_key:
            existing_id = _REQUEST_JOBS.get(request_key)
            existing = _JOBS.get(existing_id or "")
            if existing:
                return _public_job(existing)
        active = sum(1 for job in _JOBS.values() if job["status"] in {"queued", "running"})
        if active >= MAX_CONCURRENT_JOBS:
            raise HTTPException(status_code=429, detail="Agent Dock already has four active sessions")
        job_id = uuid.uuid4().hex
        try:
            kanban_task_id = _create_kanban_task(request, job_id)
        except (PermissionError, RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=503, detail=f"Kanban assignment failed: {exc}") from exc
        _JOBS[job_id] = {
            "id": job_id,
            "profile": request.profile.strip().lower(),
            "model": (request.model or "").strip() or None,
            "provider": (request.provider or "").strip() or None,
            "reasoning_effort": request.reasoning_effort or "none",
            "fast": bool(request.fast),
            "request_id": request_id or None,
            "status": "queued",
            "created_at": int(time.time()),
            "started_at": None,
            "finished_at": None,
            "session_id": request.session_id,
            "response": None,
            "error": None,
            "kanban_task_id": kanban_task_id,
            "kanban_board": KANBAN_BOARD if kanban_task_id else None,
            "kanban_error": None,
        }
        if request_key:
            _REQUEST_JOBS[request_key] = job_id

    threading.Thread(target=_run_job, args=(job_id, request), name=f"agent-dock-{job_id[:8]}", daemon=True).start()
    return _public_job(_JOBS[job_id])


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return _public_job(job)


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str) -> dict[str, Any]:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job["status"] in {"done", "error", "cancelled", "finalizing"}:
            return _public_job(job)
        process = job.get("_process")
        job.update({"status": "cancelled", "finished_at": int(time.time()), "error": None})
    _terminate_process(process)
    _settle_job_kanban(job_id, "cancelled", "Cancelled by Dad from Agent Dock")
    return _public_job(job)


def _achievement_snapshot() -> Path | None:
    current = Path(get_hermes_home()).resolve()
    candidates = [
        current / "plugins" / "hermes-achievements" / "scan_snapshot.json",
        _root_home() / "plugins" / "hermes-achievements" / "scan_snapshot.json",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size <= 10 * 1024 * 1024:
                return candidate
        except OSError:
            continue
    return None


@router.get("/achievements")
async def achievements() -> dict[str, Any]:
    path = _achievement_snapshot()
    if path is None:
        return {"available": False, "items": [], "unlocked_count": 0, "total_count": 0, "generated_at": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Achievements snapshot is unreadable") from exc

    raw_items = payload.get("achievements") if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        raw_items = []
    unlocked = [item for item in raw_items if isinstance(item, dict) and item.get("unlocked")]
    unlocked.sort(key=lambda item: int(item.get("unlocked_at") or 0), reverse=True)
    items = []
    for item in unlocked[:12]:
        items.append(
            {
                "id": str(item.get("id") or "")[:100],
                "name": str(item.get("name") or "Achievement")[:120],
                "description": str(item.get("description") or "")[:280],
                "category": str(item.get("category") or "Hermes")[:80],
                "tier": str(item.get("tier") or "Earned")[:40],
                "next_tier": str(item.get("next_tier") or "")[:40] or None,
                "next_threshold": item.get("next_threshold") if isinstance(item.get("next_threshold"), (int, float)) else None,
                "progress": item.get("progress") if isinstance(item.get("progress"), (int, float)) else None,
                "progress_pct": item.get("progress_pct") if isinstance(item.get("progress_pct"), (int, float)) else None,
                "unlocked_at": int(item.get("unlocked_at") or 0),
            }
        )
    return {
        "available": True,
        "items": items,
        "unlocked_count": int(payload.get("unlocked_count") or len(unlocked)),
        "total_count": int(payload.get("total_count") or len(raw_items)),
        "generated_at": payload.get("generated_at"),
    }
