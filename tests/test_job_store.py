from __future__ import annotations

import sqlite3
import threading
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.dashboard.job_store import JobStore


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.home = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def reserve(self, store: JobStore, *, profile: str = "jarvis", request_id: str = "req-12345678"):
        row, created = store.reserve_job(
            profile_id=profile,
            request_id=request_id,
            provider="openai-codex",
            model="gpt-5.6-terra",
            reasoning_effort="none",
            fast=False,
            assign_task=False,
            image_count=0,
            session_id="20260805_010203_a1b2c3",
            kanban_task_id=None,
            kanban_board=None,
        )
        self.assertTrue(created)
        return row

    def test_fresh_instance_reopen_returns_same_job_and_interrupts_inflight_attempt(self):
        first_store = JobStore(hermes_home=self.home)
        first = self.reserve(first_store)
        old_token = first["attempt_token"]
        self.assertTrue(first_store.mark_running(first["job_id"], old_token))

        reopened = JobStore(hermes_home=self.home)
        recovered = reopened.get_job(first["job_id"])

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["job_id"], first["job_id"])
        self.assertEqual(recovered["profile_id"], "jarvis")
        self.assertEqual(recovered["request_id"], "req-12345678")
        self.assertEqual(recovered["status"], "interrupted")
        self.assertNotEqual(recovered["attempt_token"], old_token)
        self.assertEqual(recovered["session_id"], "20260805_010203_a1b2c3")
        self.assertIsNone(recovered["response_body"])

    def test_every_active_state_is_interrupted_on_reopen(self):
        for index, status in enumerate(("starting", "queued", "running", "finalizing")):
            with self.subTest(status=status):
                request_id = f"req-active-{index:02d}-12345678"
                store = JobStore(hermes_home=self.home)
                row = self.reserve(store, request_id=request_id)
                token = row["attempt_token"]
                with closing(sqlite3.connect(store.database_path)) as connection:
                    connection.execute(
                        "UPDATE jobs SET status=?, attempt_token=? WHERE job_id=?",
                        (status, token, row["job_id"]),
                    )
                    connection.commit()

                reopened = JobStore(hermes_home=self.home)
                recovered = reopened.get_job(row["job_id"])
                self.assertEqual(recovered["status"], "interrupted")
                self.assertNotEqual(recovered["attempt_token"], token)

    def test_cancelling_is_reconciled_to_cancelled_without_relaunch(self):
        first_store = JobStore(hermes_home=self.home)
        first = self.reserve(first_store)
        token = first["attempt_token"]
        self.assertTrue(first_store.mark_running(first["job_id"], token))
        self.assertTrue(first_store.request_cancel(first["job_id"], token))
        self.assertEqual(first_store.get_job(first["job_id"])["status"], "cancelling")

        reopened = JobStore(hermes_home=self.home)
        recovered = reopened.get_job(first["job_id"])
        self.assertEqual(recovered["status"], "cancelled")
        self.assertNotEqual(recovered["attempt_token"], token)
        self.assertIn("restart", recovered["error_summary"].lower())

    def test_two_concurrent_first_submissions_create_one_reservation(self):
        store = JobStore(hermes_home=self.home)
        results: list[tuple[dict, bool]] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def submit() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(
                    store.reserve_job(
                        profile_id="jarvis",
                        request_id="req-concurrent-123",
                        provider="openai-codex",
                        model="gpt-5.6-terra",
                        reasoning_effort="none",
                        fast=False,
                        assign_task=False,
                        image_count=0,
                        session_id=None,
                        kanban_task_id=None,
                        kanban_board=None,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual({row["job_id"] for row, _ in results}, {results[0][0]["job_id"]})
        self.assertEqual(len(store.list_jobs()), 1)

    def test_same_request_id_across_profiles_is_distinct(self):
        store = JobStore(hermes_home=self.home)
        first = self.reserve(store, profile="jarvis", request_id="req-shared-123")
        second_row, created = store.reserve_job(
            profile_id="atlas",
            request_id="req-shared-123",
            provider="openai-codex",
            model="gpt-5.6-terra",
            reasoning_effort="none",
            fast=False,
            assign_task=False,
            image_count=0,
            session_id=None,
            kanban_task_id=None,
            kanban_board=None,
        )
        self.assertTrue(created)
        self.assertNotEqual(first["job_id"], second_row["job_id"])
        self.assertEqual({row["profile_id"] for row in store.list_jobs()}, {"jarvis", "atlas"})

    def test_terminal_states_are_monotonic(self):
        store = JobStore(hermes_home=self.home)
        row = self.reserve(store)
        token = row["attempt_token"]
        self.assertTrue(store.mark_running(row["job_id"], token))
        self.assertTrue(store.begin_finalization(row["job_id"], token, session_id=row["session_id"]))
        self.assertTrue(store.complete_done(row["job_id"], token))
        self.assertFalse(store.mark_running(row["job_id"], token))
        self.assertFalse(store.complete_error(row["job_id"], token, "late error"))
        self.assertEqual(store.get_job(row["job_id"])["status"], "done")

    def test_all_terminal_states_reject_active_and_competing_terminal_transitions(self):
        for index, status in enumerate(("done", "error", "cancelled", "interrupted")):
            with self.subTest(status=status):
                store = JobStore(hermes_home=self.home)
                row = self.reserve(store, request_id=f"req-terminal-{index}-12345678")
                token = row["attempt_token"]
                with closing(sqlite3.connect(store.database_path)) as connection:
                    connection.execute(
                        "UPDATE jobs SET status=? WHERE job_id=?",
                        (status, row["job_id"]),
                    )
                    connection.commit()
                self.assertFalse(store.mark_running(row["job_id"], token))
                if status != "done":
                    self.assertFalse(store.complete_done(row["job_id"], token))
                if status != "error":
                    self.assertFalse(store.complete_error(row["job_id"], token, "late error"))
                self.assertEqual(store.get_job(row["job_id"])["status"], status)

    def test_stale_attempt_cannot_finalize_after_restart_invalidation(self):
        first_store = JobStore(hermes_home=self.home)
        row = self.reserve(first_store)
        old_token = row["attempt_token"]
        self.assertTrue(first_store.mark_running(row["job_id"], old_token))

        restarted = JobStore(hermes_home=self.home)
        self.assertEqual(restarted.get_job(row["job_id"])["status"], "interrupted")
        self.assertFalse(first_store.begin_finalization(row["job_id"], old_token, session_id=None))
        self.assertFalse(first_store.complete_done(row["job_id"], old_token))
        self.assertEqual(restarted.get_job(row["job_id"])["status"], "interrupted")

    def test_sqlite_ledger_contains_metadata_only(self):
        store = JobStore(hermes_home=self.home)
        row = self.reserve(store)
        self.assertNotIn("prompt", store.columns)
        self.assertNotIn("message", store.columns)
        self.assertNotIn("response", store.columns)
        self.assertNotIn("image", store.columns)
        self.assertNotIn("path", store.columns)
        self.assertEqual(row["image_count"], 0)
        self.assertIsNone(row["response_body"])

    def test_error_summary_redacts_bearer_credentials_and_private_paths(self):
        store = JobStore(hermes_home=self.home)
        row = self.reserve(store, request_id="req-redaction-12345678")
        token = row["attempt_token"]
        self.assertTrue(store.mark_running(row["job_id"], token))
        bearer = "SUPER" + "SECRET_BEARER_123456"
        summary = (
            f"authorization: Bearer {bearer} "
            "C:\\Users\\Alice\\My Documents\\board.sqlite; "
            "\\\\server\\share\\private\\board.sqlite; "
            "/tmp/private/board.sqlite"
        )
        self.assertTrue(store.complete_error(row["job_id"], token, summary))
        stored = store.get_job(row["job_id"])["error_summary"]
        self.assertNotIn(bearer, stored)
        self.assertNotIn("Alice", stored)
        self.assertNotIn("server", stored)
        self.assertNotIn("/tmp/", stored)
        self.assertIn("[REDACTED]", stored)
        self.assertIn("[PRIVATE_PATH]", stored)
        raw = store.database_path.read_bytes()
        for sentinel in (bearer.encode(), b"Alice", b"server", b"/tmp/private"):
            self.assertNotIn(sentinel, raw)

    def test_publish_commits_before_callback_failure(self):
        store = JobStore(hermes_home=self.home)
        row = self.reserve(store, request_id="req-publish-order-12345678")
        token = row["attempt_token"]
        self.assertTrue(store.mark_running(row["job_id"], token))
        self.assertTrue(store.begin_finalization(row["job_id"], token, session_id=None))
        effects = []

        def fail_after_effect(_row):
            effects.append("settled")
            raise RuntimeError("external settlement failed")

        with self.assertRaisesRegex(RuntimeError, "external settlement failed"):
            store.publish(row["job_id"], token, fail_after_effect)
        self.assertEqual(effects, ["settled"])
        self.assertEqual(store.get_job(row["job_id"])["status"], "done")

    def test_database_schema_is_sqlite_and_reopenable(self):
        store = JobStore(hermes_home=self.home)
        self.reserve(store)
        with closing(sqlite3.connect(store.database_path)) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("jobs", names)


    def test_reset_attempt_rearms_terminal_job_with_fresh_token(self):
        store = JobStore(hermes_home=self.home)
        row = self.reserve(store)
        token = row["attempt_token"]
        self.assertTrue(store.mark_running(row["job_id"], token))
        self.assertTrue(store.begin_finalization(row["job_id"], token, session_id=None))
        self.assertTrue(store.complete_error(row["job_id"], token, "provider rejected request"))
        self.assertEqual(store.get_job(row["job_id"])["status"], "error")

        fresh = store.reset_attempt(row["job_id"], allow_statuses=frozenset({"done", "error", "cancelled", "interrupted"}))
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh["job_id"], row["job_id"])
        self.assertEqual(fresh["profile_id"], "jarvis")
        self.assertEqual(fresh["request_id"], row["request_id"])
        self.assertEqual(fresh["status"], "starting")
        self.assertIsNotNone(fresh["attempt_token"])
        self.assertNotEqual(fresh["attempt_token"], token)
        self.assertIsNone(fresh["error_summary"])
        self.assertIsNone(fresh["finished_at"])

    def test_reset_attempt_is_idempotent_safe_and_fails_closed_on_active(self):
        store = JobStore(hermes_home=self.home)
        row = self.reserve(store)
        token = row["attempt_token"]

        # Active (starting) is not an allowed source status -> fails closed.
        self.assertIsNone(
            store.reset_attempt(row["job_id"], allow_statuses=frozenset({"done", "error", "cancelled", "interrupted"}))
        )
        self.assertEqual(store.get_job(row["job_id"])["status"], "starting")

        # Unknown job -> fails closed.
        self.assertIsNone(
            store.reset_attempt("f" * 32, allow_statuses=frozenset({"done", "error", "cancelled", "interrupted"}))
        )

    def test_link_kanban_terminal_records_card_for_finished_job(self):
        store = JobStore(hermes_home=self.home)
        row = self.reserve(store)
        token = row["attempt_token"]
        self.assertTrue(store.mark_running(row["job_id"], token))
        self.assertTrue(store.begin_finalization(row["job_id"], token, session_id=None))
        self.assertTrue(store.publish(row["job_id"], token))
        self.assertEqual(store.get_job(row["job_id"])["status"], "done")

        self.assertTrue(
            store.link_kanban_terminal(
                row["job_id"],
                kanban_task_id="t_assign_after",
                kanban_board="executive-organization",
                allow_statuses=frozenset({"done", "error", "cancelled", "interrupted"}),
            )
        )
        stored = store.get_job(row["job_id"])
        self.assertEqual(stored["kanban_task_id"], "t_assign_after")
        self.assertEqual(stored["kanban_board"], "executive-organization")

    def test_link_kanban_terminal_fails_closed_off_terminal_status(self):
        store = JobStore(hermes_home=self.home)
        row = self.reserve(store)
        # Job is still `starting`; linking to a terminal card is not legal.
        self.assertFalse(
            store.link_kanban_terminal(
                row["job_id"],
                kanban_task_id="t_noop",
                kanban_board="executive-organization",
                allow_statuses=frozenset({"done", "error", "cancelled", "interrupted"}),
            )
        )
        self.assertIsNone(store.get_job(row["job_id"])["kanban_task_id"])


if __name__ == "__main__":
    unittest.main()
