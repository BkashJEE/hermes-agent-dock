"""Local backend for the Hermes Agent Dock desktop plugin.

The API is mounted at /api/plugins/hermes-agent-dock by Hermes.  It discovers
real Hermes profiles, launches direct profile-scoped CLI sessions without a
shell, and exposes a privacy-reduced view of the optional achievements plugin.
"""
from __future__ import annotations

import base64
import binascii
import importlib.util
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

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, StrictBool, field_validator

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

_CONTROL_SPEC = importlib.util.spec_from_file_location(
    "hermes_agent_dock_control_store",
    Path(__file__).resolve().with_name("control_store.py"),
)
if _CONTROL_SPEC is None or _CONTROL_SPEC.loader is None:  # pragma: no cover - installation corruption
    raise RuntimeError("Agent Dock control store is unavailable")
_CONTROL_MODULE = importlib.util.module_from_spec(_CONTROL_SPEC)
_CONTROL_SPEC.loader.exec_module(_CONTROL_MODULE)
ControlStore = _CONTROL_MODULE.ControlStore
ControlValidationError = _CONTROL_MODULE.ValidationError
BindingError = _CONTROL_MODULE.BindingError
ConflictError = _CONTROL_MODULE.ConflictError
ConfirmationRequired = _CONTROL_MODULE.ConfirmationRequired
TransitionError = _CONTROL_MODULE.TransitionError
LeaseError = _CONTROL_MODULE.LeaseError

_JOB_SPEC = importlib.util.spec_from_file_location(
    "hermes_agent_dock_job_store",
    Path(__file__).resolve().with_name("job_store.py"),
)
if _JOB_SPEC is None or _JOB_SPEC.loader is None:  # pragma: no cover - installation corruption
    raise RuntimeError("Agent Dock job store is unavailable")
_JOB_MODULE = importlib.util.module_from_spec(_JOB_SPEC)
_JOB_SPEC.loader.exec_module(_JOB_MODULE)
JobStore = _JOB_MODULE.JobStore
_SUBAGENT_SPEC = importlib.util.spec_from_file_location(
    "hermes_agent_dock_subagent_progress",
    Path(__file__).resolve().with_name("subagent_progress.py"),
)
if _SUBAGENT_SPEC is None or _SUBAGENT_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("Agent Dock subagent progress support is unavailable")
_SUBAGENT_MODULE = importlib.util.module_from_spec(_SUBAGENT_SPEC)
_SUBAGENT_SPEC.loader.exec_module(_SUBAGENT_MODULE)
merge_subagent_entries = _SUBAGENT_MODULE.merge_subagent_entries
public_subagent_record = _SUBAGENT_MODULE.public_subagent_record

router = APIRouter()

PLUGIN_VERSION = "0.4.0"
MAX_MESSAGE_CHARS = 12_000
MAX_RESPONSE_CHARS = 120_000
MAX_IMAGE_ATTACHMENTS = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 25 * 1024 * 1024
MAX_IMAGE_DATA_URL_CHARS = ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 128
MAX_CONCURRENT_JOBS = 4
JOB_TIMEOUT_SECONDS = 15 * 60
CATALOG_TIMEOUT_SECONDS = 60
CATALOG_CACHE_SECONDS = 15
JOB_RETENTION_SECONDS = 60 * 60
MAX_RETAINED_JOBS = 200
MAX_SUBAGENT_PROGRESS_BYTES = 2 * 1024 * 1024
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
_CONTROL_LOCK = threading.RLock()
_CONTROL_STORE: Any | None = None
_CONTROL_STORE_HOME: Path | None = None
_JOB_STORE_LOCK = threading.RLock()
_JOB_STORE: Any | None = None
_JOB_STORE_HOME: Path | None = None
_JOB_STORE_REHYDRATED = False


