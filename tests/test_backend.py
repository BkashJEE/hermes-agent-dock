from __future__ import annotations

import asyncio
import base64
import importlib.util
import inspect
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

REAL_THREAD = threading.Thread

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
                "gpt-5.6-sol": {"reasoning": True, "fast": True},
                "gpt-5.6-terra": {"reasoning": True, "fast": True},
                "gpt-5.6-luna": {"reasoning": True, "fast": True},
            },
        },
    ],
}


class BackendSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        self.home_patch = patch.object(api, "get_hermes_home", return_value=self.home)
        self.home_patch.start()
        api._reset_job_store_for_tests()
        api._JOBS.clear()
        api._REQUEST_JOBS.clear()
        api._CATALOG_CACHE.clear()

    def tearDown(self):
        api._reset_job_store_for_tests()
        self.home_patch.stop()
        self.temp_dir.cleanup()

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
            "images": [],
        }
        values.update(overrides)
        return api.SendRequest(**values)

    def validation(self):
        return patch.multiple(
            api,
            _profile_rows=Mock(return_value=PROFILE_ROWS),
            _load_model_catalog=Mock(return_value=CATALOG),
        )

    def seed_durable_job(self, job_id, request):
        row, created = api._job_store().reserve_job(
            job_id=job_id,
            profile_id=request.profile,
            request_id=request.request_id,
            provider=request.provider,
            model=request.model,
            reasoning_effort=request.reasoning_effort,
            fast=request.fast,
            assign_task=request.assign_task,
            image_count=len(request.images),
            session_id=request.session_id,
            kanban_task_id=None,
            kanban_board=None,
        )
        self.assertTrue(created)
        api._JOBS[job_id] = api._memory_job_from_row(row)
        return api._JOBS[job_id]

    def cancel_seeded_job(self, job_id):
        job = api._JOBS[job_id]
        token = job["_attempt_token"]
        self.assertTrue(api._job_store().request_cancel(job_id, token))
        self.assertTrue(api._job_store().complete_cancelled(job_id, token))
        job.update({"status": "cancelled", "finished_at": 1})

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
            with self.assertRaisesRegex(ValueError, "Message is empty"):
                api._build_command(self.request(message="   "))
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

    def test_provider_and_model_are_profile_scoped(self):
        with self.validation():
            api._build_command(self.request())
            api._build_command(self.request(model="gpt-5.6-sol"))
            payload = json.loads(api._runner_payload(self.request(model="gpt-5.6-luna")))
            self.assertEqual(payload["provider"], "openai-codex")
            self.assertEqual(payload["model"], "gpt-5.6-luna")
            with self.assertRaisesRegex(ValueError, "Provider is not present"):
                api._build_command(self.request(provider="nous", model="anthropic/claude-opus-5"))
            with self.assertRaisesRegex(ValueError, "Model is not configured"):
                api._build_command(self.request(model="unknown/model"))

    def test_reasoning_and_fast_are_capability_gated(self):
        with self.validation():
            api._build_command(self.request(reasoning_effort="ultra"))
            api._build_command(self.request(fast=True))
            with self.assertRaisesRegex(ValueError, "Invalid reasoning effort"):
                api._build_command(self.request(reasoning_effort="extreme"))
        unsupported = {
            **CATALOG,
            "providers": [{**CATALOG["providers"][0], "capabilities": {"gpt-5.6-terra": {}}}],
        }
        with patch.multiple(
            api,
            _profile_rows=Mock(return_value=PROFILE_ROWS),
            _load_model_catalog=Mock(return_value=unsupported),
        ):
            with self.assertRaisesRegex(ValueError, "does not support reasoning"):
                api._build_command(self.request(reasoning_effort="high"))
            with self.assertRaisesRegex(ValueError, "does not support fast"):
                api._build_command(self.request(fast=True))

    def test_image_payload_is_signature_checked_bounded_and_can_be_image_only(self):
        png = b"\x89PNG\r\n\x1a\ncontent"
        attachment = {
            "name": "diagram.png",
            "mime_type": "image/png",
            "data_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        }
        request = self.request(message="", images=[attachment])
        with self.validation():
            api._build_command(request)
        decoded, extension = api._decode_image_attachment(request.images[0])
        self.assertEqual(decoded, png)
        self.assertEqual(extension, ".png")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            api._decode_image_attachment(
                api.ImageAttachment(
                    name="fake.png",
                    mime_type="image/png",
                    data_url="data:image/png;base64," + base64.b64encode(b"not-an-image").decode("ascii"),
                )
            )
        with self.assertRaises(ValidationError):
            self.request(images=[attachment] * (api.MAX_IMAGE_ATTACHMENTS + 1))

    def test_runner_payload_contains_paths_not_image_bytes_and_temp_files_are_removed(self):
        png = b"\x89PNG\r\n\x1a\ncontent"
        request = self.request(
            images=[
                {
                    "name": "diagram.png",
                    "mime_type": "image/png",
                    "data_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                }
            ]
        )

        class FakeProcess:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                self.received = input
                return "image reply", "session_id: 20260805_010203_image\n"

        process = FakeProcess()
        self.seed_durable_job("job-image", request)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(api, "_build_command", return_value=[sys.executable, "runner", "--chat"]), patch.object(
                api, "_profile_home", return_value=home
            ), patch.object(api, "_catalog_environment", return_value={"HERMES_HOME": str(home)}), patch.object(
                api.subprocess, "Popen", return_value=process
            ):
                api._run_job("job-image", request)
            payload = json.loads(process.received)
            self.assertEqual(len(payload["images"]), 1)
            self.assertNotIn(request.images[0].data_url, process.received)
            self.assertFalse((home / "images" / "agent-dock" / "job-image").exists())
        self.assertEqual(api._JOBS["job-image"]["status"], "done")

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

    def test_runner_error_redacts_credentials_and_private_paths(self):
        error = api._safe_runner_error(
            "authorization: Bearer SUPERSECRET_BEARER_123456 "
            "C:\\Users\\Alice\\My Documents\\board.sqlite; "
            "\\\\server\\share\\private\\board.sqlite; "
            "/tmp/private/runner.log",
            1,
        )
        self.assertNotIn("SUPERSECRET_BEARER_123456", error)
        self.assertNotIn("Alice", error)
        self.assertNotIn("server", error)
        self.assertNotIn("/tmp/", error)
        self.assertIn("[REDACTED]", error)
        self.assertIn("[PRIVATE_PATH]", error)

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
        self.seed_durable_job("job-transport", request)
        with patch.object(api, "_build_command", return_value=[sys.executable, "runner", "--chat"]), patch.object(
            api, "_catalog_environment", return_value={"HERMES_HOME": "C:/profile"}
        ), patch.object(api.subprocess, "Popen", return_value=process) as popen:
            api._run_job("job-transport", request)

        self.assertIs(popen.call_args.kwargs["stdin"], api.subprocess.PIPE)
        self.assertEqual(json.loads(process.received)["message"], "live transport probe")
        self.assertEqual(api._JOBS["job-transport"]["status"], "done")
        self.assertEqual(api._JOBS["job-transport"]["response"], "agent reply")

    def test_run_job_without_durable_row_fails_closed(self):
        request = self.request(message="must not run from memory alone")
        api._JOBS["memory-only-job"] = {
            "id": "memory-only-job",
            "status": "starting",
            "kanban_task_id": None,
        }
        with patch.object(api.subprocess, "Popen") as popen, patch.object(
            api, "_settle_job_kanban"
        ) as settle:
            api._run_job("memory-only-job", request)
        popen.assert_not_called()
        settle.assert_not_called()
        self.assertEqual(api._JOBS["memory-only-job"]["status"], "starting")

    def test_cancellation_during_command_build_prevents_process_spawn(self):
        request = self.request(message="cancel before spawn")
        self.seed_durable_job("job-cancel-build", request)

        def cancel_during_build(_request):
            self.cancel_seeded_job("job-cancel-build")
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
        self.seed_durable_job("job-cancel-attach", request)

        def spawn_then_cancel(**_kwargs):
            self.cancel_seeded_job("job-cancel-attach")
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

    def test_kanban_settlement_error_never_exposes_private_path(self):
        api._JOBS["job-private-path"] = {
            "id": "job-private-path",
            "kanban_task_id": "t_agentdock",
        }
        private_path = r"C:\Users\Alice\private\board.sqlite"
        with patch.object(
            api,
            "_settle_kanban_task",
            side_effect=PermissionError(f"{private_path}: permission denied"),
        ):
            api._settle_job_kanban("job-private-path", "done", "response")
        stored = api._JOBS["job-private-path"]["kanban_error"]
        self.assertEqual(stored, "Kanban update failed; inspect local Hermes logs")
        self.assertNotIn(private_path, stored)

    @patch.object(api.threading, "Thread")
    def test_kanban_assignment_error_never_exposes_private_path(self, _thread_cls):
        request = self.request(request_id="req-private-path", assign_task=True)
        private_path = r"C:\Users\Alice\private\board.sqlite"
        with self.validation(), patch.object(
            api,
            "_create_kanban_task",
            side_effect=PermissionError(f"{private_path}: permission denied"),
        ):
            with self.assertRaises(api.HTTPException) as raised:
                api.create_job(request)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            "Kanban assignment failed; inspect local Hermes logs",
        )
        self.assertNotIn(private_path, raised.exception.detail)
        rows = api._job_store().list_jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "error")
        self.assertEqual(
            rows[0]["error_summary"],
            "Kanban assignment failed; inspect local Hermes logs",
        )

    @patch.object(api.threading, "Thread")
    def test_slow_kanban_assignment_does_not_hold_sqlite_write_lock(self, _thread_cls):
        request = self.request(request_id="req-slow-kanban-123", assign_task=True)
        callback_started = threading.Event()
        release_callback = threading.Event()
        result = []
        errors = []

        def slow_assignment(_request, _job_id):
            callback_started.set()
            if not release_callback.wait(timeout=5):
                raise RuntimeError("test callback timed out")
            return "t_slow_assignment"

        def submit():
            try:
                result.append(api.create_job(request))
            except BaseException as exc:
                errors.append(exc)

        with self.validation(), patch.object(
            api, "_create_kanban_task", side_effect=slow_assignment
        ):
            submitter = REAL_THREAD(target=submit)
            submitter.start()
            self.assertTrue(callback_started.wait(timeout=5))
            other, created = api._job_store().reserve_job(
                job_id="other-job",
                profile_id="jarvis",
                request_id="req-independent-write-123",
                provider="openai-codex",
                model="gpt-5.6-codex",
                reasoning_effort="none",
                fast=False,
                assign_task=False,
                image_count=0,
                session_id=None,
                kanban_task_id=None,
                kanban_board=None,
            )
            self.assertTrue(created)
            self.assertEqual(other["job_id"], "other-job")
            release_callback.set()
            submitter.join(timeout=5)
        self.assertFalse(submitter.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0]["kanban_task_id"], "t_slow_assignment")

    @patch.object(api, "_profile_rows", return_value=[])
    def test_profiles_advertises_native_controls(self, _rows):
        result = asyncio.run(api.profiles())
        self.assertTrue(result["capabilities"]["model_catalog"])
        self.assertTrue(result["capabilities"]["image_upload"])
        self.assertEqual(result["capabilities"]["max_images"], api.MAX_IMAGE_ATTACHMENTS)
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

    @patch.object(api.threading, "Thread")
    def test_duplicate_after_store_reopen_bypasses_catalog_and_starts_no_worker(self, thread_cls):
        request = self.request(request_id="req-restart-duplicate-123")
        with patch.object(api, "_build_command", return_value=[sys.executable, "runner", "--chat"]):
            first = api.create_job(request)
        api._reset_job_store_for_tests(clear_memory=True)
        with patch.object(api, "_build_command", side_effect=RuntimeError("catalog unavailable")) as command:
            second = api.create_job(request)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["status"], "interrupted")
        command.assert_not_called()
        self.assertEqual(thread_cls.call_count, 1)

    @patch.object(api.threading, "Thread")
    def test_concurrent_api_submissions_reserve_once_and_start_one_worker(self, thread_cls):
        request = self.request(request_id="req-concurrent-api-123", assign_task=True)
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def submit():
            try:
                barrier.wait(timeout=5)
                results.append(api.create_job(request))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(api, "_build_command", return_value=[sys.executable, "runner", "--chat"]), patch.object(
            api, "_create_kanban_task", return_value="t_concurrent_once"
        ) as create_kanban:
            workers = [REAL_THREAD(target=submit) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual({result["id"] for result in results}, {results[0]["id"]})
        self.assertEqual(thread_cls.call_count, 1)
        create_kanban.assert_called_once_with(request, results[0]["id"])
        self.assertEqual(results[0]["kanban_task_id"], "t_concurrent_once")

    @patch.object(api.threading, "Thread")
    def test_durable_lookup_and_cancellation_survive_memory_loss(self, thread_cls):
        request = self.request(request_id="req-durable-lookup-123")
        with patch.object(api, "_build_command", return_value=[sys.executable, "runner", "--chat"]):
            created = api.create_job(request)
        api._JOBS.clear()
        api._REQUEST_JOBS.clear()
        recovered = asyncio.run(api.get_job(created["id"]))
        self.assertEqual(recovered["id"], created["id"])
        cancelled = asyncio.run(api.cancel_job(created["id"]))
        self.assertEqual(cancelled["status"], "cancelled")
        api._reset_job_store_for_tests(clear_memory=True)
        self.assertEqual(asyncio.run(api.get_job(created["id"]))["status"], "cancelled")

    @patch.object(api.threading, "Thread")
    def test_invalid_new_request_creates_no_durable_row(self, thread_cls):
        request = self.request(request_id="req-invalid-new-123", model="not-configured")
        with self.validation(), self.assertRaises(api.HTTPException):
            api.create_job(request)
        self.assertEqual(api._job_store().list_jobs(), [])
        thread_cls.assert_not_called()

    @patch.object(api.threading, "Thread")
    def test_public_job_and_sqlite_omit_private_execution_content(self, thread_cls):
        prompt = "PRIVATE_PROMPT_SENTINEL C:\\Users\\Private\\capture.png"
        image_sentinel = b"PRIVATE_IMAGE_SENTINEL"
        png = bytes.fromhex("89504e470d0a1a0a") + image_sentinel
        request = self.request(
            message=prompt,
            request_id="req-private-ledger-123",
            images=[{
                "name": "C:\\Users\\Private\\capture.png",
                "mime_type": "image/png",
                "data_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            }],
        )
        with patch.object(api, "_build_command", return_value=[sys.executable, "runner", "--chat"]):
            public = api.create_job(request)
        self.assertFalse({"attempt_token", "_attempt_token", "_process"} & set(public))
        database = api._job_store().database_path
        api._reset_job_store_for_tests(clear_memory=False)
        raw = database.read_bytes()
        for sentinel in (prompt.encode(), image_sentinel, b"PRIVATE_PROMPT_SENTINEL", b"capture.png"):
            self.assertNotIn(sentinel, raw)

    @patch.object(api.threading, "Thread")
    def test_stale_attempt_after_reopen_cannot_settle_kanban(self, thread_cls):
        request = self.request(request_id="req-stale-settlement-123")
        with patch.object(api, "_build_command", return_value=[sys.executable, "runner", "--chat"]):
            created = api.create_job(request)
        old_token = api._JOBS[created["id"]]["_attempt_token"]
        self.assertTrue(api._job_store().mark_running(created["id"], old_token))
        api._reset_job_store_for_tests(clear_memory=False)
        with patch.object(api, "_settle_job_kanban") as settle, patch.object(
            api, "_build_command", return_value=[sys.executable, "runner", "--chat"]
        ):
            api._run_job(created["id"], request)
        settle.assert_not_called()
        self.assertEqual(api._job_store().get_job(created["id"])["status"], "interrupted")

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

    def test_subagent_refresh_rejects_malformed_prestart_and_cross_job_jsonl(self):
        job_id = "job-parent"
        with tempfile.TemporaryDirectory() as directory:
            progress_path = Path(directory) / "progress.jsonl"
            rows = [
                "not json",
                {
                    "event": "subagent.tool",
                    "subagent_id": f"{job_id}:subagent:0",
                    "task_index": 0,
                    "status": "running",
                    "started_at": 10,
                    "updated_at": 11,
                    "finished_at": None,
                    "current_tool": "terminal",
                    "duration_seconds": 1,
                },
                {
                    "event": "subagent.start",
                    "subagent_id": "other-parent:subagent:0",
                    "task_index": 0,
                    "status": "running",
                    "started_at": 10,
                    "updated_at": 10,
                    "finished_at": None,
                    "current_tool": None,
                    "duration_seconds": 0,
                },
                {
                    "event": "subagent.start",
                    "subagent_id": f"{job_id}:subagent:0",
                    "task_index": 0,
                    "status": "running",
                    "started_at": 10,
                    "updated_at": 10,
                    "finished_at": None,
                    "current_tool": None,
                    "duration_seconds": 0,
                },
                {
                    "event": "subagent.complete",
                    "subagent_id": f"{job_id}:subagent:0",
                    "task_index": 0,
                    "status": "completed",
                    "started_at": 10,
                    "updated_at": 12,
                    "finished_at": 12,
                    "current_tool": None,
                    "duration_seconds": 2,
                },
                {
                    "event": "subagent.complete",
                    "subagent_id": f"{job_id}:subagent:0",
                    "task_index": 0,
                    "status": "completed",
                    "started_at": 10,
                    "updated_at": 12,
                    "finished_at": 12,
                    "current_tool": None,
                    "duration_seconds": 2,
                    "prompt": "PRIVATE PROMPT",
                },
                {
                    "event": "subagent.progress",
                    "subagent_id": f"{job_id}:subagent:0",
                    "task_index": 0,
                    "status": "running",
                    "started_at": 10,
                    "updated_at": 13,
                    "finished_at": None,
                    "current_tool": "terminal",
                    "duration_seconds": 3,
                },
            ]
            progress_path.write_text(
                "\n".join(row if isinstance(row, str) else json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            job = {
                "id": job_id,
                "subagents": [],
                "_subagent_progress_path": progress_path,
                "_subagent_started_ids": set(),
            }
            api._refresh_subagents(job)

        self.assertEqual(len(job["subagents"]), 1)
        child = job["subagents"][0]
        self.assertEqual(child["subagent_id"], f"{job_id}:subagent:0")
        self.assertEqual(child["status"], "completed")
        self.assertIsNone(child["current_tool"])
        self.assertNotIn("event", child)
        self.assertNotIn("prompt", child)
        self.assertNotIn("summary", child)
        self.assertIsNone(child["model"])
        self.assertIsNone(child["api_calls"])
        self.assertIsNone(child["total_tokens"])
        self.assertEqual(child["usage_state"], "unavailable")
        self.assertFalse(child["direct_chat_available"])

    def test_subagent_progress_file_is_removed_with_evicted_job(self):
        with tempfile.TemporaryDirectory() as directory:
            progress_path = Path(directory) / "job.jsonl"
            progress_path.write_text("{}\n", encoding="utf-8")
            now = 10_000
            api._JOBS["job-cleanup"] = {
                "id": "job-cleanup",
                "status": "done",
                "finished_at": now - api.JOB_RETENTION_SECONDS - 1,
                "_subagent_progress_path": progress_path,
            }
            api._evict_completed_jobs(now=now)
            self.assertFalse(progress_path.exists())
            self.assertNotIn("job-cleanup", api._JOBS)

    def test_rehydrated_interrupted_job_restores_children_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            profile_home = Path(directory) / "profiles" / "jarvis"
            progress_path = profile_home / "cache" / "agent-dock-progress" / "job-restart.jsonl"
            progress_path.parent.mkdir(parents=True)
            progress_path.write_text(json.dumps({
                "event": "subagent.start",
                "subagent_id": "job-restart:subagent:0",
                "task_index": 0,
                "status": "running",
                "started_at": 10,
                "updated_at": 10,
                "finished_at": None,
                "current_tool": None,
                "duration_seconds": 0,
                "model": None,
                "api_calls": None,
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "usage_state": "unavailable",
                "direct_chat_available": False,
            }) + "\n", encoding="utf-8")
            row = {
                "job_id": "job-restart",
                "profile_id": "jarvis",
                "status": "interrupted",
                "attempt_token": "attempt",
                "created_at": 10,
                "finished_at": 20,
            }
            with patch.object(api, "_profile_home", return_value=profile_home):
                job = api._memory_job_from_row(row)
            self.assertEqual(len(job["subagents"]), 1)
            self.assertEqual(job["subagents"][0]["status"], "interrupted")
            self.assertEqual(job["subagents"][0]["finished_at"], 20)
            self.assertIsNone(job["subagents"][0]["current_tool"])


if __name__ == "__main__":
    unittest.main()
