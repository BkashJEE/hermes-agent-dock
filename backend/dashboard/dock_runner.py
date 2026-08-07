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
import sys
from collections.abc import Mapping
from typing import Any

MAX_TURNS = 120
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
    from hermes_cli.inventory import build_model_options_payload, load_picker_context

    payload = build_model_options_payload(load_picker_context(), explicit_only=True)
    if not isinstance(payload, dict):
        raise RuntimeError("Hermes model catalog returned an invalid payload")
    return payload


def _request_from_stdin() -> dict[str, Any]:
    try:
        raw = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Agent Dock request must be one JSON object") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("Agent Dock request must be one JSON object")
    return dict(raw)


def _quiet_chat(request: Mapping[str, Any]) -> tuple[str, str]:
    message = request.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Agent Dock request message is empty")

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
    cli.reasoning_config = parse_reasoning_effort(effort)
    cli.service_tier = bool(fast)

    # HermesCLI.chat renders its response and status chrome for interactive
    # callers.  Capture both streams while it runs, then emit the API contract
    # below: response on stdout and one parseable session line on stderr.
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            response = cli.chat(message)
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
