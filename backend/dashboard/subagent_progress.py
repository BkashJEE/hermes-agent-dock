"""Privacy-reduced Hermes delegated-subagent lifecycle records.

The runner writes only records from this bounded schema.  The API validates the
JSONL again before exposing anything to the plugin, because the progress file
is an internal transport boundary rather than a public trust boundary.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, MutableSet, Sequence
from typing import Any

SUBAGENT_EVENTS = frozenset(
    {
        "subagent.start",
        "subagent.progress",
        "subagent.tool",
        "subagent.complete",
    }
)
SUBAGENT_STATUSES = frozenset({"running", "completed", "failed", "interrupted"})
TERMINAL_SUBAGENT_STATUSES = frozenset({"completed", "failed", "interrupted"})
_STATUS_RANK = {
    "running": 1,
    "completed": 2,
    "failed": 2,
    "interrupted": 2,
}

# Tool labels are intentionally a finite public vocabulary.  Arguments,
# paths, result text, and provider-specific tool names never cross this
# boundary.  Keep this list aligned with the tools that can be useful as a
# compact activity label in Agent Dock.
SAFE_CURRENT_TOOLS = frozenset(
    {
        "browser",
        "browser_use",
        "computer",
        "computer_use",
        "delegate",
        "delegate_task",
        "execute_code",
        "image_generate",
        "mcp",
        "patch",
        "read_file",
        "search_files",
        "terminal",
        "text_to_speech",
        "vision_analyze",
        "web_extract",
        "web_search",
        "write_file",
    }
)

PUBLIC_SUBAGENT_FIELDS = frozenset(
    {
        "subagent_id",
        "task_index",
        "status",
        "started_at",
        "updated_at",
        "finished_at",
        "current_tool",
        "duration_seconds",
        "model",
        "api_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "usage_state",
        "direct_chat_available",
    }
)
INTERNAL_SUBAGENT_FIELDS = PUBLIC_SUBAGENT_FIELDS | {"event"}
_PRIVATE_FIELD_NAMES = frozenset(
    {
        "args",
        "credentials",
        "goal",
        "path",
        "private_path",
        "prompt",
        "result",
        "summary",
        "tool_args",
        "tool_input",
        "tool_output",
        "token",
    }
)
_SUBAGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}:subagent:\d+$")
_TOOL_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_MAX_TASK_INDEX = 10_000
_MAX_DURATION_SECONDS = 24 * 60 * 60 * 30
_MAX_USAGE_VALUE = 10**15
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")


def subagent_id_for(main_job_id: str, task_index: int) -> str:
    """Return the only public child identity accepted for a main job."""
    normalized_job_id = str(main_job_id or "").strip()
    if not normalized_job_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}", normalized_job_id):
        raise ValueError("invalid main job id")
    if isinstance(task_index, bool) or not isinstance(task_index, int) or not 0 <= task_index <= _MAX_TASK_INDEX:
        raise ValueError("invalid subagent task index")
    return f"{normalized_job_id}:subagent:{task_index}"


def _number(value: Any, *, maximum: float | None = None) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    if maximum is not None and number > maximum:
        return None
    return int(number) if number.is_integer() else number


def _task_index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_TASK_INDEX:
        return None
    return value


def _status_for_event(event: str, raw_status: Any) -> str:
    status = str(raw_status or "").strip().lower()
    if status in SUBAGENT_STATUSES:
        return status
    return "completed" if event == "subagent.complete" else "running"


def _safe_tool(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    tool = value.strip().lower()
    if not _TOOL_LABEL_RE.fullmatch(tool) or tool not in SAFE_CURRENT_TOOLS:
        return None
    return tool


def _usage_integer(value: Any) -> int | None:
    number = _number(value, maximum=_MAX_USAGE_VALUE)
    return number if isinstance(number, int) else None


def _safe_model(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    model = value.strip()
    return model if _MODEL_RE.fullmatch(model) else None


def validate_progress_entry(
    raw: Any,
    *,
    expected_job_id: str | None = None,
    require_event: bool = True,
) -> dict[str, Any] | None:
    """Validate and privacy-project one internal JSONL record.

    ``None`` means the line is rejected.  Unknown keys are rejected rather than
    silently copied, which prevents a future Hermes event field from becoming
    an accidental prompt/path/credential channel.
    """
    if not isinstance(raw, Mapping):
        return None
    keys = {str(key) for key in raw}
    if keys & _PRIVATE_FIELD_NAMES:
        return None
    if not keys.issubset(INTERNAL_SUBAGENT_FIELDS):
        return None

    event = str(raw.get("event") or "").strip().lower()
    if require_event and event not in SUBAGENT_EVENTS:
        return None
    if not require_event and event and event not in SUBAGENT_EVENTS:
        return None

    index = _task_index(raw.get("task_index"))
    if index is None:
        return None
    expected_id = subagent_id_for(expected_job_id, index) if expected_job_id else None
    subagent_id = str(raw.get("subagent_id") or "").strip()
    if expected_id:
        if subagent_id != expected_id:
            return None
    elif not _SUBAGENT_ID_RE.fullmatch(subagent_id):
        return None

    status = _status_for_event(event, raw.get("status"))
    if status not in SUBAGENT_STATUSES:
        return None

    started_at = _number(raw.get("started_at"))
    updated_at = _number(raw.get("updated_at"))
    finished_at = _number(raw.get("finished_at"))
    duration = _number(raw.get("duration_seconds"), maximum=_MAX_DURATION_SECONDS)
    if updated_at is None:
        return None
    if started_at is not None and updated_at < started_at:
        return None
    if finished_at is not None and finished_at < updated_at:
        # A callback timestamp can be slightly reordered, but exposing a
        # backwards terminal clock makes UI duration misleading.  Reject it.
        return None
    if status in TERMINAL_SUBAGENT_STATUSES and finished_at is None:
        return None
    if status == "running" and finished_at is not None:
        return None

    current_tool = _safe_tool(raw.get("current_tool"))
    if raw.get("current_tool") is not None and current_tool is None:
        # Unknown labels are safe to omit for a progress update.  A path-like
        # or object-valued label is rejected by the strict type/shape above.
        value = raw.get("current_tool")
        if not isinstance(value, str) or any(marker in value for marker in ("/", "\\", "=", " ")):
            return None

    model = _safe_model(raw.get("model"))
    if raw.get("model") is not None and model is None:
        return None
    api_calls = _usage_integer(raw.get("api_calls"))
    input_tokens = _usage_integer(raw.get("input_tokens"))
    output_tokens = _usage_integer(raw.get("output_tokens"))
    total_tokens = _usage_integer(raw.get("total_tokens"))
    usage_state = str(raw.get("usage_state") or "unavailable").strip().lower()
    if usage_state not in {"unavailable", "reported"}:
        return None
    if status not in TERMINAL_SUBAGENT_STATUSES and usage_state == "reported":
        return None
    if usage_state == "reported":
        if input_tokens is None or output_tokens is None:
            return None
        computed_total = input_tokens + output_tokens
        if total_tokens is None:
            total_tokens = computed_total
        if total_tokens != computed_total:
            return None
    else:
        api_calls = input_tokens = output_tokens = total_tokens = None
    if raw.get("direct_chat_available", False) is not False:
        return None

    projected: dict[str, Any] = {
        "subagent_id": subagent_id,
        "task_index": index,
        "status": status,
        "started_at": started_at,
        "updated_at": updated_at,
        "finished_at": finished_at,
        "current_tool": current_tool,
        "duration_seconds": duration,
        "model": model,
        "api_calls": api_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "usage_state": usage_state,
        "direct_chat_available": False,
    }
    if event:
        projected["event"] = event
    return projected


def _record_key(record: Mapping[str, Any]) -> str:
    return str(record.get("subagent_id") or "")


def _merge_record(previous: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    old = dict(previous)
    new = dict(incoming)
    old_status = str(old.get("status") or "running")
    new_status = str(new.get("status") or "running")
    old_terminal = old_status in TERMINAL_SUBAGENT_STATUSES
    if old_terminal:
        status = old_status
    elif _STATUS_RANK.get(new_status, 0) >= _STATUS_RANK.get(old_status, 0):
        status = new_status
    else:
        status = old_status

    def earliest(name: str) -> int | float | None:
        values = [value for value in (old.get(name), new.get(name)) if value is not None]
        return min(values) if values else None

    def latest(name: str) -> int | float | None:
        values = [value for value in (old.get(name), new.get(name)) if value is not None]
        return max(values) if values else None

    merged: dict[str, Any] = {
        "subagent_id": _record_key(old) or _record_key(new),
        "task_index": old.get("task_index", new.get("task_index")),
        "status": status,
        "started_at": earliest("started_at"),
        "updated_at": latest("updated_at"),
        "finished_at": old.get("finished_at") if old.get("finished_at") is not None else new.get("finished_at"),
        "current_tool": None,
        "duration_seconds": latest("duration_seconds"),
        "model": new.get("model") or old.get("model"),
        "api_calls": new.get("api_calls") if new.get("api_calls") is not None else old.get("api_calls"),
        "input_tokens": new.get("input_tokens") if new.get("input_tokens") is not None else old.get("input_tokens"),
        "output_tokens": new.get("output_tokens") if new.get("output_tokens") is not None else old.get("output_tokens"),
        "total_tokens": new.get("total_tokens") if new.get("total_tokens") is not None else old.get("total_tokens"),
        "usage_state": "reported" if "reported" in {old.get("usage_state"), new.get("usage_state")} else "unavailable",
        "direct_chat_available": False,
    }
    if status not in TERMINAL_SUBAGENT_STATUSES:
        merged["finished_at"] = None
        merged["current_tool"] = new.get("current_tool") or old.get("current_tool")
    else:
        started = merged.get("started_at")
        finished = merged.get("finished_at")
        computed = (finished - started) if started is not None and finished is not None else None
        if computed is not None and computed >= 0:
            merged["duration_seconds"] = max(merged.get("duration_seconds") or 0, computed)
    return merged


def merge_subagent_entries(
    existing: Sequence[Mapping[str, Any]] | None,
    incoming: Sequence[Any],
    *,
    expected_job_id: str | None = None,
    started_ids: MutableSet[str] | None = None,
) -> list[dict[str, Any]]:
    """Merge accepted lifecycle snapshots without status regression.

    A child is admitted only after an explicit ``subagent.start`` record.  The
    caller may retain ``started_ids`` across polling reads; existing public
    records are also treated as already-started when no set is supplied.
    """
    records: dict[str, dict[str, Any]] = {}
    for raw in existing or []:
        validated = validate_progress_entry(
            {**dict(raw), "event": "subagent.start"},
            expected_job_id=expected_job_id,
            require_event=False,
        )
        if validated is None:
            # Existing public records have no event field and are trusted only
            # as already-admitted records after an earlier validation pass.
            if isinstance(raw, Mapping) and str(raw.get("subagent_id") or ""):
                candidate = {
                    key: raw.get(key)
                    for key in PUBLIC_SUBAGENT_FIELDS
                    if key in raw
                }
                if expected_job_id:
                    try:
                        expected = subagent_id_for(expected_job_id, int(candidate.get("task_index")))
                    except (TypeError, ValueError):
                        continue
                    if candidate.get("subagent_id") != expected:
                        continue
                records[_record_key(candidate)] = candidate
            continue
        records[_record_key(validated)] = {key: validated.get(key) for key in PUBLIC_SUBAGENT_FIELDS}

    admitted = started_ids if started_ids is not None else set(records)
    if started_ids is None:
        admitted.update(records)
    for raw in incoming:
        validated = validate_progress_entry(raw, expected_job_id=expected_job_id)
        if validated is None:
            continue
        key = _record_key(validated)
        event = validated.get("event")
        if event == "subagent.start":
            admitted.add(key)
        elif key not in admitted:
            continue
        public = {field: validated.get(field) for field in PUBLIC_SUBAGENT_FIELDS}
        records[key] = _merge_record(records[key], public) if key in records else public

    return sorted(
        records.values(),
        key=lambda record: (int(record.get("task_index") or 0), str(record.get("subagent_id") or "")),
    )


def public_subagent_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the public fields, never the internal event marker."""
    return {field: record.get(field) for field in PUBLIC_SUBAGENT_FIELDS}