class ImageAttachment(BaseModel):
    name: str
    mime_type: str
    data_url: str

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Image name must be a string")
        name = Path(value.strip()).name[:180]
        if not name:
            raise ValueError("Image name is empty")
        return name

    @field_validator("mime_type", mode="before")
    @classmethod
    def normalize_mime_type(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Image MIME type must be a string")
        mime_type = value.strip().lower()
        if not mime_type.startswith("image/") or len(mime_type) > 80:
            raise ValueError("Upload payload must be an image")
        return mime_type

    @field_validator("data_url", mode="before")
    @classmethod
    def bound_data_url(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Image data must be a string")
        if len(value) > MAX_IMAGE_DATA_URL_CHARS:
            raise ValueError(f"Image exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MB cap")
        return value


class SendRequest(BaseModel):
    profile: str
    model: str | None = None
    provider: str | None = None
    message: str
    request_id: str | None = None
    session_id: str | None = None
    reasoning_effort: str | None = "none"
    fast: bool = False
    assign_task: StrictBool = False
    images: list[ImageAttachment] = Field(default_factory=list)

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
        return value.strip()

    @field_validator("images", mode="before")
    @classmethod
    def bound_image_count(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("Images must be a list")
        if len(value) > MAX_IMAGE_ATTACHMENTS:
            raise ValueError(f"At most {MAX_IMAGE_ATTACHMENTS} images can be attached")
        return value


# Dashboard plugins can be loaded through importlib without first being added
# to sys.modules. Resolve the postponed nested-model annotation explicitly so
# Pydantic behaves identically under the Desktop loader and the test harness.
SendRequest.model_rebuild(
    _types_namespace={"ImageAttachment": ImageAttachment, "StrictBool": StrictBool}
)


class AttachRunRequest(BaseModel):
    request_id: str | None = None
    profile: str
    runtime_profile: str
    runtime_session_id: str
    session_id: str
    title: str | None = None
    objective: str = ""
    permission_scope: str = "inherit-only"
    status: str = "working"
    subagent_id: str | None = None
    kanban_task_id: str | None = None


class RebindRunRequest(BaseModel):
    profile: str
    session_id: str
    old_runtime_profile: str
    old_runtime_session_id: str
    runtime_profile: str
    runtime_session_id: str
    permission_scope: str


class ControlMessageRequest(BaseModel):
    message_id: str
    run_id: str
    profile: str
    session_id: str
    kind: str
    body: str
    confirmed: StrictBool = False
    permission_scope: str = "inherit-only"

    @field_validator("confirmed", mode="before")
    @classmethod
    def require_confirmation_boolean(cls, value: Any) -> bool:
        if type(value) is not bool:
            raise ValueError("confirmed must be a JSON boolean")
        return value


class ClaimMessageRequest(BaseModel):
    dispatcher_id: str
    profile: str
    session_id: str
    runtime_profile: str
    runtime_session_id: str
    lease_seconds: float = Field(default=30, ge=1, le=300)


class ReceiptRequest(BaseModel):
    receipt_id: str
    state: str
    source: str
    verification: str
    profile: str
    session_id: str
    runtime_profile: str
    runtime_session_id: str
    dispatch_token: str | None = None
    source_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("receipt_id")
    @classmethod
    def require_nonblank_receipt_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("receipt_id must be nonblank")
        return normalized


class ObserveRunRequest(BaseModel):
    profile: str
    session_id: str
    status: str
    heartbeat_at: float | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


ControlMessageRequest.model_rebuild(_types_namespace={"StrictBool": StrictBool})
ReceiptRequest.model_rebuild(_types_namespace={"Any": Any})
ObserveRunRequest.model_rebuild(_types_namespace={"Any": Any})


def _bounded_message(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("message must be a string")
    if len(value) > MAX_MESSAGE_CHARS:
        raise ValueError(f"Message exceeds {MAX_MESSAGE_CHARS} characters")
    normalized = value.strip()
    if not normalized:
        raise ValueError("Message is empty")
    return normalized


class AssignAfterRequest(BaseModel):
    """Assign a finished job to the Kanban board without re-running it."""

    message: str

    @field_validator("message", mode="before")
    @classmethod
    def normalize_bounded_message(cls, value: Any) -> str:
        return _bounded_message(value)


class RetryRequest(BaseModel):
    """Re-run a terminal job with a fresh attempt under the same identity."""

    message: str
    model: str | None = None
    provider: str | None = None
    session_id: str | None = None
    reasoning_effort: str | None = "none"
    fast: bool = False
    images: list[ImageAttachment] = Field(default_factory=list)

    @field_validator("message", mode="before")
    @classmethod
    def normalize_bounded_message(cls, value: Any) -> str:
        return _bounded_message(value)

    @field_validator("images", mode="before")
    @classmethod
    def bound_image_count(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("Images must be a list")
        if len(value) > MAX_IMAGE_ATTACHMENTS:
            raise ValueError(f"At most {MAX_IMAGE_ATTACHMENTS} images can be attached")
        return value


AssignAfterRequest.model_rebuild(_types_namespace={"Any": Any})
RetryRequest.model_rebuild(
    _types_namespace={"ImageAttachment": ImageAttachment, "Any": Any}
)


def _control_store() -> Any:
    global _CONTROL_STORE, _CONTROL_STORE_HOME
    home = Path(get_hermes_home()).resolve()
    with _CONTROL_LOCK:
        if _CONTROL_STORE is None or _CONTROL_STORE_HOME != home:
            if _CONTROL_STORE is not None:
                _CONTROL_STORE.close()
            _CONTROL_STORE = ControlStore(hermes_home=home)
            _CONTROL_STORE_HOME = home
        return _CONTROL_STORE


def _job_key(profile_id: str, request_id: str | None) -> str | None:
    if request_id is None:
        return None
    return f"{profile_id}:{request_id}"


def _memory_job_from_row(row: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    response = None
    if existing and existing.get("_attempt_token") == row.get("attempt_token"):
        response = existing.get("response")
    job = {
        "id": row["job_id"],
        "profile": row["profile_id"],
        "model": row.get("model"),
        "provider": row.get("provider"),
        "reasoning_effort": row.get("reasoning_effort") or "none",
        "fast": bool(row.get("fast")),
        "request_id": row.get("request_id"),
        # `starting` is the durable pre-worker reservation state. The public
        # API retains its established `queued` presentation until the worker
        # wins the exact-attempt transition to `running`.
        "status": "queued" if row.get("status") == "starting" else row.get("status"),
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "session_id": row.get("session_id"),
        "response": response,
        "error": row.get("error_summary"),
        "kanban_task_id": row.get("kanban_task_id"),
        "kanban_board": row.get("kanban_board"),
        "kanban_error": existing.get("kanban_error") if existing else None,
        "image_count": int(row.get("image_count") or 0),
        "_attempt_token": row.get("attempt_token"),
    }
    progress_path = (
        _profile_home(row["profile_id"]).resolve()
        / "cache"
        / "agent-dock-progress"
        / f"{row['job_id']}.jsonl"
    )
    if progress_path.is_file():
        job.update(
            {
                "subagents": list(existing.get("subagents") or []) if existing else [],
                "_subagent_progress_path": progress_path,
                "_subagent_started_ids": set(existing.get("_subagent_started_ids") or ()) if existing else set(),
            }
        )
        _refresh_subagents(job)
        if job["status"] in {"interrupted", "cancelled"}:
            for child in job.get("subagents") or []:
                if child.get("status") == "running":
                    child.update(
                        {
                            "status": "interrupted",
                            "finished_at": row.get("finished_at") or child.get("updated_at"),
                            "current_tool": None,
                        }
                    )
    return job


def _rehydrate_jobs(store: Any) -> None:
    rows = store.list_jobs()
    with _JOBS_LOCK:
        for row in rows:
            existing = _JOBS.get(row["job_id"])
            _JOBS[row["job_id"]] = _memory_job_from_row(row, existing)
            request_key = _job_key(row["profile_id"], row.get("request_id"))
            if request_key:
                _REQUEST_JOBS[request_key] = row["job_id"]


def _job_store() -> Any:
    global _JOB_STORE, _JOB_STORE_HOME, _JOB_STORE_REHYDRATED
    home = Path(get_hermes_home()).resolve()
    with _JOB_STORE_LOCK:
        if _JOB_STORE is None or _JOB_STORE_HOME != home:
            if _JOB_STORE is not None:
                _JOB_STORE.close()
            _JOB_STORE = JobStore(hermes_home=home)
            _JOB_STORE_HOME = home
            _JOB_STORE_REHYDRATED = False
        if not _JOB_STORE_REHYDRATED:
            _rehydrate_jobs(_JOB_STORE)
            _JOB_STORE_REHYDRATED = True
        return _JOB_STORE


def _reset_job_store_for_tests(*, clear_memory: bool = True) -> None:
    """Reset the lazy ledger without touching the live Hermes home."""
    global _JOB_STORE, _JOB_STORE_HOME, _JOB_STORE_REHYDRATED
    with _JOB_STORE_LOCK:
        if _JOB_STORE is not None:
            _JOB_STORE.close()
        _JOB_STORE = None
        _JOB_STORE_HOME = None
        _JOB_STORE_REHYDRATED = False
    if clear_memory:
        with _JOBS_LOCK:
            _JOBS.clear()
            _REQUEST_JOBS.clear()


def _load_durable_job(job_id: str) -> dict[str, Any] | None:
    row = _job_store().get_job(job_id)
    if row is None:
        return None
    with _JOBS_LOCK:
        existing = _JOBS.get(job_id)
        job = _memory_job_from_row(row, existing)
        _JOBS[job_id] = job
        request_key = _job_key(row["profile_id"], row.get("request_id"))
        if request_key:
            _REQUEST_JOBS[request_key] = job_id
        return job


def _control_error(exc: Exception) -> HTTPException:
    if isinstance(exc, BindingError):
        return HTTPException(status_code=404, detail="Control run or message was not found for this binding")
    if isinstance(exc, ConfirmationRequired):
        return HTTPException(status_code=409, detail="Explicit confirmation is required")
    if isinstance(exc, (ConflictError, TransitionError, LeaseError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ControlValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Agent Dock control store failed safely")


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


_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


def _image_extension(data: bytes) -> str | None:
    head = data[:16]
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    for signature, extension in _IMAGE_MAGIC:
        if head.startswith(signature):
            return extension
    return None


def _decode_image_attachment(attachment: ImageAttachment) -> tuple[bytes, str]:
    header, separator, encoded = attachment.data_url.partition(",")
    if not separator or not header.lower().startswith("data:image/") or ";base64" not in header.lower():
        raise ValueError("Image data must be a base64 data URL")
    header_mime = header[5:].split(";", 1)[0].strip().lower()
    if header_mime != attachment.mime_type:
        raise ValueError("Image MIME type does not match its data URL")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Image data is not valid base64") from exc
    if not data:
        raise ValueError("Image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MB cap")
    extension = _image_extension(data)
    if extension is None:
        raise ValueError("Unsupported image type")
    return data, extension


def _materialize_job_images(profile: str, attachments: list[ImageAttachment], job_id: str) -> tuple[list[Path], Path | None]:
    if not attachments:
        return [], None
    decoded = [_decode_image_attachment(attachment) for attachment in attachments]
    if sum(len(data) for data, _ in decoded) > MAX_IMAGE_TOTAL_BYTES:
        raise ValueError(f"Attached images exceed the {MAX_IMAGE_TOTAL_BYTES // (1024 * 1024)} MB total cap")

    root = _profile_home(profile).resolve() / "images" / "agent-dock"
    job_dir = root / job_id
    paths: list[Path] = []
    try:
        job_dir.mkdir(parents=True, exist_ok=False)
        for index, (data, extension) in enumerate(decoded, start=1):
            path = job_dir / f"image-{index}{extension}"
            with path.open("xb") as handle:
                handle.write(data)
            paths.append(path)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    return paths, job_dir


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


def _create_kanban_task_for_job(
    *,
    profile_id: str,
    model: str | None,
    provider: str | None,
    reasoning_effort: str,
    session_id: str | None,
    job_id: str,
    title: str,
    body: str,
    idempotency_key: str,
    initial_status: str,
) -> str:
    """Create a Kanban card on the shared board for an existing durable job.

    Centralized so create-time assignment and assign-after produce the same
    card shape. The idempotency key is the caller's responsibility; it is what
    makes a repeat of the same logical assignment a no-op.
    """
    kb = _kanban_module()
    conn = kb.connect(board=KANBAN_BOARD)
    try:
        return kb.create_task(
            conn,
            title=title,
            body=body,
            assignee=profile_id,
            created_by="default",
            workspace_kind="dir",
            workspace_path=str(_kanban_workspace()),
            priority=50,
            idempotency_key=idempotency_key,
            model_override=(model or "").strip() or None,
            provider_override=(provider or "").strip() or None,
            reasoning_effort=reasoning_effort or "none",
            initial_status=initial_status,
            session_id=session_id,
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
    detail = re.sub(
        r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?[^\s,;}]+",
        "authorization=[REDACTED]",
        detail,
    )
    detail = re.sub(
        r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;}]+",
        lambda match: f"{match.group(1)}=[REDACTED]",
        detail,
    )
    detail = re.sub(r"(?i)\b[A-Z]:\\[^\n,;}]+", "[PRIVATE_PATH]", detail)
    detail = re.sub(r"\\\\[^\n,;}]+", "[PRIVATE_PATH]", detail)
    detail = re.sub(
        r"(?i)(?:/home/|/Users/|/tmp/|/var/|/etc/|/opt/|/srv/)[^\n,;}]+",
        "[PRIVATE_PATH]",
        detail,
    )
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
                job["kanban_error"] = "Kanban update failed; inspect local Hermes logs"


def _sync_control_to_orchestrator(run_id: str, message_id: str, kind: str, state: str) -> dict[str, str]:
    """Write a privacy-reduced intervention receipt to the bound Kanban task."""
    run = _control_store().get_run(run_id)
    task_id = run.get("kanban_task_id") if run else None
    if not task_id:
        return {"state": "unavailable", "reason": "No orchestrator task is bound to this run"}
    try:
        kb = _kanban_module()
        conn = kb.connect(board=KANBAN_BOARD)
        try:
            kb.add_comment(
                conn,
                task_id,
                "agent-dock",
                f"Agent Dock control receipt: {kind.upper()} {state}; message {message_id}; run {run_id}.",
            )
        finally:
            conn.close()
    except Exception:
        return {"state": "failed", "reason": "Orchestrator synchronization failed; inspect local logs"}
    return {"state": "observed", "task_id": str(task_id)}


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
    if not message and not request.images:
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
    # Whitelist the transport contract rather than merely stripping one known
    # private key.  This prevents attempt tokens, process handles, prompts,
    # paths, and future internal bookkeeping from leaking accidentally.
    public_keys = {
        "id",
        "profile",
        "model",
        "provider",
        "reasoning_effort",
        "fast",
        "request_id",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "session_id",
        "response",
        "error",
        "kanban_task_id",
        "kanban_board",
        "kanban_error",
        "image_count",
        "subagents",
    }
    return {key: value for key, value in job.items() if key in public_keys}


def _progress_root(profile: str) -> Path:
    root = _profile_home(profile).resolve() / "cache" / "agent-dock-progress"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _create_progress_file(profile: str, job_id: str) -> Path:
    path = _progress_root(profile) / f"{job_id}.jsonl"
    with path.open("x", encoding="utf-8"):
        pass
    return path


def _refresh_subagents(job: dict[str, Any]) -> None:
    path = job.get("_subagent_progress_path")
    if not isinstance(path, Path):
        return
    try:
        if path.stat().st_size > MAX_SUBAGENT_PROGRESS_BYTES:
            return
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    incoming: list[Any] = []
    for line in lines:
        if not line or len(line) > 4096:
            continue
        try:
            incoming.append(json.loads(line))
        except (TypeError, ValueError):
            continue
    merged = merge_subagent_entries(
        job.get("subagents") or [],
        incoming,
        expected_job_id=str(job.get("id") or ""),
        started_ids=job.setdefault("_subagent_started_ids", set()),
    )
    job["subagents"] = [public_subagent_record(record) for record in merged]


def _evict_completed_jobs(now: int | None = None) -> None:
    """Bound terminal-job memory while preserving a one-hour retry window."""
    current = int(time.time()) if now is None else now
    with _JOBS_LOCK:
        terminal = []
        remove: set[str] = set()
        for job_id, job in _JOBS.items():
            if job.get("status") not in {"done", "error", "cancelled", "interrupted"}:
                continue
            finished = int(job.get("finished_at") or job.get("created_at") or current)
            terminal.append((finished, job_id))
            if finished < current - JOB_RETENTION_SECONDS:
                remove.add(job_id)
        retained = sorted(item for item in terminal if item[1] not in remove)
        if len(retained) > MAX_RETAINED_JOBS:
            remove.update(job_id for _, job_id in retained[: len(retained) - MAX_RETAINED_JOBS])
        for job_id in remove:
            removed = _JOBS.pop(job_id, None)
            progress_path = removed.get("_subagent_progress_path") if removed else None
            if isinstance(progress_path, Path):
                progress_path.unlink(missing_ok=True)
        for request_key, job_id in list(_REQUEST_JOBS.items()):
            if job_id not in _JOBS:
                _REQUEST_JOBS.pop(request_key, None)


def _trim(value: str, limit: int = MAX_RESPONSE_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n\n[response truncated by Agent Dock]"


def _runner_payload(
    request: SendRequest,
    image_paths: list[Path] | None = None,
    *,
    job_id: str | None = None,
    subagent_progress_path: Path | None = None,
) -> str:
    payload = {
        "message": request.message,
        "model": (request.model or "").strip() or None,
        "provider": (request.provider or "").strip() or None,
        "session_id": request.session_id,
        "reasoning_effort": request.reasoning_effort or "none",
        "fast": bool(request.fast),
        "images": [str(path) for path in image_paths or []],
        "job_id": job_id,
        "subagent_progress_path": str(subagent_progress_path) if subagent_progress_path else None,
    }
    return json.dumps(payload, ensure_ascii=False)


def _begin_job_finalization(
    job_id: str,
    response: str,
    session_id: str | None,
    attempt_token: str | None = None,
) -> bool:
    """CAS the durable attempt before linked-card settlement."""
    with _JOBS_LOCK:
        memory_job = _JOBS.get(job_id)
        memory_token = memory_job.get("_attempt_token") if memory_job else None
    store = _job_store()
    durable = store.get_job(job_id)
    if durable is not None:
        token = attempt_token or memory_token or durable.get("attempt_token")
        if not token or not store.begin_finalization(job_id, token, session_id=session_id):
            return False
        with _JOBS_LOCK:
            job = _JOBS.get(job_id) or _memory_job_from_row(durable)
            job.update(
                {
                    "status": "finalizing",
                    "response": response,
                    "session_id": session_id,
                    "_attempt_token": token,
                }
            )
            _JOBS[job_id] = job
        return True

    # Keep direct unit-level worker probes that construct an in-memory job
    # compatible; production-created jobs always have a durable row.
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job or job.get("status") == "cancelled":
            return False
        job.update({"status": "finalizing", "response": response, "session_id": session_id})
        return True


def _publish_job_success(job_id: str, attempt_token: str | None = None) -> bool:
    """Expose terminal success only after the exact attempt wins its CAS."""
    with _JOBS_LOCK:
        memory_job = _JOBS.get(job_id)
        memory_token = memory_job.get("_attempt_token") if memory_job else None
    store = _job_store()
    durable = store.get_job(job_id)
    if durable is not None:
        token = attempt_token or memory_token or durable.get("attempt_token")
        if not token or not store.complete_done(job_id, token):
            return False
        with _JOBS_LOCK:
            job = _JOBS.get(job_id) or _memory_job_from_row(durable)
            job.update({"status": "done", "finished_at": int(time.time()), "_attempt_token": token})
            _JOBS[job_id] = job
        return True

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
    image_dir: Path | None = None
    store = _job_store()
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    durable_row = store.get_job(job_id)
    if job is None and durable_row is not None:
        job = _load_durable_job(job_id)
    if not job or durable_row is None:
        return

    attempt_token = job.get("_attempt_token")
    if not attempt_token or not store.mark_running(job_id, attempt_token):
        return
    with _JOBS_LOCK:
        current = _JOBS.get(job_id)
        if current:
            current.update({"status": "running", "started_at": int(time.time())})

    def attempt_is_running() -> bool:
        row = store.get_job(job_id)
        return bool(
            row
            and row.get("attempt_token") == attempt_token
            and row.get("status") == "running"
        )

    try:
        command = _build_command(request)
        if not attempt_is_running():
            return
        image_paths, image_dir = _materialize_job_images(request.profile, request.images, job_id)
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
        if not attempt_is_running():
            return
        process = subprocess.Popen(**popen_kwargs)
        attached = False
        with _JOBS_LOCK:
            current = _JOBS.get(job_id)
            if current and current.get("status") not in {"cancelled", "cancelling", "interrupted"}:
                current["_process"] = process
                attached = True
        if not attempt_is_running():
            attached = False
        if not attached:
            _terminate_process(process)
            return

        try:
            stdout, stderr = process.communicate(
                input=_runner_payload(
                    request,
                    image_paths,
                    job_id=job_id,
                    subagent_progress_path=job.get("_subagent_progress_path") if job else None,
                ),
                timeout=JOB_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            _terminate_process(process)
            raise RuntimeError("Agent session exceeded the 15 minute timeout")

        if not attempt_is_running():
            return

        match = SESSION_LINE_RE.search(stderr or "")
        session_id = match.group(1) if match and SESSION_ID_RE.fullmatch(match.group(1)) else request.session_id
        if process.returncode != 0:
            raise RuntimeError(_safe_runner_error(stderr or "", process.returncode))

        response = _trim((stdout or "").strip())
        if not _begin_job_finalization(job_id, response, session_id, attempt_token):
            return

        if not store.publish(job_id, attempt_token):
            return
        with _JOBS_LOCK:
            current = _JOBS.get(job_id)
            if current:
                current.update({"status": "done", "finished_at": int(time.time())})
        _settle_job_kanban(job_id, "done", response or "Agent returned no response text")
    except Exception as exc:
        message = f"Image attachment rejected: {exc}" if request.images and isinstance(exc, ValueError) else str(exc)
        if (
            "Agent session failed:" not in message
            and "Image attachment rejected:" not in message
            and "timeout" not in message.lower()
        ):
            message = "Agent session could not start. Check the selected profile and Hermes logs."

        won_error = store.complete_error(job_id, attempt_token, message)
        if won_error:
            with _JOBS_LOCK:
                current = _JOBS.get(job_id)
                if current:
                    current.update(
                        {
                            "status": "error",
                            "error": message[:500],
                            "finished_at": int(time.time()),
                        }
                    )
            _settle_job_kanban(job_id, "error", message)
    finally:
        if image_dir is not None:
            shutil.rmtree(image_dir, ignore_errors=True)
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job:
                _refresh_subagents(job)
                job.pop("_process", None)


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "hermes-agent-dock", "version": PLUGIN_VERSION}


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
            "model_override": False,
            "reasoning": True,
            "fast": True,
            "model_catalog": True,
            "image_upload": True,
            "max_images": MAX_IMAGE_ATTACHMENTS,
            "max_image_bytes": MAX_IMAGE_BYTES,
            "kanban_assignment": True,
            "kanban_board": KANBAN_BOARD,
            "active_run_attachment": True,
            "durable_control_queue": True,
            "interventions": ["ask", "nudge", "redirect"],
            "pause_resume": False,
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


def _known_control_profile(profile: str) -> str:
    try:
        return _normalize_profile(profile)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/control/runs")
def control_runs(profile: str) -> dict[str, Any]:
    normalized = _known_control_profile(profile)
    try:
        rows = _control_store().list_runs(profile=normalized)
    except Exception as exc:
        raise _control_error(exc) from exc
    return {"runs": rows, "count": len(rows), "durable": True}


@router.post("/control/runs")
def attach_control_run(request: AttachRunRequest) -> dict[str, Any]:
    try:
        normalized_profile = _normalize_profile(request.profile)
        normalized_runtime_profile = _normalize_profile(request.runtime_profile)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if normalized_profile != normalized_runtime_profile:
        raise HTTPException(status_code=409, detail="Selected profile does not own this Desktop runtime")
    try:
        return _control_store().attach_run(
            profile=normalized_profile,
            runtime_profile=normalized_runtime_profile,
            session_id=request.session_id,
            runtime_session_id=request.runtime_session_id,
            subagent_id=request.subagent_id,
            kanban_task_id=request.kanban_task_id,
            source="desktop-session",
            title=request.title,
            objective=request.objective,
            permission_scope=request.permission_scope,
            status=request.status,
        )
    except Exception as exc:
        raise _control_error(exc) from exc


@router.post("/control/runs/{run_id}/rebind")
def rebind_control_run(run_id: str, request: RebindRunRequest) -> dict[str, Any]:
    selected_profile = _known_control_profile(request.profile)
    try:
        old_runtime_profile = _normalize_profile(request.old_runtime_profile)
        runtime_profile = _normalize_profile(request.runtime_profile)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if selected_profile != old_runtime_profile or selected_profile != runtime_profile:
        raise HTTPException(status_code=409, detail="Selected profile does not own this Desktop runtime")
    try:
        return _control_store().rebind_run_runtime(
            run_id=run_id,
            profile=selected_profile,
            session_id=request.session_id,
            old_runtime_profile=old_runtime_profile,
            old_runtime_session_id=request.old_runtime_session_id,
            runtime_profile=runtime_profile,
            runtime_session_id=request.runtime_session_id,
            permission_scope=request.permission_scope,
        )
    except Exception as exc:
        raise _control_error(exc) from exc


@router.get("/control/runs/{run_id}")
def control_run(run_id: str, profile: str, session_id: str) -> dict[str, Any]:
    normalized = _known_control_profile(profile)
    try:
        row = _control_store().get_run(run_id, profile=normalized, session_id=session_id)
    except Exception as exc:
        raise _control_error(exc) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Control run was not found")
    return row


@router.post("/control/runs/{run_id}/observations")
def observe_control_run(run_id: str, request: ObserveRunRequest) -> dict[str, Any]:
    normalized = _known_control_profile(request.profile)
    try:
        return _control_store().observe_run(
            run_id,
            profile=normalized,
            session_id=request.session_id,
            status=request.status,
            heartbeat_at=request.heartbeat_at,
            detail=request.detail,
        )
    except Exception as exc:
        raise _control_error(exc) from exc


@router.post("/control/messages", status_code=202)
def enqueue_control_message(request: ControlMessageRequest) -> dict[str, Any]:
    normalized = _known_control_profile(request.profile)
    try:
        binding = _control_store().get_run(
            request.run_id, profile=normalized, session_id=request.session_id
        )
        if binding is None:
            raise BindingError("run not found")
        row = _control_store().enqueue_message(
            message_id=request.message_id,
            run_id=request.run_id,
            kind=request.kind,
            body=request.body,
            confirmed=request.confirmed,
            permission_scope=request.permission_scope,
        )
        return {
            **row,
            "orchestrator_sync": _sync_control_to_orchestrator(
                request.run_id, request.message_id, request.kind, row["state"]
            ),
        }
    except Exception as exc:
        raise _control_error(exc) from exc


@router.post("/control/messages/{message_id}/claim")
def claim_control_message(message_id: str, request: ClaimMessageRequest) -> dict[str, Any]:
    normalized = _known_control_profile(request.profile)
    runtime_profile = _known_control_profile(request.runtime_profile)
    if runtime_profile != normalized:
        raise HTTPException(status_code=409, detail="Selected profile does not own this Desktop runtime")
    try:
        message = _control_store().get_message(message_id)
        if message is None:
            raise BindingError("message not found")
        binding = _control_store().get_run(
            message["run_id"], profile=normalized, session_id=request.session_id
        )
        if binding is None:
            raise BindingError("run not found")
        return _control_store().claim_message(
            message_id,
            runtime_profile=runtime_profile,
            runtime_session_id=request.runtime_session_id,
            dispatcher_id=request.dispatcher_id,
            lease_seconds=request.lease_seconds,
        )
    except Exception as exc:
        raise _control_error(exc) from exc


@router.post("/control/messages/{message_id}/receipts")
def record_control_receipt(message_id: str, request: ReceiptRequest) -> dict[str, Any]:
    normalized = _known_control_profile(request.profile)
    runtime_profile = _known_control_profile(request.runtime_profile)
    if runtime_profile != normalized:
        raise HTTPException(status_code=409, detail="Selected profile does not own this Desktop runtime")
    try:
        message = _control_store().get_message(message_id)
        if message is None:
            raise BindingError("message not found")
        binding = _control_store().get_run(
            message["run_id"], profile=normalized, session_id=request.session_id
        )
        if binding is None:
            raise BindingError("run not found")
        row = _control_store().record_receipt(
            message_id=message_id,
            runtime_profile=runtime_profile,
            runtime_session_id=request.runtime_session_id,
            receipt_id=request.receipt_id,
            state=request.state,
            verification=request.verification,
            source=request.source,
            source_id=request.source_id,
            dispatch_token=request.dispatch_token,
            detail=request.detail,
        )
        message = _control_store().get_message(message_id)
        sync = (
            _sync_control_to_orchestrator(
                message["run_id"], message_id, message["kind"], row["message_state"]
            )
            if message
            else {"state": "unavailable", "reason": "Message binding unavailable"}
        )
        return {**row, "orchestrator_sync": sync}
    except Exception as exc:
        raise _control_error(exc) from exc


@router.get("/control/events")
def control_events(
    run_id: str,
    profile: str,
    session_id: str,
    message_id: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    normalized = _known_control_profile(profile)
    try:
        binding = _control_store().get_run(run_id, profile=normalized, session_id=session_id)
        if binding is None:
            raise BindingError("run not found")
        rows = _control_store().search_events(
            run_id=run_id,
            message_id=message_id,
            kind=kind,
            q=q,
            limit=limit,
        )
    except Exception as exc:
        raise _control_error(exc) from exc
    return {"events": rows, "count": len(rows)}


@router.post("/jobs", status_code=202)
def create_job(request: SendRequest) -> dict[str, Any]:
    _evict_completed_jobs()
    request_id = (request.request_id or "").strip()
    if request.assign_task and not request_id:
        raise HTTPException(status_code=400, detail="request_id is required for Kanban assignment")
    profile_id = request.profile.strip().lower()
    request_key = _job_key(profile_id, request_id or None)
    store = _job_store()

    # A transport retry must resolve from the reservation ledger even when
    # provider discovery is temporarily unavailable. Re-check after validation
    # as well so concurrent first submissions still reserve exactly one job.
    if request_key:
        existing_row = store.get_by_request(profile_id, request_id)
        if existing_row is not None:
            if existing_row.get("assign_task") and not existing_row.get("kanban_task_id"):
                # The winner performs external assignment after committing the
                # reservation. Concurrent duplicates wait without holding a
                # SQLite write transaction or repeating the side effect.
                for _ in range(100):
                    if existing_row.get("status") not in {"starting", "queued"}:
                        break
                    time.sleep(0.05)
                    refreshed = store.get_job(existing_row["job_id"])
                    if refreshed is None:
                        break
                    existing_row = refreshed
                    if existing_row.get("kanban_task_id"):
                        break
            existing = _load_durable_job(existing_row["job_id"])
            if existing is not None:
                return _public_job(existing)

    try:
        _build_command(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    with _JOBS_LOCK:
        active = sum(
            1
            for job in _JOBS.values()
            if job["status"] in {"starting", "queued", "running", "finalizing", "cancelling"}
        )
        if active >= MAX_CONCURRENT_JOBS:
            raise HTTPException(status_code=429, detail="Agent Dock already has four active sessions")

    provisional_job_id = uuid.uuid4().hex
    try:
        row, created = store.reserve_job(
            job_id=provisional_job_id,
            profile_id=profile_id,
            request_id=request_id or None,
            provider=(request.provider or "").strip() or None,
            model=(request.model or "").strip() or None,
            reasoning_effort=request.reasoning_effort or "none",
            fast=bool(request.fast),
            assign_task=bool(request.assign_task),
            image_count=len(request.images),
            session_id=request.session_id,
            kanban_task_id=None,
            kanban_board=None,
        )
    except (PermissionError, RuntimeError, ValueError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent Dock job reservation failed; inspect local Hermes logs",
        ) from exc
    if not created and row.get("assign_task") and not row.get("kanban_task_id"):
        for _ in range(100):
            if row.get("status") not in {"starting", "queued"}:
                break
            time.sleep(0.05)
            refreshed = store.get_job(row["job_id"])
            if refreshed is None:
                break
            row = refreshed
            if row.get("kanban_task_id"):
                break
    if created and request.assign_task:
        try:
            task_id = _create_kanban_task(request, row["job_id"])
            if task_id and not store.update_kanban(
                row["job_id"],
                row["attempt_token"],
                kanban_task_id=task_id,
                kanban_board=KANBAN_BOARD,
            ):
                raise RuntimeError("Durable job authority changed during Kanban assignment")
            row = store.get_job(row["job_id"]) or row
        except (PermissionError, RuntimeError, ValueError, OSError) as exc:
            store.complete_error(
                row["job_id"],
                row["attempt_token"],
                "Kanban assignment failed; inspect local Hermes logs",
            )
            raise HTTPException(
                status_code=503,
                detail="Kanban assignment failed; inspect local Hermes logs",
            ) from exc
    job_id = row["job_id"]
    if created:
        try:
            progress_path = _create_progress_file(request.profile, job_id)
        except OSError as exc:
            store.complete_error(
                row["job_id"],
                row["attempt_token"],
                "Subagent progress store is unavailable",
            )
            raise HTTPException(status_code=503, detail="Subagent progress store is unavailable") from exc
    else:
        progress_path = None
    with _JOBS_LOCK:
        existing = _JOBS.get(job_id)
        job = _memory_job_from_row(row, existing)
        if created:
            job.update({
                "subagents": [],
                "_subagent_progress_path": progress_path,
                "_subagent_started_ids": set(),
            })
        _JOBS[job_id] = job
        if request_key:
            _REQUEST_JOBS[request_key] = job_id

    if created:
        threading.Thread(
            target=_run_job,
            args=(job_id, request),
            name=f"agent-dock-{job_id[:8]}",
            daemon=True,
        ).start()
    return _public_job(job)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        job = _load_durable_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    with _JOBS_LOCK:
        _refresh_subagents(job)
    return _public_job(job)


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str) -> dict[str, Any]:
    store = _job_store()
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        job = _load_durable_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] in {"done", "error", "cancelled", "interrupted", "finalizing"}:
        return _public_job(job)

    token = job.get("_attempt_token")
    if not token or not store.request_cancel(job_id, token):
        refreshed = _load_durable_job(job_id)
        if refreshed is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return _public_job(refreshed)
    with _JOBS_LOCK:
        current = _JOBS.get(job_id) or job
        process = current.get("_process")
        current["status"] = "cancelling"
        _JOBS[job_id] = current
    _terminate_process(process)
    if store.complete_cancelled(job_id, token):
        with _JOBS_LOCK:
            current = _JOBS.get(job_id) or job
            current.update({"status": "cancelled", "finished_at": int(time.time()), "error": None})
            _JOBS[job_id] = current
        _settle_job_kanban(job_id, "cancelled", "Cancelled by Dad from Agent Dock")
    return _public_job(_load_durable_job(job_id) or current)


TERMINAL_JOB_STATUSES = frozenset({"done", "error", "cancelled", "interrupted"})
ASSIGNABLE_JOB_STATUSES = frozenset({"done", "error", "cancelled", "interrupted"})


@router.get("/jobs")
async def list_jobs_route(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    """List durable jobs, newest first, bounded and privacy-reduced.

    The ledger never stores prompts or responses; the list therefore
    exposes only identity, profile, status, and lifecycle timestamps.
    """
    _evict_completed_jobs()
    store = _job_store()
    rows = store.list_jobs(limit=limit)
    jobs = []
    for row in rows:
        job_id = row["job_id"]
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            job = _load_durable_job(job_id)
            if job is None:
                continue
        jobs.append(_public_job(job))
    return {"jobs": jobs, "count": len(jobs)}


@router.post("/jobs/{job_id}/assign", status_code=201)
async def assign_job(job_id: str, request: AssignAfterRequest) -> dict[str, Any]:
    """Assign a finished job to the shared Kanban board without re-running it.

    The durable ledger intentionally stores no prompt, so the caller supplies
    the task text for the card. Repeating the same logical assignment is a
    no-op: the idempotency key is the stable job ID, and the existing card is
    returned instead of creating a duplicate.
    """
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    store = _job_store()
    row = store.get_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if row.get("kanban_task_id"):
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            job = _load_durable_job(job_id)
        if job is not None:
            return _public_job(job)

    status = row.get("status")
    if status not in ASSIGNABLE_JOB_STATUSES:
        if status in {"starting", "queued", "running", "finalizing"}:
            raise HTTPException(status_code=409, detail="Job is still active; assign after it finishes")
        raise HTTPException(status_code=409, detail="Job cannot be assigned in this state")

    profile_id = row["profile_id"]
    try:
        _normalize_profile(profile_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Job profile is no longer valid") from None

    title = _task_title(request.message)
    body = (
        "Assigned after completion through Hermes Agent Dock.\n\n"
        f"Accountable profile: {profile_id}\n"
        f"Agent Dock job: {job_id}\n"
        f"Outcome: {status}\n\n"
        f"{request.message.strip()}"
    )
    try:
        task_id = _create_kanban_task_for_job(
            profile_id=profile_id,
            model=row.get("model"),
            provider=row.get("provider"),
            reasoning_effort=row.get("reasoning_effort") or "none",
            session_id=row.get("session_id"),
            job_id=job_id,
            title=title,
            body=body,
            idempotency_key=f"agent-dock:assign-after:{profile_id}:{job_id}",
            initial_status="ready",
        )
    except (PermissionError, RuntimeError, ValueError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Kanban assignment failed; inspect local Hermes logs",
        ) from exc

    if not store.link_kanban_terminal(
        job_id,
        kanban_task_id=task_id,
        kanban_board=KANBAN_BOARD,
        allow_statuses=ASSIGNABLE_JOB_STATUSES,
    ):
        # The card was created but the job left an assignable state mid-flight.
        # Leave the card for the operator; never invent a settlement.
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is not None:
                job["kanban_error"] = "Job state changed during assignment"
        raise HTTPException(status_code=409, detail="Job state changed during assignment")

    refreshed = _load_durable_job(job_id)
    if refreshed is None:
        raise HTTPException(status_code=404, detail="Job not found")
    with _JOBS_LOCK:
        current = _JOBS.get(job_id) or refreshed
        current.update({"kanban_task_id": task_id, "kanban_board": KANBAN_BOARD})
        _JOBS[job_id] = current
    return _public_job(current)


def _retry_send_request(job: dict[str, Any], request: RetryRequest) -> SendRequest:
    profile = job["profile"]
    message = request.message
    provider = (request.provider or "").strip() or None
    model = (request.model or "").strip() or None
    effort = request.reasoning_effort or "none"
    session_id = request.session_id
    if session_id and not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("Invalid Hermes session ID")
    return SendRequest(
        profile=profile,
        model=model,
        provider=provider,
        message=message,
        request_id=job.get("request_id"),
        session_id=session_id,
        reasoning_effort=effort,
        fast=bool(request.fast),
        assign_task=False,
        images=list(request.images),
    )


@router.post("/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, request: RetryRequest) -> dict[str, Any]:
    """Re-run a terminal job under its stable identity with a fresh attempt.

    The durable ledger stores no prompt, so the caller re-supplies the task
    text. The job ID, profile, and request ID are preserved — a retry is the
    same logical job run again, not a new job. Automatic recovery (the
    dispatcher re-queuing a failed task) uses the prior attempt's exact
    parameters; explicit user retries may pass overrides, which are
    re-validated against the profile catalog.
    """
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    _evict_completed_jobs()
    store = _job_store()
    row = store.get_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if row.get("status") not in TERMINAL_JOB_STATUSES:
        if row.get("status") in {"starting", "queued", "running", "finalizing"}:
            raise HTTPException(status_code=409, detail="Job is still active; retry after it finishes")
        raise HTTPException(status_code=409, detail="Job cannot be retried in this state")

    profile_id = row["profile_id"]
    try:
        _normalize_profile(profile_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Job profile is no longer valid") from None

    try:
        send_request = _retry_send_request(
            {
                "profile": profile_id,
                "request_id": row.get("request_id"),
            },
            request,
        )
        _build_command(send_request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    with _JOBS_LOCK:
        active = sum(
            1
            for job in _JOBS.values()
            if job["status"] in {"starting", "queued", "running", "finalizing", "cancelling"}
        )
        if active >= MAX_CONCURRENT_JOBS:
            raise HTTPException(status_code=429, detail="Agent Dock already has four active sessions")

    fresh_row = store.reset_attempt(job_id, allow_statuses=TERMINAL_JOB_STATUSES)
    if fresh_row is None:
        refreshed = _load_durable_job(job_id)
        if refreshed is None:
            raise HTTPException(status_code=404, detail="Job not found")
        raise HTTPException(status_code=409, detail="Job state changed; retry again")

    try:
        _create_progress_file(profile_id, job_id)
    except FileExistsError:
        pass  # prior attempt's file; runner appends per-attempt entries
    except OSError as exc:
        store.complete_error(job_id, fresh_row["attempt_token"], "Subagent progress store is unavailable")
        raise HTTPException(status_code=503, detail="Subagent progress store is unavailable") from exc

    with _JOBS_LOCK:
        existing = _JOBS.get(job_id)
        job = _memory_job_from_row(fresh_row, existing)
        job.update({"subagents": [], "_subagent_progress_path": _progress_root(profile_id) / f"{job_id}.jsonl", "_subagent_started_ids": set()})
        _JOBS[job_id] = job
        if row.get("request_id"):
            _REQUEST_JOBS[_job_key(profile_id, row["request_id"])] = job_id

    threading.Thread(
        target=_run_job,
        args=(job_id, send_request),
        name=f"agent-dock-{job_id[:8]}",
        daemon=True,
    ).start()
    return _public_job(_JOBS[job_id])


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
