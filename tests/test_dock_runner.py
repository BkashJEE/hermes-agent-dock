from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
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
    def test_catalog_contains_only_the_profile_provider_with_available_models(self):
        context = types.SimpleNamespace(current_provider="custom:local", current_model="qwen-profile")
        payload = {
            "providers": [
                {
                    "slug": "custom:local",
                    "name": "Local Qwen",
                    "models": ["qwen-fast", "qwen-profile"],
                    "capabilities": {
                        "qwen-fast": {"reasoning": False, "fast": True},
                        "qwen-profile": {"reasoning": True, "fast": False},
                    },
                },
                {"slug": "openrouter", "models": ["unrelated/model"]},
            ]
        }
        build_calls = []

        def build_models_payload(received_context, **kwargs):
            build_calls.append((received_context, kwargs))
            return payload

        with patch.dict(
            sys.modules,
            {
                "hermes_cli.inventory": types.SimpleNamespace(
                    load_picker_context=lambda: context,
                    build_models_payload=build_models_payload,
                ),
            },
        ):
            catalog = runner._catalog()
        self.assertEqual(catalog["model"], "qwen-profile")
        self.assertEqual(catalog["provider"], "custom:local")
        self.assertEqual(len(catalog["providers"]), 1)
        self.assertEqual(catalog["providers"][0]["models"], ["qwen-fast", "qwen-profile"])
        self.assertEqual(catalog["providers"][0]["name"], "Local Qwen")
        self.assertEqual(catalog["providers"][0]["capabilities"]["qwen-fast"]["fast"], True)
        self.assertEqual(build_calls[0][0], context)
        self.assertEqual(
            build_calls[0][1],
            {
                "explicit_only": True,
                "capabilities": True,
                "for_picker": True,
                "probe_custom_providers": False,
                "probe_current_custom_provider": True,
            },
        )

    def test_catalog_keeps_saved_model_when_provider_discovery_is_unavailable(self):
        context = types.SimpleNamespace(current_provider="custom:offline", current_model="saved-model")
        with patch.dict(
            sys.modules,
            {
                "hermes_cli.inventory": types.SimpleNamespace(
                    load_picker_context=lambda: context,
                    build_models_payload=lambda *_args, **_kwargs: {"providers": []},
                ),
            },
        ):
            catalog = runner._catalog()
        self.assertEqual(catalog["providers"][0]["models"], ["saved-model"])

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

            def chat(self, message, images=None):
                self.seen = message
                self.seen_images = images
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

    def test_quiet_chat_passes_only_profile_scoped_image_paths(self):
        instances = []

        class FakeCLI:
            def __init__(self, **_kwargs):
                self.agent = types.SimpleNamespace(session_id="20260805_010203_image")
                self._session_db = None
                instances.append(self)

            def chat(self, message, images=None):
                self.seen_message = message
                self.seen_images = images
                return "image received"

        cli_module = types.SimpleNamespace(HermesCLI=FakeCLI)
        constants_module = types.SimpleNamespace(parse_reasoning_effort=lambda value: value)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            image = home / "images" / "agent-dock" / "job" / "image-1.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
            request = {"message": "", "images": [str(image)], "reasoning_effort": "none", "fast": False}
            with patch.dict(sys.modules, {"cli": cli_module, "hermes_constants": constants_module}), patch.dict(
                os.environ, {"HERMES_HOME": str(home)}
            ):
                response, _session_id = runner._quiet_chat(request)
            self.assertEqual(response, "image received")
            self.assertEqual(instances[0].seen_message, "Please analyze the attached image or images.")
            self.assertEqual(instances[0].seen_images, [image.resolve()])

            outside = home / "outside.png"
            outside.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
                with self.assertRaisesRegex(ValueError, "escaped"):
                    runner._request_image_paths({"images": [str(outside)]})

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

    def test_subagent_callback_requires_start_and_writes_only_public_lifecycle_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            progress_path = home / "cache" / "agent-dock-progress" / "job-123.jsonl"
            progress_path.parent.mkdir(parents=True)
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
                callback = runner._subagent_progress_callback(
                    {"job_id": "job-123", "subagent_progress_path": str(progress_path)}
                )
                self.assertIsNotNone(callback)
                callback(
                    "subagent.tool",
                    "terminal",
                    "PRIVATE PREVIEW",
                    {"command": "PRIVATE ARGS"},
                    task_index=0,
                    goal="PRIVATE GOAL",
                    prompt="PRIVATE PROMPT",
                    summary="PRIVATE SUMMARY",
                    reasoning="PRIVATE REASONING",
                    model="private-model",
                    api_calls=7,
                    tokens={"input": 11, "output": 13},
                )
                callback(
                    "subagent.start",
                    task_index=0,
                    goal="PRIVATE GOAL",
                    prompt="PRIVATE PROMPT",
                    summary="PRIVATE SUMMARY",
                    reasoning="PRIVATE REASONING",
                    model="private-model",
                    api_calls=7,
                    tokens={"input": 11, "output": 13},
                )
                callback(
                    "subagent.tool",
                    "terminal",
                    "PRIVATE PREVIEW",
                    {"command": "PRIVATE ARGS"},
                    task_index=0,
                    goal="PRIVATE GOAL",
                    result="PRIVATE RESULT",
                    path="C:/private/path",
                    credentials="PRIVATE CREDENTIAL",
                )
                callback(
                    "subagent.progress",
                    task_index=0,
                    preview="PRIVATE PROGRESS",
                    summary="PRIVATE SUMMARY",
                    reasoning="PRIVATE REASONING",
                )
                callback(
                    "subagent.complete",
                    task_index=0,
                    status="completed",
                    summary="PRIVATE SUMMARY",
                    model="gpt-5.6-luna",
                    api_calls=7,
                    input_tokens=11,
                    output_tokens=13,
                )

            rows = [json.loads(line) for line in progress_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["event"] for row in rows], [
                "subagent.start",
                "subagent.tool",
                "subagent.progress",
                "subagent.complete",
            ])
            public_fields = {
                "event",
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
            for row in rows:
                self.assertEqual(set(row), public_fields)
                self.assertNotIn("PRIVATE", json.dumps(row))
                self.assertFalse(row["direct_chat_available"])
            self.assertEqual(rows[0]["subagent_id"], "job-123:subagent:0")
            self.assertEqual(rows[1]["current_tool"], "terminal")
            self.assertIsNone(rows[2]["current_tool"])
            self.assertEqual(rows[-1]["model"], "gpt-5.6-luna")
            self.assertEqual(rows[-1]["api_calls"], 7)
            self.assertEqual(rows[-1]["input_tokens"], 11)
            self.assertEqual(rows[-1]["output_tokens"], 13)
            self.assertEqual(rows[-1]["total_tokens"], 24)
            self.assertEqual(rows[-1]["usage_state"], "reported")

    def test_subagent_callback_rejects_progress_path_outside_profile_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            outside = home / "outside.jsonl"
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
                with self.assertRaisesRegex(ValueError, "escaped"):
                    runner._subagent_progress_callback(
                        {"job_id": "job-123", "subagent_progress_path": str(outside)}
                    )

    def test_subagent_completion_maps_timeout_and_cancel_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            progress_path = home / "cache" / "agent-dock-progress" / "job-status.jsonl"
            progress_path.parent.mkdir(parents=True)
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
                callback = runner._subagent_progress_callback(
                    {"job_id": "job-status", "subagent_progress_path": str(progress_path)}
                )
                callback("subagent.start", task_index=0)
                callback("subagent.complete", task_index=0, status="timeout")
                callback("subagent.start", task_index=1)
                callback("subagent.complete", task_index=1, status="cancelled")
            rows = [json.loads(line) for line in progress_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[1]["status"], "failed")
            self.assertEqual(rows[3]["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
