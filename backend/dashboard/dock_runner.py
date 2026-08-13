"""Profile-scoped Hermes execution runner for Agent Dock.

The dashboard backend starts this module with ``sys.executable`` and passes
request data over stdin.  Keeping prompts and model selection out of argv
avoids leaking them through process listings and makes the profile boundary
explicit through ``HERMES_HOME``.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_TURNS = 120
MAX_IMAGE_ATTACHMENTS = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
VALID_REASONING_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)


def _subagent_progress_callback(request: Mapping[str, Any]):
    """Return a privacy-reduced JSONL callback for delegation events."""
    progress_path = request.get("subagent_progress_path")
    job_id = request.get("job_id")
    if not isinstance(progress_path, str) or not progress_path or not isinstance(job_id, str):
        return None

    try:
        from .subagent_progress import SAFE_CURRENT_TOOLS, SUBAGENT_EVENTS, subagent_id_for
    except ImportError:
        # ``dock_runner.py`` is also executed as a standalone script by the
        # profile-scoped subprocess, and tests load it through a file spec;
        # neither path has a package context or guarantees this directory is
        # already present on sys.path.
        dashboard_root = str(Path(__file__).resolve().parent)
        if dashboard_root not in sys.path:
            sys.path.insert(0, dashboard_root)
        from subagent_progress import SAFE_CURRENT_TOOLS, SUBAGENT_EVENTS, subagent_id_for

    path = Path(progress_path).resolve()
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").resolve()
    allowed_root = home / "cache" / "agent-dock-progress"
    try:
        path.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError("Subagent progress path escaped the profile boundary") from exc

    lock = threading.Lock()
    started_at: dict[int, float] = {}

    def callback(event_type: str, tool_name: str | None = None, _preview=None, _args=None, **kwargs):
        event = str(event_type or "").strip().lower()
        if event not in SUBAGENT_EVENTS:
            return
        task_index = kwargs.get("task_index")
        if isinstance(task_index, bool) or not isinstance(task_index, int) or task_index < 0:
            return
        now = time.time()
        if event == "subagent.start":
            started_at.setdefault(task_index, now)
        elif task_index not in started_at:
            return
        raw_status = str(kwargs.get("status") or "").strip().lower()
        status = "running"
        if event == "subagent.complete":
            status = raw_status if raw_status in {"completed", "failed", "interrupted"} else "completed"
        record = {
            "event": event,
            "subagent_id": subagent_id_for(job_id, task_index),
            "task_index": task_index,
            "status": status,
            "started_at": started_at[task_index],
            "updated_at": now,
            "finished_at": now if status != "running" else None,
            "current_tool": tool_name if event == "subagent.tool" and tool_name in SAFE_CURRENT_TOOLS else None,
            "duration_seconds": max(0, now - started_at[task_index]),
            "model": None,
            "api_calls": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "usage_state": "unavailable",
            "direct_chat_available": False,
        }
        if event == "subagent.complete":
            model = kwargs.get("model")
            input_tokens = kwargs.get("input_tokens")
            output_tokens = kwargs.get("output_tokens")
            api_calls = kwargs.get("api_calls")
            if isinstance(model, str) and model.strip():
                record["model"] = model.strip()
            if all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in (input_tokens, output_tokens)
            ):
                record["input_tokens"] = input_tokens
                record["output_tokens"] = output_tokens
                record["total_tokens"] = input_tokens + output_tokens
                record["usage_state"] = "reported"
            if isinstance(api_calls, int) and not isinstance(api_calls, bool) and api_calls >= 0:
                record["api_calls"] = api_calls
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n"
        with lock:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()

    return callback


def _diagnostic_tail(*streams: str, limit: int = 360) -> str:
    lines = [line.strip() for stream in streams for line in stream.splitlines() if line.strip()]
    detail = lines[-1] if lines else ""
    try:
        from agent.redact import redact_sensitive_text

        detail = redact_sensitive_text(detail)
    except Exception:
        detail = "Hermes runner failed; diagnostics unavailable"
    return detail[-limit:]


def _catalog() -> dict[str, Any]:
    """Return the configured provider and its Hermes-available alternatives."""
    from hermes_cli.inventory import build_models_payload, load_picker_context

    context = load_picker_context()
    provider = str(context.current_provider or "").strip()
    model = str(context.current_model or "").strip()
    payload = build_models_payload(
        context,
        explicit_only=True,
        capabilities=True,
        for_picker=True,
        probe_custom_providers=False,
        probe_current_custom_provider=True,
    )
    source_row = next(
        (
            row
            for row in payload.get("providers", [])
            if isinstance(row, dict) and str(row.get("slug") or "").strip() == provider
        ),
        None,
    )

    providers: list[dict[str, Any]] = []
    if provider:
        raw_models = source_row.get("models", []) if source_row else []
        models = list(
            dict.fromkeys(
                str(entry).strip()
                for entry in raw_models
                if isinstance(entry, str) and str(entry).strip()
            )
        )
        if model and model not in models:
            models.insert(0, model)

        raw_capabilities = source_row.get("capabilities", {}) if source_row else {}
        capabilities: dict[str, dict[str, bool]] = {}
        for model_id in models:
            raw = raw_capabilities.get(model_id, {}) if isinstance(raw_capabilities, dict) else {}
            capabilities[model_id] = {
                "reasoning": bool(raw.get("reasoning")) if isinstance(raw, dict) else False,
                "fast": bool(raw.get("fast")) if isinstance(raw, dict) else False,
            }

        providers.append(
            {
                "slug": provider,
                "name": str(source_row.get("name") or provider) if source_row else provider,
                "models": models,
                "total_models": len(models),
                "capabilities": capabilities,
            }
        )
    return {"providers": providers, "model": model, "provider": provider}


def _request_from_stdin() -> dict[str, Any]:
    try:
        raw = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Agent Dock request must be one JSON object") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("Agent Dock request must be one JSON object")
    return dict(raw)


def _request_image_paths(request: Mapping[str, Any]) -> list[Path]:
    raw_images = request.get("images") or []
    if not isinstance(raw_images, list) or len(raw_images) > MAX_IMAGE_ATTACHMENTS:
        raise ValueError("Invalid Agent Dock image list")
    if not raw_images:
        return []
    configured_home = (os.environ.get("HERMES_HOME") or "").strip()
    if not configured_home:
        raise ValueError("Hermes profile home is unavailable for image attachments")
    allowed_root = (Path(configured_home).resolve() / "images" / "agent-dock").resolve()
    images: list[Path] = []
    for raw_path in raw_images:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("Invalid Agent Dock image path")
        try:
            path = Path(raw_path).resolve(strict=True)
        except OSError as exc:
            raise ValueError("Agent Dock image is unavailable") from exc
        if allowed_root != path.parent and allowed_root not in path.parents:
            raise ValueError("Agent Dock image escaped the profile image directory")
        if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("Agent Dock image is unavailable or too large")
        images.append(path)
    return images


def _quiet_chat(request: Mapping[str, Any]) -> tuple[str, str]:
    images = _request_image_paths(request)
    message = request.get("message")
    if not isinstance(message, str):
        raise ValueError("Agent Dock request message must be a string")
    message = message.strip()
    if not message and not images:
        raise ValueError("Agent Dock request message is empty")
    if not message:
        message = "Please analyze the attached image or images."

    model = request.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError("Agent Dock request model must be a string or null")
    provider = request.get("provider")
    if provider is not None and not isinstance(provider, str):
        raise ValueError("Agent Dock request provider must be a string or null")
    session_id = request.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("Agent Dock request session_id must be a string or null")

    effort = request.get("reasoning_effort", "none")
    if effort is None:
        effort = "none"
    if not isinstance(effort, str) or effort not in VALID_REASONING_EFFORTS:
        raise ValueError("Invalid reasoning effort")

    fast = request.get("fast", False)
    if not isinstance(fast, bool):
        raise ValueError("Agent Dock request fast must be a boolean")

    from cli import HermesCLI
    from hermes_constants import parse_reasoning_effort

    cli = HermesCLI(
        model=model,
        provider=provider,
        resume=session_id,
        max_turns=MAX_TURNS,
        verbose=False,
    )
    # Hermes' existing -Q/--quiet path is represented on a direct CLI object by
    # disabling tool progress.  The agent created by _init_agent inherits the
    # quiet flags from this value and verbose=False.
    cli.tool_progress_mode = "off"
    progress_callback = _subagent_progress_callback(request)
    if progress_callback is not None:
        # HermesCLI._init_agent snapshots this method into AIAgent. Child
        # delegation callbacks then relay authoritative subagent.* events here.
        cli._on_tool_progress = progress_callback
    cli.reasoning_config = parse_reasoning_effort(effort)
    cli.service_tier = bool(fast)

    # HermesCLI.chat renders its response and status chrome for interactive
    # callers.  Capture both streams while it runs, then emit the API contract
    # below: response on stdout and one parseable session line on stderr.
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            response = cli.chat(message, images=images or None)
        if response is None:
            detail = _diagnostic_tail(captured_stderr.getvalue(), captured_stdout.getvalue())
            raise RuntimeError(f"Hermes returned no response{f': {detail}' if detail else ''}")
        session_id = getattr(getattr(cli, "agent", None), "session_id", None) or getattr(cli, "session_id", None)
        if not session_id:
            detail = _diagnostic_tail(captured_stderr.getvalue(), captured_stdout.getvalue())
            raise RuntimeError(f"Hermes returned no session id{f': {detail}' if detail else ''}")
        return str(response), str(session_id)
    finally:
        _cleanup_cli(cli)


def _cleanup_cli(cli: Any) -> None:
    """Best-effort cleanup for direct HermesCLI use without interactive run()."""
    try:
        persist = getattr(cli, "_persist_active_session_before_close", None)
        if callable(persist):
            persist()
    except Exception:
        pass

    try:
        agent = getattr(cli, "agent", None)
        session_db = getattr(cli, "_session_db", None)
        session_id = getattr(agent, "session_id", None) or getattr(cli, "session_id", None)
        if session_db is not None and session_id:
            end_session = getattr(session_db, "end_session", None)
            if callable(end_session):
                end_session(session_id, "dock_close")
    except Exception:
        pass

    try:
        release = getattr(cli, "_release_active_session", None)
        if callable(release):
            release()
    except Exception:
        pass

    try:
        session_db = getattr(cli, "_session_db", None)
        close = getattr(session_db, "close", None)
        if callable(close):
            close()
    except Exception:
        pass

    try:
        from agent.auxiliary_client import cleanup_stale_async_clients

        cleanup_stale_async_clients()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Dock Hermes runner")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--catalog", action="store_true", help="Print the installed Hermes model catalog as JSON")
    modes.add_argument("--chat", action="store_true", help="Read one chat request JSON object from stdin")
    args = parser.parse_args(argv)

    try:
        if args.catalog:
            json.dump(_catalog(), sys.stdout, ensure_ascii=False, separators=(",", ":"))
            sys.stdout.write("\n")
            return 0

        request = _request_from_stdin()
        response, session_id = _quiet_chat(request)
        sys.stdout.write(response.strip())
        sys.stdout.write("\n")
        print(f"session_id: {session_id}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"Agent Dock runner error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
