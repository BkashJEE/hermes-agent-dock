from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agent_dock_runner", ROOT / "backend" / "dashboard" / "dock_runner.py"
)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(runner)


class DockRunnerTests(unittest.TestCase):
    def test_catalog_mode_emits_machine_readable_json(self):
        output = io.StringIO()
        with patch.object(runner, "_catalog", return_value={"providers": []}), patch.object(
            sys, "stdout", output
        ):
            code = runner.main(["--catalog"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"providers": []})

    def test_chat_mode_reads_request_from_stdin_and_emits_session_marker(self):
        request = {
            "message": "private prompt",
            "provider": "openai-codex",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "high",
            "fast": True,
        }
        stdin = io.StringIO(json.dumps(request))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(runner, "_quiet_chat", return_value=("answer", "20260805_010203_a1b2c3")) as chat, patch.object(
            sys, "stdin", stdin
        ), patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            code = runner.main(["--chat"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "answer\n")
        self.assertEqual(stderr.getvalue(), "session_id: 20260805_010203_a1b2c3\n")
        self.assertEqual(chat.call_args.args[0]["message"], "private prompt")

    def test_runner_rejects_invalid_reasoning_without_starting_cli(self):
        with self.assertRaisesRegex(ValueError, "Invalid reasoning effort"):
            runner._quiet_chat(
                {
                    "message": "hello",
                    "model": "model",
                    "provider": "provider",
                    "reasoning_effort": "extreme",
                    "fast": False,
                }
            )

    def test_quiet_chat_returns_agent_reply_and_session(self):
        class FakeCLI:
            def __init__(self, **_kwargs):
                self.agent = types.SimpleNamespace(session_id="20260805_010203_reply")
                self._session_db = None

            def chat(self, message):
                self.seen = message
                return "Jarvis replied"

        cli_module = types.SimpleNamespace(HermesCLI=FakeCLI)
        constants_module = types.SimpleNamespace(parse_reasoning_effort=lambda value: value)
        request = {
            "message": "hello Jarvis",
            "model": "gpt-5.6-terra",
            "provider": "openai-codex",
            "reasoning_effort": "high",
            "fast": False,
        }
        with patch.dict(sys.modules, {"cli": cli_module, "hermes_constants": constants_module}):
            response, session_id = runner._quiet_chat(request)
        self.assertEqual(response, "Jarvis replied")
        self.assertEqual(session_id, "20260805_010203_reply")

    def test_diagnostic_tail_keeps_last_actionable_line(self):
        self.assertEqual(
            runner._diagnostic_tail("Initializing agent\nProvider rejected request"),
            "Provider rejected request",
        )

    def test_diagnostic_tail_redaction_failure_never_returns_raw_text(self):
        raw = "request failed with secret-token-value"
        with patch.dict(sys.modules, {"agent.redact": None}):
            detail = runner._diagnostic_tail(raw)
        self.assertEqual(detail, "Hermes runner failed; diagnostics unavailable")
        self.assertNotIn("secret-token-value", detail)


if __name__ == "__main__":
    unittest.main()
