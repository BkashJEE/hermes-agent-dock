"""Durable metadata-only job ledger for the Hermes Agent Dock.

The ledger owns job identity and state transitions, but deliberately does not
store prompts, responses, image bytes, attachment paths, commands, or process
output.  Each operation opens its own SQLite connection so callers may safely
share one ``JobStore`` across worker threads and backend instances.
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


BUSY_TIMEOUT_SECONDS = 5.0
MAX_SUMMARY_CHARS = 500
MAX_IDENTIFIER_CHARS = 512

_ACTIVE_ON_RESTART = frozenset({"starting", "queued", "running", "finalizing"})
_TERMINAL_STATUSES = frozenset({"done", "error", "cancelled", "interrupted"})
_ALLOWED_TRANSITIONS = {
    "starting": frozenset({"running", "cancelling", "error"}),
    "queued": frozenset({"running", "cancelling", "error"}),
    "running": frozenset({"finalizing", "cancelling", "error"}),
    "finalizing": frozenset({"done", "error"}),
    "cancelling": frozenset({"cancelled"}),
}

# Keep these expressions intentionally conservative.  The ledger only needs a
# bounded human-readable summary; anything that looks like a secret or private
# path is replaced before it reaches SQLite.
_AUTHORIZATION_RE = re.compile(
    r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?[^\s,;}]+"
)
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;}]+"
)
# Error summaries are not evidence logs. Prefer over-redacting the remainder
# of a path-bearing clause to leaking a path containing spaces.
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\[^\r\n,;}]+")
_UNC_PATH_RE = re.compile(r"\\\\[^\r\n,;}]+")
_POSIX_PRIVATE_PATH_RE = re.compile(
    r"(?i)(?:/home/|/Users/|/tmp/|/var/|/etc/|/opt/|/srv/)[^\r\n,;}]+"
)

_UNSET = object()


class JobStore:
    """SQLite-backed job identity and state ledger.

    ``hermes_home`` is the root for this backend instance.  A fresh store
    instance reconciles unfinished work while holding a write transaction,
    invalidating every old attempt token before it returns.
    """

    def __init__(self, hermes_home: Path | str) -> None:
        self.hermes_home = Path(hermes_home).resolve()
        self.database_path = (
            self.hermes_home
            / "plugins"
            / "hermes-agent-dock"
            / "state"
            / "jobs.sqlite3"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._reconcile_on_startup()

    @property
    def path(self) -> Path:
        """Compatibility alias for callers that use other local stores."""
        return self.database_path

    @property
    def columns(self) -> set[str]:
        with self._connection() as db:
            rows = db.execute("PRAGMA table_info(jobs)").fetchall()
        return {str(row[1]) for row in rows}

    def close(self) -> None:
        """Retained for singleton lifecycle symmetry; operations use short connections."""
        return None

    @staticmethod
    def _now() -> int:
        return int(time.time())

    @staticmethod
    def _token() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _job_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _identifier(value: Any, *, allow_none: bool = True) -> str | None:
        if value is None and allow_none:
            return None
        text = str(value if value is not None else "").strip()
        if not text and allow_none:
            return None
        if not text or len(text) > MAX_IDENTIFIER_CHARS or any(ord(char) < 32 for char in text):
            raise ValueError("job identifier is invalid")
        return text

    @staticmethod
    def _safe_summary(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value)
        text = _AUTHORIZATION_RE.sub("authorization=[REDACTED]", text)
        text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
        text = _WINDOWS_PATH_RE.sub("[PRIVATE_PATH]", text)
        text = _UNC_PATH_RE.sub("[PRIVATE_PATH]", text)
        text = _POSIX_PRIVATE_PATH_RE.sub("[PRIVATE_PATH]", text)
        text = " ".join(text.split())
        if len(text) > MAX_SUMMARY_CHARS:
            text = text[: MAX_SUMMARY_CHARS - 14] + "…[truncated]"
        return text or None

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(
            self.database_path,
            timeout=BUSY_TIMEOUT_SECONDS,
            isolation_level=None,
            check_same_thread=False,
        )
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_SECONDS * 1000)}")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connection(write=True) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    request_id TEXT,
                    provider TEXT,
                    model TEXT,
                    reasoning_effort TEXT NOT NULL DEFAULT 'none',
                    fast INTEGER NOT NULL DEFAULT 0,
                    assign_task INTEGER NOT NULL DEFAULT 0,
                    image_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER,
                    updated_at INTEGER NOT NULL,
                    session_id TEXT,
                    attempt_token TEXT NOT NULL,
                    error_summary TEXT,
                    kanban_task_id TEXT,
                    kanban_board TEXT,
                    UNIQUE(profile_id, request_id)
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_updated "
                "ON jobs(status, updated_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_profile_request "
                "ON jobs(profile_id, request_id)"
            )

    def _reconcile_on_startup(self) -> None:
        timestamp = self._now()
        with self._connection(write=True) as db:
            rows = db.execute(
                "SELECT job_id, status FROM jobs WHERE status IN (?, ?, ?, ?, ?)",
                ("starting", "queued", "running", "finalizing", "cancelling"),
            ).fetchall()
            for row in rows:
                was_cancelling = row["status"] == "cancelling"
                status = "cancelled" if was_cancelling else "interrupted"
                summary = (
                    "Job cancelled during Agent Dock restart reconciliation"
                    if was_cancelling
                    else "Job interrupted during Agent Dock restart reconciliation"
                )
                db.execute(
                    """
                    UPDATE jobs
                    SET status=?, attempt_token=?, finished_at=?, updated_at=?, error_summary=?
                    WHERE job_id=? AND status=?
                    """,
                    (
                        status,
                        self._token(),
                        timestamp,
                        timestamp,
                        summary,
                        row["job_id"],
                        row["status"],
                    ),
                )

    @staticmethod
    def _row_value(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["fast"] = bool(result.get("fast"))
        result["assign_task"] = bool(result.get("assign_task"))
        result["image_count"] = int(result.get("image_count") or 0)
        # The response is intentionally virtual.  It is never a SQLite column.
        result["response_body"] = None
        return result

    def _get_row(self, db: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._row_value(row) if row else None

    def reserve_job(
        self,
        *,
        job_id: str | None = None,
        profile_id: str,
        request_id: str | None,
        provider: str | None,
        model: str | None,
        reasoning_effort: str,
        fast: bool,
        assign_task: bool,
        image_count: int,
        session_id: str | None,
        kanban_task_id: str | None,
        kanban_board: str | None,
    ) -> tuple[dict[str, Any], bool]:
        profile = self._identifier(profile_id, allow_none=False)
        request = self._identifier(request_id)
        provider_value = self._identifier(provider)
        model_value = self._identifier(model)
        session = self._identifier(session_id)
        task = self._identifier(kanban_task_id)
        board = self._identifier(kanban_board)
        effort = self._identifier(reasoning_effort, allow_none=False) or "none"
        count = max(0, int(image_count))
        timestamp = self._now()

        with self._connection(write=True) as db:
            # A missing request ID is intentionally non-idempotent.  SQLite's
            # UNIQUE semantics allow multiple NULLs, but a retry has no key to
            # resolve, so do not select an arbitrary keyless job here.
            existing = None
            if request is not None:
                existing_row = db.execute(
                    "SELECT * FROM jobs WHERE profile_id=? AND request_id=?",
                    (profile, request),
                ).fetchone()
                if existing_row:
                    return self._row_value(existing_row), False

            job_id = self._identifier(job_id) or self._job_id()
            db.execute(
                """
                INSERT INTO jobs(
                    job_id, profile_id, request_id, provider, model, reasoning_effort,
                    fast, assign_task, image_count, status, created_at, started_at,
                    finished_at, updated_at, session_id, attempt_token, error_summary,
                    kanban_task_id, kanban_board
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    profile,
                    request,
                    provider_value,
                    model_value,
                    effort,
                    int(bool(fast)),
                    int(bool(assign_task)),
                    count,
                    "starting",
                    timestamp,
                    None,
                    None,
                    timestamp,
                    session,
                    self._token(),
                    None,
                    task,
                    board,
                ),
            )
            return self._get_row(db, job_id) or {}, True

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        identity = self._identifier(job_id, allow_none=False)
        with self._connection() as db:
            return self._get_row(db, identity or "")

    def get_by_request(self, profile_id: str, request_id: str | None) -> dict[str, Any] | None:
        profile = self._identifier(profile_id, allow_none=False)
        request = self._identifier(request_id)
        if request is None:
            return None
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE profile_id=? AND request_id=?",
                (profile, request),
            ).fetchone()
            return self._row_value(row) if row else None

    def list_jobs(self, *, limit: int = 200) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 1_000))
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, job_id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [self._row_value(row) for row in rows]

    def _transition(
        self,
        job_id: str,
        attempt_token: str,
        target_status: str,
        *,
        source_statuses: set[str] | frozenset[str] | None = None,
        session_id: str | None | object = _UNSET,
        error_summary: str | None | object = _UNSET,
        finished: bool = False,
        started: bool = False,
    ) -> bool:
        identity = self._identifier(job_id, allow_none=False) or ""
        token = self._identifier(attempt_token, allow_none=False) or ""
        timestamp = self._now()
        allowed_sources = source_statuses or {
            source for source, targets in _ALLOWED_TRANSITIONS.items() if target_status in targets
        }
        if target_status not in _TERMINAL_STATUSES | {"starting", "queued", "running", "finalizing", "cancelling"}:
            return False

        assignments = ["status=?", "updated_at=?"]
        values: list[Any] = [target_status, timestamp]
        if started:
            assignments.append("started_at=COALESCE(started_at, ?)")
            values.append(timestamp)
        if finished:
            assignments.append("finished_at=COALESCE(finished_at, ?)")
            values.append(timestamp)
        if session_id is not _UNSET:
            assignments.append("session_id=?")
            values.append(self._identifier(session_id))
        if error_summary is not _UNSET:
            assignments.append("error_summary=?")
            values.append(self._safe_summary(error_summary))
        values.extend([identity, token, *allowed_sources])
        placeholders = ",".join("?" for _ in allowed_sources)
        with self._connection(write=True) as db:
            row = db.execute(
                f"SELECT status FROM jobs WHERE job_id=? AND attempt_token=?", (identity, token)
            ).fetchone()
            if not row:
                return False
            current = str(row["status"])
            if current == target_status:
                return True
            if current in _TERMINAL_STATUSES or current not in allowed_sources:
                return False
            result = db.execute(
                f"UPDATE jobs SET {', '.join(assignments)} "
                f"WHERE job_id=? AND attempt_token=? AND status IN ({placeholders})",
                values,
            )
            return result.rowcount == 1

    def transition(
        self,
        job_id: str,
        attempt_token: str,
        target_status: str,
        *,
        session_id: str | None = None,
        error_summary: str | None = None,
    ) -> bool:
        """CAS a job to a legal state, for integration code needing a generic hook."""
        return self._transition(
            job_id,
            attempt_token,
            target_status,
            session_id=session_id if session_id is not None else _UNSET,
            error_summary=error_summary if error_summary is not None else _UNSET,
            finished=target_status in _TERMINAL_STATUSES,
            started=target_status == "running",
        )

    def mark_running(self, job_id: str, attempt_token: str) -> bool:
        return self._transition(
            job_id,
            attempt_token,
            "running",
            source_statuses=frozenset({"starting", "queued"}),
            started=True,
        )

    def request_cancel(self, job_id: str, attempt_token: str) -> bool:
        return self._transition(
            job_id,
            attempt_token,
            "cancelling",
            source_statuses=frozenset({"starting", "queued", "running"}),
        )

    def complete_cancelled(
        self,
        job_id: str,
        attempt_token: str,
        summary: str = "Job cancelled by Agent Dock",
    ) -> bool:
        return self._transition(
            job_id,
            attempt_token,
            "cancelled",
            source_statuses=frozenset({"cancelling"}),
            error_summary=summary,
            finished=True,
        )

    def begin_finalization(
        self,
        job_id: str,
        attempt_token: str,
        *,
        session_id: str | None,
    ) -> bool:
        return self._transition(
            job_id,
            attempt_token,
            "finalizing",
            source_statuses=frozenset({"running"}),
            session_id=session_id,
        )

    def complete_done(self, job_id: str, attempt_token: str) -> bool:
        return self._transition(
            job_id,
            attempt_token,
            "done",
            source_statuses=frozenset({"finalizing"}),
            finished=True,
            error_summary=None,
        )

    def complete_error(self, job_id: str, attempt_token: str, summary: str) -> bool:
        return self._transition(
            job_id,
            attempt_token,
            "error",
            source_statuses=frozenset({"starting", "queued", "running", "finalizing"}),
            error_summary=summary,
            finished=True,
        )

    def reset_attempt(
        self,
        job_id: str,
        *,
        allow_statuses: frozenset[str] | set[str],
    ) -> dict[str, Any] | None:
        """Re-arm a terminal job for exactly one new attempt.

        Returns the fresh row (new attempt token, ``starting`` status) when the
        reset is applied, or ``None`` when the job is unknown, not in an
        allowed status, or lost a concurrent race. The job id, identity, and
        idempotency key are preserved so the retry is the same logical job run
        again — not a new job.
        """
        identity = self._identifier(job_id, allow_none=False) or ""
        timestamp = self._now()
        placeholders = ",".join("?" for _ in allow_statuses)
        with self._connection(write=True) as db:
            row = db.execute(
                f"""
                UPDATE jobs
                SET status='starting',
                    attempt_token=?,
                    finished_at=NULL,
                    updated_at=?,
                    error_summary=NULL
                WHERE job_id=? AND status IN ({placeholders})
                """,
                (self._token(), timestamp, identity, *allow_statuses),
            )
            if row.rowcount != 1:
                return None
            return self._get_row(db, identity)

    def link_kanban_terminal(
        self,
        job_id: str,
        *,
        kanban_task_id: str,
        kanban_board: str,
        allow_statuses: frozenset[str] | set[str],
    ) -> bool:
        """Record card linkage for a terminal job.

        ``update_kanban`` is guarded to live attempts only; assign-after
        legally links a card to a finished job. The status guard is the
        caller's explicit allow-set so a race to another state fails closed.
        """
        identity = self._identifier(job_id, allow_none=False) or ""
        task = self._identifier(kanban_task_id, allow_none=False) or ""
        board = self._identifier(kanban_board, allow_none=False) or ""
        timestamp = self._now()
        statuses = frozenset(allow_statuses)
        placeholders = ",".join("?" for _ in statuses)
        with self._connection(write=True) as db:
            result = db.execute(
                f"""
                UPDATE jobs
                SET kanban_task_id=?, kanban_board=?, updated_at=?
                WHERE job_id=? AND status IN ({placeholders})
                """,
                (task, board, timestamp, identity, *statuses),
            )
            return result.rowcount == 1

    def update_kanban(
        self,
        job_id: str,
        attempt_token: str,
        *,
        kanban_task_id: str | None,
        kanban_board: str | None,
    ) -> bool:
        identity = self._identifier(job_id, allow_none=False) or ""
        token = self._identifier(attempt_token, allow_none=False) or ""
        task = self._identifier(kanban_task_id)
        board = self._identifier(kanban_board)
        timestamp = self._now()
        statuses = frozenset({"starting", "queued", "running", "finalizing"})
        placeholders = ",".join("?" for _ in statuses)
        with self._connection(write=True) as db:
            result = db.execute(
                f"""
                UPDATE jobs
                SET kanban_task_id=?, kanban_board=?, updated_at=?
                WHERE job_id=? AND attempt_token=? AND status IN ({placeholders})
                """,
                (task, board, timestamp, identity, token, *statuses),
            )
            return result.rowcount == 1

    def publish(
        self,
        job_id: str,
        attempt_token: str,
        callback: Callable[[dict[str, Any]], Any] | None = None,
    ) -> bool:
        """Commit terminal success for the exact finalizing attempt.

        Any callback runs only after the durable commit. External effects can
        therefore never occur and then be rolled back to ``finalizing``.
        """
        identity = self._identifier(job_id, allow_none=False) or ""
        token = self._identifier(attempt_token, allow_none=False) or ""
        timestamp = self._now()
        published_row: dict[str, Any] | None = None
        with self._connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE job_id=? AND attempt_token=? AND status='finalizing'",
                (identity, token),
            ).fetchone()
            if not row:
                return False
            result = db.execute(
                """
                UPDATE jobs
                SET status='done', finished_at=COALESCE(finished_at, ?),
                    updated_at=?, error_summary=NULL
                WHERE job_id=? AND attempt_token=? AND status='finalizing'
                """,
                (timestamp, timestamp, identity, token),
            )
            if result.rowcount != 1:
                return False
            published_row = self._row_value(row)
        if callback is not None and published_row is not None:
            callback(published_row)
        return True
