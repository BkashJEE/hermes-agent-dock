"""Durable, profile-local control ledger for Hermes Agent Dock.

The store owns identity bindings, queued operator interventions, leases, receipts,
and privacy-reduced events. It never executes a Hermes action; the Desktop gateway
remains authoritative for safe-checkpoint delivery and permissions.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    from hermes_constants import get_hermes_home
except ImportError:  # Standalone repository tests.
    def get_hermes_home() -> Path:
        return Path.home() / ".hermes"

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5_000
MAX_MESSAGE_CHARS = 20_000
MAX_DETAIL_CHARS = 2_048
MAX_OBJECTIVE_CHARS = 1_000
VALID_MESSAGE_KINDS = frozenset({"ask", "nudge", "redirect", "stop"})
CONFIRMED_MESSAGE_KINDS = frozenset({"redirect", "stop"})
VALID_RUN_STATUSES = frozenset({"starting", "idle", "working", "waiting", "stopped", "completed", "failed", "unavailable"})
VALID_RECEIPT_STAGES = frozenset({"accepted", "delivered", "applied", "rejected", "failed", "superseded", "unknown"})
VALID_VERIFICATION_STATES = frozenset({"unverified", "reported", "documented", "observed", "failed"})
TERMINAL_MESSAGE_STATES = frozenset({"applied", "rejected", "failed", "superseded", "unknown"})
TERMINAL_RUN_STATUSES = frozenset({"stopped", "completed", "failed"})

_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*[^\s,;}]+"
)
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\(?:[^\s\\]+\\)*[^\s,;}]+")
_POSIX_PRIVATE_PATH_RE = re.compile(r"(?i)(?:/home/|/Users/)[^\s,;}]+")


class ControlStoreError(RuntimeError):
    pass


class ValidationError(ControlStoreError):
    pass


class BindingError(ControlStoreError):
    pass


class ConflictError(ControlStoreError):
    pass


class ConfirmationRequired(ControlStoreError):
    pass


class TransitionError(ControlStoreError):
    pass


class LeaseError(ControlStoreError):
    pass


def _now(value: float | int | None = None) -> float:
    return float(time.time() if value is None else value)


def _clean_identifier(value: Any, field: str, *, required: bool = False, limit: int = 240) -> str | None:
    if value is None:
        if required:
            raise ValidationError(f"{field} is required")
        return None
    text = str(value).strip()
    if required and not text:
        raise ValidationError(f"{field} is required")
    if not text:
        return None
    if len(text) > limit or any(ord(char) < 32 for char in text):
        raise ValidationError(f"{field} is invalid")
    return text


def _redact_text(value: Any, limit: int = MAX_DETAIL_CHARS) -> str:
    text = str(value or "")
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _WINDOWS_PATH_RE.sub("[PRIVATE_PATH]", text)
    text = _POSIX_PRIVATE_PATH_RE.sub("[PRIVATE_PATH]", text)
    if len(text) > limit:
        text = text[: max(0, limit - 13)] + "…[truncated]"
    return text


def _safe_detail(detail: Any) -> dict[str, Any]:
    if detail is None:
        return {}
    try:
        raw = json.dumps(detail, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        raw = json.dumps({"value": str(detail)}, ensure_ascii=False)
    redacted = _redact_text(raw, MAX_DETAIL_CHARS - 64)
    if len(redacted) >= MAX_DETAIL_CHARS - 64:
        return {"summary": redacted}
    try:
        decoded = json.loads(redacted)
    except json.JSONDecodeError:
        return {"summary": redacted}
    return decoded if isinstance(decoded, dict) else {"value": decoded}


def _json(detail: Any) -> str:
    return json.dumps(_safe_detail(detail), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _decode_detail(value: str | None) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {"summary": _redact_text(value)}
    return decoded if isinstance(decoded, dict) else {"value": decoded}


class ControlStore:
    def __init__(
        self,
        *,
        hermes_home: Path | str | None = None,
        db_path: Path | str | None = None,
    ) -> None:
        base = Path(hermes_home) if hermes_home is not None else Path(get_hermes_home())
        self.path = Path(db_path) if db_path is not None else base / "agent-dock" / "control-plane.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    @property
    def schema_version(self) -> int:
        with self._read() as db:
            row = db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 0

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self.connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def _migrate(self) -> None:
        with self._write() as db:
            db.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    binding_key TEXT NOT NULL UNIQUE,
                    profile TEXT NOT NULL,
                    runtime_profile TEXT,
                    session_id TEXT NOT NULL,
                    runtime_session_id TEXT,
                    subagent_id TEXT,
                    kanban_task_id TEXT,
                    source TEXT NOT NULL,
                    title TEXT,
                    objective TEXT NOT NULL DEFAULT '',
                    permission_scope TEXT NOT NULL DEFAULT 'inherit-only',
                    status TEXT NOT NULL,
                    current_action TEXT,
                    heartbeat_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_profile_updated ON runs(profile, updated_at DESC);

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    body TEXT NOT NULL,
                    state TEXT NOT NULL,
                    confirmed_at REAL,
                    dispatch_token TEXT,
                    lease_expires_at REAL,
                    claim_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_run_created ON messages(run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_state_lease ON messages(state, lease_expires_at);

                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    verification_state TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_id TEXT,
                    detail_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_receipts_run_created ON receipts(run_id, created_at);

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    message_id TEXT,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_id TEXT,
                    detail_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run_created ON events(run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_kind_created ON events(kind, created_at);
                """
            )
            db.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _binding_key(
        profile: str,
        session_id: str,
        subagent_id: str | None,
        kanban_task_id: str | None,
        source: str,
    ) -> str:
        return "\x1f".join((profile, session_id, subagent_id or "", kanban_task_id or "", source))

    def _event(
        self,
        db: sqlite3.Connection,
        *,
        kind: str,
        run_id: str | None = None,
        message_id: str | None = None,
        source: str = "agent-dock",
        source_id: str | None = None,
        detail: Any = None,
        now: float | int | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO events(run_id,message_id,kind,source,source_id,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                run_id,
                message_id,
                kind,
                _clean_identifier(source, "source", required=True),
                _redact_text(source_id, 240) if source_id else None,
                _json(detail),
                _now(now),
            ),
        )

    @staticmethod
    def _run_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    @staticmethod
    def _message_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def attach_run(
        self,
        *,
        profile: str,
        session_id: str,
        source: str,
        subagent_id: str | None = None,
        kanban_task_id: str | None = None,
        runtime_profile: str | None = None,
        runtime_session_id: str | None = None,
        title: str | None = None,
        objective: str = "",
        permission_scope: str = "inherit-only",
        status: str = "idle",
        run_id: str | None = None,
        now: float | int | None = None,
    ) -> dict[str, Any]:
        normalized_profile = (_clean_identifier(profile, "profile", required=True) or "").lower()
        stable_session = _clean_identifier(session_id, "session_id", required=True) or ""
        source_value = _clean_identifier(source, "source", required=True) or ""
        child = _clean_identifier(subagent_id, "subagent_id")
        task = _clean_identifier(kanban_task_id, "kanban_task_id")
        if status not in VALID_RUN_STATUSES:
            raise ValidationError("invalid run status")
        if permission_scope != "inherit-only":
            raise ValidationError("Agent Dock cannot expand run permissions")
        binding = self._binding_key(normalized_profile, stable_session, child, task, source_value)
        timestamp = _now(now)
        with self._write() as db:
            existing = db.execute("SELECT * FROM runs WHERE binding_key=?", (binding,)).fetchone()
            if existing:
                expected_runtime_profile = _clean_identifier(runtime_profile, "runtime_profile")
                if expected_runtime_profile:
                    expected_runtime_profile = expected_runtime_profile.lower()
                expected_runtime_session = _clean_identifier(runtime_session_id, "runtime_session_id")
                requested_run_id = _clean_identifier(run_id, "run_id")
                if (
                    (existing["runtime_profile"] or None) != expected_runtime_profile
                    or (existing["runtime_session_id"] or None) != expected_runtime_session
                    or (requested_run_id is not None and existing["run_id"] != requested_run_id)
                ):
                    raise ConflictError("run binding is already attached to a different runtime identity")
                return dict(existing)
            identity = _clean_identifier(run_id, "run_id") or f"run_{uuid.uuid4().hex}"
            db.execute(
                """INSERT INTO runs(
                    run_id,binding_key,profile,runtime_profile,session_id,runtime_session_id,
                    subagent_id,kanban_task_id,source,title,objective,permission_scope,status,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identity,
                    binding,
                    normalized_profile,
                    (_clean_identifier(runtime_profile, "runtime_profile") or "").lower() or None,
                    stable_session,
                    _clean_identifier(runtime_session_id, "runtime_session_id"),
                    child,
                    task,
                    source_value,
                    _redact_text(title, 240) if title else None,
                    _redact_text(objective, MAX_OBJECTIVE_CHARS),
                    permission_scope,
                    status,
                    timestamp,
                    timestamp,
                ),
            )
            self._event(
                db,
                kind="run_attached",
                run_id=identity,
                source="agent-dock",
                detail={"profile": normalized_profile, "source": source_value, "status": status},
                now=timestamp,
            )
            return dict(db.execute("SELECT * FROM runs WHERE run_id=?", (identity,)).fetchone())

    def list_runs(self, *, profile: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 200))
        with self._read() as db:
            if profile:
                rows = db.execute(
                    "SELECT * FROM runs WHERE profile=? ORDER BY updated_at DESC LIMIT ?",
                    (str(profile).strip().lower(), bounded),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?", (bounded,)
                ).fetchall()
        return [dict(row) for row in rows]

    def get_run(
        self,
        run_id: str,
        *,
        profile: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._read() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        if profile is not None and result["profile"] != str(profile).strip().lower():
            raise BindingError("run profile binding mismatch")
        if session_id is not None and result["session_id"] != str(session_id).strip():
            raise BindingError("run session binding mismatch")
        result["messages"] = self.list_messages(run_id)
        result["receipts"] = self.list_receipts(run_id=run_id)
        result["events"] = self.list_events(run_id=run_id)
        return result

    def enqueue_message(
        self,
        *,
        message_id: str,
        run_id: str,
        kind: str,
        body: str,
        confirmed: bool = False,
        permission_scope: str = "inherit-only",
        now: float | int | None = None,
    ) -> dict[str, Any]:
        identity = _clean_identifier(message_id, "message_id", required=True) or ""
        run_identity = _clean_identifier(run_id, "run_id", required=True) or ""
        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in VALID_MESSAGE_KINDS:
            raise ValidationError("invalid intervention kind")
        if permission_scope != "inherit-only":
            raise ValidationError("Agent Dock cannot expand run permissions")
        text = str(body or "").strip()
        if not text or len(text) > MAX_MESSAGE_CHARS:
            raise ValidationError("message body is empty or too long")
        if normalized_kind in CONFIRMED_MESSAGE_KINDS and confirmed is not True:
            raise ConfirmationRequired(f"{normalized_kind} requires explicit confirmation")
        timestamp = _now(now)
        with self._write() as db:
            if not db.execute("SELECT 1 FROM runs WHERE run_id=?", (run_identity,)).fetchone():
                raise BindingError("run not found")
            existing = db.execute("SELECT * FROM messages WHERE message_id=?", (identity,)).fetchone()
            if existing:
                same = (
                    existing["run_id"] == run_identity
                    and existing["kind"] == normalized_kind
                    and existing["body"] == text
                )
                if not same:
                    raise ConflictError("message_id is already bound to a different request")
                return dict(existing)
            confirmed_at = timestamp if normalized_kind in CONFIRMED_MESSAGE_KINDS else None
            db.execute(
                "INSERT INTO messages(message_id,run_id,kind,body,state,confirmed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (identity, run_identity, normalized_kind, text, "queued", confirmed_at, timestamp, timestamp),
            )
            db.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (timestamp, run_identity))
            self._event(
                db,
                kind="message_queued",
                run_id=run_identity,
                message_id=identity,
                detail={"kind": normalized_kind, "confirmed": bool(confirmed_at)},
                now=timestamp,
            )
            return dict(db.execute("SELECT * FROM messages WHERE message_id=?", (identity,)).fetchone())

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._read() as db:
            row = db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
        return self._message_row(row)

    def list_messages(self, run_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._read() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE run_id=? ORDER BY created_at ASC LIMIT ?",
                (run_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_message(
        self,
        message_id: str,
        *,
        dispatch_token: str | None = None,
        dispatcher_id: str | None = None,
        now: float | int | None = None,
        lease_seconds: float = 30,
    ) -> dict[str, Any]:
        token = _clean_identifier(dispatch_token or dispatcher_id, "dispatch_token") or f"dispatch_{uuid.uuid4().hex}"
        timestamp = _now(now)
        lease = max(1.0, min(float(lease_seconds), 300.0))
        with self._write() as db:
            row = db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if not row:
                raise BindingError("message not found")
            if row["state"] in TERMINAL_MESSAGE_STATES or row["state"] in {"accepted", "delivered"}:
                raise LeaseError("message is no longer claimable")
            if row["state"] == "dispatching" and float(row["lease_expires_at"] or 0) > timestamp:
                if row["dispatch_token"] == token:
                    return dict(row)
                raise LeaseError("message already has an active dispatch lease")
            db.execute(
                """UPDATE messages SET state='dispatching',dispatch_token=?,lease_expires_at=?,
                    claim_count=claim_count+1,updated_at=? WHERE message_id=?""",
                (token, timestamp + lease, timestamp, message_id),
            )
            self._event(
                db,
                kind="message_claimed",
                run_id=row["run_id"],
                message_id=message_id,
                detail={"lease_seconds": lease, "claim_count": int(row["claim_count"]) + 1},
                now=timestamp,
            )
            return dict(db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone())

    def reclaim_expired(self, *, now: float | int | None = None) -> int:
        timestamp = _now(now)
        with self._write() as db:
            rows = db.execute(
                "SELECT message_id,run_id FROM messages WHERE state='dispatching' AND lease_expires_at<=?",
                (timestamp,),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE messages SET state='queued',dispatch_token=NULL,lease_expires_at=NULL,updated_at=? WHERE message_id=?",
                    (timestamp, row["message_id"]),
                )
                self._event(
                    db,
                    kind="message_reclaimed",
                    run_id=row["run_id"],
                    message_id=row["message_id"],
                    detail={"reason": "lease_expired"},
                    now=timestamp,
                )
            return len(rows)

    def transition_message(self, message_id: str, state: str, *, now: float | int | None = None) -> dict[str, Any]:
        if state not in {"queued", "dispatching", *VALID_RECEIPT_STAGES}:
            raise TransitionError("invalid message state")
        with self._write() as db:
            row = db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if not row:
                raise BindingError("message not found")
            if row["state"] in TERMINAL_MESSAGE_STATES:
                raise TransitionError("terminal message state is immutable")
            allowed = {
                "queued": {"dispatching", "failed", "rejected", "superseded", "unknown"},
                "dispatching": {"accepted", "delivered", "failed", "rejected", "superseded", "unknown"},
                "accepted": {"delivered", "failed", "rejected", "superseded", "unknown"},
                "delivered": {"applied", "failed", "rejected", "superseded", "unknown"},
            }
            if state not in allowed.get(row["state"], set()):
                raise TransitionError(f"invalid transition {row['state']} -> {state}")
            timestamp = _now(now)
            db.execute(
                "UPDATE messages SET state=?,updated_at=?,lease_expires_at=NULL WHERE message_id=?",
                (state, timestamp, message_id),
            )
            return dict(db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone())

    def record_receipt(
        self,
        *,
        message_id: str,
        stage: str | None = None,
        state: str | None = None,
        verification_state: str | None = None,
        verification: str | None = None,
        source: str,
        detail: Any = None,
        source_id: str | None = None,
        receipt_id: str | None = None,
        dispatch_token: str | None = None,
        now: float | int | None = None,
    ) -> dict[str, Any]:
        receipt_stage = str(stage or state or "").strip().lower()
        verified = str(verification_state or verification or "unverified").strip().lower()
        if receipt_stage not in VALID_RECEIPT_STAGES:
            raise ValidationError("invalid receipt stage")
        if verified not in VALID_VERIFICATION_STATES:
            raise ValidationError("invalid verification state")
        source_value = _clean_identifier(source, "source", required=True) or ""
        timestamp = _now(now)
        identity = _clean_identifier(receipt_id, "receipt_id", required=True) or ""
        claim_token = _clean_identifier(dispatch_token, "dispatch_token")
        detail_json = _json(detail)
        safe_source_id = _redact_text(source_id, 240) if source_id else None
        with self._write() as db:
            message = db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if not message:
                raise BindingError("message not found")
            existing_receipt = db.execute(
                "SELECT * FROM receipts WHERE receipt_id=?", (identity,)
            ).fetchone()
            if existing_receipt:
                same = (
                    existing_receipt["message_id"] == message_id
                    and existing_receipt["stage"] == receipt_stage
                    and existing_receipt["verification_state"] == verified
                    and existing_receipt["source"] == source_value
                    and (existing_receipt["source_id"] or None) == safe_source_id
                    and existing_receipt["detail_json"] == detail_json
                )
                if not same:
                    raise ConflictError("receipt_id is already bound to a different receipt")
                result = dict(existing_receipt)
                result["detail"] = _decode_detail(result.pop("detail_json"))
                result["message_state"] = message["state"]
                return result
            if message["state"] in TERMINAL_MESSAGE_STATES:
                raise TransitionError("terminal message state is immutable")
            if message["state"] == "queued" and receipt_stage in {"accepted", "delivered", "applied"}:
                raise LeaseError("message must be claimed before a delivery receipt")
            if message["state"] == "dispatching":
                if not claim_token or claim_token != message["dispatch_token"]:
                    raise LeaseError("receipt does not own the active dispatch lease")
                if float(message["lease_expires_at"] or 0) <= timestamp:
                    raise LeaseError("dispatch lease expired before receipt")
            current = db.execute("SELECT state FROM messages WHERE message_id=?", (message_id,)).fetchone()[0]
            if receipt_stage != current:
                allowed = {
                    "dispatching": {"accepted", "delivered", "failed", "rejected", "superseded", "unknown"},
                    "accepted": {"delivered", "failed", "rejected", "superseded", "unknown"},
                    "delivered": {"applied", "failed", "rejected", "superseded", "unknown"},
                    "queued": {"failed", "rejected", "superseded", "unknown"},
                }
                if receipt_stage not in allowed.get(current, set()):
                    raise TransitionError(f"invalid receipt transition {current} -> {receipt_stage}")
                db.execute(
                    "UPDATE messages SET state=?,updated_at=?,lease_expires_at=NULL WHERE message_id=?",
                    (receipt_stage, timestamp, message_id),
                )
            db.execute(
                "INSERT INTO receipts(receipt_id,message_id,run_id,stage,verification_state,source,source_id,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    message_id,
                    message["run_id"],
                    receipt_stage,
                    verified,
                    source_value,
                    safe_source_id,
                    detail_json,
                    timestamp,
                ),
            )
            self._event(
                db,
                kind="receipt_recorded",
                run_id=message["run_id"],
                message_id=message_id,
                source=source_value,
                source_id=source_id,
                detail={"stage": receipt_stage, "verification_state": verified, **_decode_detail(detail_json)},
                now=timestamp,
            )
            result = dict(db.execute("SELECT * FROM receipts WHERE receipt_id=?", (identity,)).fetchone())
            result["detail"] = _decode_detail(result.pop("detail_json"))
            result["message_state"] = receipt_stage
            return result

    def list_receipts(self, *, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._read() as db:
            rows = db.execute(
                "SELECT * FROM receipts WHERE run_id=? ORDER BY created_at ASC LIMIT ?",
                (run_id, max(1, min(int(limit), 500))),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["detail"] = _decode_detail(item.pop("detail_json"))
            results.append(item)
        return results

    def observe_run(
        self,
        run_id: str,
        *,
        profile: str | None = None,
        session_id: str | None = None,
        status: str,
        heartbeat_at: float | int | None = None,
        detail: Any = None,
        now: float | int | None = None,
    ) -> dict[str, Any]:
        if status not in VALID_RUN_STATUSES:
            raise TransitionError("invalid observed run status")
        timestamp = _now(now)
        with self._write() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise BindingError("run not found")
            if profile is not None and row["profile"] != str(profile).strip().lower():
                raise BindingError("run profile binding mismatch")
            if session_id is not None and row["session_id"] != str(session_id).strip():
                raise BindingError("run session binding mismatch")
            if row["status"] in TERMINAL_RUN_STATUSES and status != row["status"]:
                raise TransitionError("terminal run status is immutable")
            safe = _safe_detail(detail)
            db.execute(
                "UPDATE runs SET status=?,current_action=?,heartbeat_at=?,updated_at=? WHERE run_id=?",
                (status, _redact_text(safe.get("current_action"), 240) if safe.get("current_action") else None,
                 _now(heartbeat_at) if heartbeat_at is not None else row["heartbeat_at"], timestamp, run_id),
            )
            self._event(db, kind="run_observed", run_id=run_id, detail={"status": status, **safe}, now=timestamp)
            return dict(db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())

    def reconcile_runs(self, observations: list[dict[str, Any]], *, now: float | int | None = None) -> dict[str, Any]:
        timestamp = _now(now)
        updated: list[str] = []
        unknown: list[str] = []
        for observation in observations:
            identity = str(observation.get("run_id") or "").strip()
            with self._read() as db:
                row = db.execute("SELECT run_id FROM runs WHERE run_id=?", (identity,)).fetchone()
            if not row:
                unknown.append(identity)
                with self._write() as db:
                    self._event(
                        db,
                        kind="unknown_observation",
                        source="agent-dock",
                        source_id=identity,
                        detail={
                            "run_id": identity,
                            "profile": observation.get("profile"),
                            "session_id": observation.get("session_id"),
                            "status": observation.get("status"),
                        },
                        now=timestamp,
                    )
                continue
            self.observe_run(
                identity,
                profile=observation.get("profile"),
                session_id=observation.get("session_id"),
                status=str(observation.get("status") or "unavailable"),
                heartbeat_at=observation.get("heartbeat_at"),
                detail=observation.get("detail"),
                now=timestamp,
            )
            updated.append(identity)
        return {"updated": updated, "unknown": unknown}

    def list_events(
        self,
        *,
        run_id: str | None = None,
        message_id: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self.search_events(run_id=run_id, message_id=message_id, kind=kind, limit=limit)

    def search_events(
        self,
        *,
        run_id: str | None = None,
        message_id: str | None = None,
        kind: str | None = None,
        q: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        if message_id is not None:
            clauses.append("message_id=?")
            params.append(message_id)
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if q:
            clauses.append("(kind LIKE ? OR source LIKE ? OR source_id LIKE ? OR detail_json LIKE ?)")
            needle = f"%{str(q)[:120]}%"
            params.extend([needle, needle, needle, needle])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._read() as db:
            rows = db.execute(
                f"SELECT * FROM events{where} ORDER BY created_at ASC,event_id ASC LIMIT ?",
                params,
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["detail"] = _decode_detail(item.pop("detail_json"))
            results.append(item)
        return results
