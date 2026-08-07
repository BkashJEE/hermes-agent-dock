from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agent_dock_api", ROOT / "backend" / "dashboard" / "plugin_api.py"
)
api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(api)


PROFILE_ROWS = [
    {
        "name": "default",
        "is_default": True,
        "gateway_running": True,
        "model": "gpt-5.6-sol",
        "provider": "openai-codex",
        "description": "CEO",
    },
    {
        "name": "jarvis",
        "is_default": False,
        "gateway_running": False,
        "model": "gpt-5.6-terra",
        "provider": "openai-codex",
        "description": "CTO",
    },
]

CATALOG = {
    "provider": "openai-codex",
    "model": "gpt-5.6-terra",
    "providers": [
        {
            "slug": "openai-codex",
            "name": "OpenAI Codex",
            "models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
            "capabilities": {
                "gpt-5.6-sol": {"reasoning": True, "fast": False},
                "gpt-5.6-terra": {"reasoning": True, "fast": True},
                "gpt-5.6-luna": {"reasoning": False, "fast": False},
            },
        },
        {
            "slug": "nous",
            "name": "Nous Portal",
            "models": ["anthropic/claude-opus-5"],
            "capabilities": {
                "anthropic/claude-opus-5": {"reasoning": True, "fast": False}
            },
        },
    ],
}


class BackendSecurityTests(unittest.TestCase):
    def setUp(self):
        api._JOBS.clear()
        api._REQUEST_JOBS.clear()
        api._CATALOG_CACHE.clear()

    def request(self, **overrides):
        values = {
            "profile": "jarvis",
            "provider": "openai-codex",
            "model": "gpt-5.6-terra",
            "message": "hello",
            "request_id": None,
            "session_id": None,
            "reasoning_effort": "none",
            "fast": False,
            "assign_task": False,
        }
        values.update(overrides)
        return api.SendRequest(**values)

    def validation(self):
        return patch.multiple(
            api,
            _profile_rows=Mock(return_value=PROFILE_ROWS),
            _load_model_catalog=Mock(return_value=CATALOG),
        )

    def test_build_command_keeps_prompt_out_of_argv(self):
        hostile = "hello; rm -rf / && $(whoami)"
        with self.validation():
            command = api._build_command(self.request(message=hostile))
        self.assertEqual(command, [sys.executable, str(api._runner_path()), "--chat"])
        self.assertNotIn(hostile, command)
        self.assertNotIn("--yolo", command)
        self.assertNotIn("--accept-hooks", command)
        self.assertEqual(json.loads(api._runner_payload(self.request(message=hostile)))["message"], hostile)

    def test_resume_accepts_only_real_session_shape(self):
        with self.validation():
            api._build_command(self.request(session_id="20260805_010203_a1b2c3"))
            with self.assertRaisesRegex(ValueError, "Invalid Hermes session ID"):
                api._build_command(self.request(session_id="../../other-profile/config.yaml"))

    def test_profile_allowlist_and_message_bounds(self):
        with self.validation():
            with self.assertRaisesRegex(ValueError, "Unknown Hermes profile"):
                api._build_command(self.request(profile="unknown"))
            with self.assertRaisesRegex(ValueError, "exceeds"):
                api._build_command(self.request(message="x" * (api.MAX_MESSAGE_CHARS + 1)))
            with self.assertRaisesRegex(ValueError, "Invalid request ID"):
                api._build_command(self.request(request_id="short"))
        normalized = self.request(message="  hello  ")
        self.assertEqual(normalized.message, "hello")
        self.assertEqual(json.loads(api._runner_payload(normalized))["message"], "hello")
        with self.assertRaises(ValidationError):
            self.request(message=(" " * api.MAX_MESSAGE_CHARS) + "x")

    def test_assignment_requires_strict_boolean_and_request_id(self):
        with self.assertRaises(ValidationError):
            self.request(assign_task="yes")
        with self.assertRaises(ValidationError):
            self.request(assign_task=1)
        with self.assertRaises(api.HTTPException) as raised:
            api.create_job(self.request(assign_task=True, request_id=None))
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("request_id is required", raised.exception.detail)
        self.assertEqual(api._JOBS, {})

    def test_provider_and_model_are_catalog_scoped(self):
        with self.validation():
            api._build_command(self.request(model="gpt-5.6-luna"))
            api._build_command(
                self.request(provider="nous", model="anthropic/claude-opus-5")
            )
            with self.assertRaisesRegex(ValueError, "Provider is not present"):
                api._build_command(self.request(provider="unknown"))
            with self.assertRaisesRegex(ValueError, "Model is not configured"):
                api._build_command(self.request(model="unknown/model"))

    def test_reasoning_and_fast_are_capability_gated(self):
        with self.validation():
            api._build_command(self.request(reasoning_effort="ultra"))
            api._build_command(self.request(fast=True))
            with self.assertRaisesRegex(ValueError, "Invalid reasoning effort"):
                api._build_command(self.request(reasoning_effort="extreme"))
            with self.assertRaisesRegex(ValueError, "does not support reasoning"):
                api._build_command(self.request(model="gpt-5.6-luna", reasoning_effort="high"))
            with self.assertRaisesRegex(ValueError, "does not support fast"):
                api._build_command(self.request(model="gpt-5.6-sol", fast=True))

    def test_profile_home_and_environment_preserve_raw_profile_identity(self):
        root = Path("C:/HermesHome")
        with patch.object(api, "_root_home", return_value=root), patch.object(
            api, "_profile_rows", return_value=PROFILE_ROWS
        ), patch.object(api, "_child_env", return_value={"SAFE": "1"}):
            self.assertEqual(api._profile_home("default"), root)
            self.assertEqual(api._profile_home("jarvis"), root / "profiles" / "jarvis")
            env = api._catalog_environment("jarvis")
        self.assertEqual(env["HERMES_HOME"], str(root / "profiles" / "jarvis"))
        self.assertEqual(env["SAFE"], "1")

    def test_catalog_loader_uses_runner_without_shell(self):
        completed = Mock(returncode=0, stdout=json.dumps(CATALOG), stderr="")
        with patch.object(api, "_normalize_profile", return_value="jarvis"), patch.object(
            api, "_catalog_environment", return_value={"HERMES_HOME": "C:/profile"}
        ), patch.object(api.subprocess, "run", return_value=completed) as run:
            result = api._load_model_catalog("jarvis")
        self.assertEqual(result["providers"][0]["slug"], "openai-codex")
        kwargs = run.call_args.kwargs
        self.assertFalse(kwargs["shell"])
        self.assertEqual(run.call_args.args[0][-1], "--catalog")

    def test_models_route_is_synchronous_for_blocking_catalog_load(self):
        self.assertFalse(inspect.iscoroutinefunction(api.models))
        self.assertFalse(inspect.iscoroutinefunction(api.create_job))
        with patch.object(api, "_normalize_profile", return_value="jarvis"), patch.object(
            api, "_load_model_catalog", return_value=CATALOG
        ):
            self.assertEqual(api.models("jarvis"), CATALOG)

    def test_public_job_removes_process_handle(self):
        self.assertEqual(
            api._public_job({"id": "1", "_process": object(), "status": "done"}),
            {"id": "1", "status": "done"},
        )

    def test_assignment_creates_idempotent_profile_owned_kanban_task(self):
        connection = Mock()
        kanban = Mock()
        kanban.connect.return_value = connection
        kanban.create_task.return_value = "t_agentdock"
        request = self.request(
            message="Task: Repair the reply path",
            request_id="req-assignment-123",
            assign_task=True,
        )
        with patch.object(api, "_kanban_module", return_value=kanban), patch.object(
            api, "_kanban_workspace", return_value=Path("C:/organization")
        ):
            task_id = api._create_kanban_task(request, "job-1")
        self.assertEqual(task_id, "t_agentdock")
        kwargs = kanban.create_task.call_args.kwargs
        self.assertEqual(kwargs["title"], "Repair the reply path")
        self.assertEqual(kwargs["assignee"], "jarvis")
        self.assertEqual(kwargs["board"], "executive-organization")
        self.assertEqual(kwargs["initial_status"], "running")
        self.assertEqual(kwargs["idempotency_key"], "agent-dock:jarvis:req-assignment-123")
        connection.close.assert_called_once()

    def test_runner_error_surfaces_sanitized_actionable_tail(self):
        error = api._safe_runner_error(
            "session_id: 20260805_010203_a1b2c3\nAgent Dock runner error: provider rejected request\n",
            1,
        )
        self.assertEqual(error, "Agent session failed: Agent Dock runner error: provider rejected request")

    def test_runner_error_redaction_failure_never_returns_raw_diagnostic(self):
        raw = "provider failed with secret-token-value"
        with patch.dict(sys.modules, {"agent.redact": None}):
            error = api._safe_runner_error(raw, 1)
        self.assertEqual(error, "Agent session failed: Hermes runner failed; inspect local Hermes logs")
        self.assertNotIn("secret-token-value", error)

    def test_run_job_pipes_exact_json_request_to_runner_stdin(self):
        class FakeProcess:
            returncode = 0

            def __init__(self):
                self.received = None

            def communicate(self, input=None, timeout=None):
                self.received = input
                self.timeout = timeout
                return "agent reply", "session_id: 20260805_010203_a1b2c3\n"

        process = FakeProcess()
        request = self.request(message="live transport probe")
        api._JOBS["job-transport"] = {
            "id": "job-transport",
            "status": "starting",
            "kanban_task_id": None,
        }
        with patch.object(api, "_build_command", return_value=[sys.executable, "runner", "--chat"]), patch.object(
            api, "_catalog_environment", return_value={"HERMES_HOME": "C:/profile"}
        ), patch.object(api.subprocess, "Popen", return_value=process) as popen:
            api._run_job("job-transport", request)

        self.assertIs(popen.call_args.kwargs["stdin"], api.subprocess.PIPE)
        self.assertEqual(json.loads(process.received)["message"], "live transport probe")
        self.assertEqual(api._JOBS["job-transport"]["status"], "done")
        self.assertEqual(api._JOBS["job-transport"]["response"], "agent reply")

    def test_cancellation_during_command_build_prevents_process_spawn(self):
        request = self.request(message="cancel before spawn")
        api._JOBS["job-cancel-build"] = {
            "id": "job-cancel-build",
            "status": "starting",
            "kanban_task_id": None,
        }

        def cancel_during_build(_request):
            api._JOBS["job-cancel-build"]["status"] = "cancelled"
            return [sys.executable, "runner", "--chat"]

        with patch.object(api, "_build_command", side_effect=cancel_during_build), patch.object(
            api, "_catalog_environment", return_value={"HERMES_HOME": "C:/profile"}
        ), patch.object(api.subprocess, "Popen") as popen:
            api._run_job("job-cancel-build", request)

        popen.assert_not_called()
        self.assertEqual(api._JOBS["job-cancel-build"]["status"], "cancelled")

    def test_cancellation_between_spawn_and_attach_terminates_process(self):
        process = Mock()
        request = self.request(message="cancel after spawn")
        api._JOBS["job-cancel-attach"] = {
            "id": "job-cancel-attach",
            "status": "starting",
            "kanban_task_id": None,
        }

        def spawn_then_cancel(**_kwargs):
            api._JOBS["job-cancel-attach"]["status"] = "cancelled"
            return process

        with patch.object(api, "_build_command", return_value=[sys.executable, "runner", "--chat"]), patch.object(
            api, "_catalog_environment", return_value={"HERMES_HOME": "C:/profile"}
        ), patch.object(api.subprocess, "Popen", side_effect=spawn_then_cancel), patch.object(
            api, "_terminate_process"
        ) as terminate:
            api._run_job("job-cancel-attach", request)

        terminate.assert_called_once_with(process)
        process.communicate.assert_not_called()
        self.assertEqual(api._JOBS["job-cancel-attach"]["status"], "cancelled")

    def test_terminal_success_cannot_overwrite_cancellation(self):
        api._JOBS["job-race"] = {"id": "job-race", "status": "cancelled", "finished_at": 1}
        self.assertFalse(api._begin_job_finalization("job-race", "late reply", None))
        self.assertEqual(api._JOBS["job-race"]["status"], "cancelled")
        self.assertNotIn("response", api._JOBS["job-race"])

        api._JOBS["job-win"] = {"id": "job-win", "status": "running"}
        self.assertTrue(api._begin_job_finalization("job-win", "reply", "20260805_010203_a1b2c3"))
        self.assertEqual(api._JOBS["job-win"]["status"], "finalizing")
        finalizing = asyncio.run(api.cancel_job("job-win"))
        self.assertEqual(finalizing["status"], "finalizing")
        self.assertTrue(api._publish_job_success("job-win"))
        self.assertEqual(api._JOBS["job-win"]["status"], "done")
        completed = asyncio.run(api.cancel_job("job-win"))
        self.assertEqual(completed["status"], "done")

    def test_posix_termination_waits_then_escalates_process_group(self):
        class WaitTimeout(Exception):
            pass

        class FakeProcess:
            pid = 42

            def __init__(self):
                self.waits = 0

            def poll(self):
                return None

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise WaitTimeout()
                return -9

        process = FakeProcess()
        with patch.object(api.os, "getpgid", return_value=42, create=True), patch.object(
            api.os, "killpg", create=True
        ) as killpg, patch.object(
            api.signal, "SIGKILL", 9, create=True
        ), patch.object(
            api.subprocess, "TimeoutExpired", WaitTimeout
        ):
            api._terminate_process(process, platform="posix")
        self.assertEqual(
            killpg.call_args_list,
            [unittest.mock.call(42, api.signal.SIGTERM), unittest.mock.call(42, 9)],
        )
        self.assertEqual(process.waits, 2)

    def test_completed_job_retention_is_bounded_and_cleans_request_ids(self):
        now = 10_000
        api._JOBS.update(
            {
                "stale": {"status": "done", "finished_at": now - api.JOB_RETENTION_SECONDS - 1},
                "old": {"status": "error", "finished_at": now - 2},
                "recent": {"status": "cancelled", "finished_at": now - 1},
                "active": {"status": "running", "finished_at": None},
            }
        )
        api._REQUEST_JOBS.update(
            {"jarvis:stale-request": "stale", "jarvis:recent-request": "recent"}
        )
        with patch.object(api, "MAX_RETAINED_JOBS", 1):
            api._evict_completed_jobs(now=now)
        self.assertEqual(set(api._JOBS), {"recent", "active"})
        self.assertNotIn("jarvis:stale-request", api._REQUEST_JOBS)
        self.assertEqual(api._REQUEST_JOBS["jarvis:recent-request"], "recent")

    def test_agent_reply_is_recorded_for_review_not_auto_completed(self):
        connection = Mock()
        kanban = Mock()
        kanban.connect.return_value = connection
        with patch.object(api, "_kanban_module", return_value=kanban):
            api._settle_kanban_task("t_agentdock", "done", "Implemented with tests", "job-1")
        kanban.add_comment.assert_called_once()
        kanban.block_task.assert_called_once_with(
            connection,
            "t_agentdock",
            reason="Agent Dock response captured; awaiting Dad/CEO verification before completion.",
            kind="needs_input",
        )
        kanban.complete_task.assert_not_called()
        connection.close.assert_called_once()

    @patch.object(api, "_profile_rows", return_value=[])
    def test_profiles_advertises_native_controls(self, _rows):
        result = asyncio.run(api.profiles())
        self.assertTrue(result["capabilities"]["model_catalog"])
        self.assertTrue(result["capabilities"]["reasoning"])
        self.assertTrue(result["capabilities"]["fast"])
        self.assertTrue(result["capabilities"]["idempotent_submit"])
        self.assertTrue(result["capabilities"]["kanban_assignment"])
        self.assertEqual(result["capabilities"]["kanban_board"], "executive-organization")

    @patch.object(api, "_build_command", return_value=[sys.executable, "runner", "--chat"])
    @patch.object(api.threading, "Thread")
    def test_duplicate_request_id_reuses_job_without_second_thread(self, thread_cls, _command):
        request = self.request(request_id="req-12345678")
        first = api.create_job(request)
        second = api.create_job(request)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(thread_cls.call_count, 1)
        self.assertEqual(first["reasoning_effort"], "none")
        self.assertEqual(first["provider"], "openai-codex")

    @patch.object(api.threading, "Thread")
    def test_assignment_job_returns_real_kanban_identity(self, thread_cls):
        request = self.request(request_id="req-kanban-job-123", assign_task=True)
        with self.validation(), patch.object(
            api, "_create_kanban_task", return_value="t_realcard"
        ):
            job = api.create_job(request)
        self.assertEqual(job["kanban_task_id"], "t_realcard")
        self.assertEqual(job["kanban_board"], "executive-organization")
        thread_cls.return_value.start.assert_called_once()

    @patch.object(api.threading, "Thread")
    def test_duplicate_request_id_resolves_before_catalog_revalidation(self, thread_cls):
        request = self.request(request_id="req-available-before-catalog")
        with patch.object(api, "_build_command", return_value=[sys.executable, "runner", "--chat"]):
            first = api.create_job(request)
        with patch.object(api, "_build_command", side_effect=RuntimeError("catalog unavailable")) as command:
            second = api.create_job(request)
        self.assertEqual(first["id"], second["id"])
        command.assert_not_called()
        self.assertEqual(thread_cls.call_count, 1)

    def test_achievement_projection_omits_private_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            snapshot = home / "plugins" / "hermes-achievements" / "scan_snapshot.json"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text(
                json.dumps(
                    {
                        "generated_at": 42,
                        "unlocked_count": 1,
                        "total_count": 60,
                        "achievements": [
                            {
                                "id": "proof_first",
                                "name": "Proof First",
                                "description": "Verify the artifact.",
                                "category": "Agent Autonomy",
                                "tier": "Gold",
                                "unlocked": True,
                                "unlocked_at": 41,
                                "evidence": ["PRIVATE SESSION TITLE"],
                                "session_id": "secret-session",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(api, "get_hermes_home", return_value=home):
                result = asyncio.run(api.achievements())
            serialized = json.dumps(result)
            self.assertTrue(result["available"])
            self.assertNotIn("PRIVATE SESSION TITLE", serialized)
            self.assertNotIn("secret-session", serialized)
            self.assertNotIn("evidence", result["items"][0])


if __name__ == "__main__":
    unittest.main()
