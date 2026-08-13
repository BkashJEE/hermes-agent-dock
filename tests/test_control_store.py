from __future__ import annotations

import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agent_dock_control_store",
    ROOT / "backend" / "dashboard" / "control_store.py",
)
store_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(store_module)


class ControlStoreTests(unittest.TestCase):
    RUNTIME_PROFILE = "jarvis"
    RUNTIME_SESSION_ID = "runtime-session-1"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        self.addCleanup(self.temp_dir.cleanup)
        self.home.mkdir(parents=True, exist_ok=True)

    def open_store(self):
        return store_module.ControlStore(hermes_home=self.home)

    def claim(self, store, message_id, **kwargs):
        return store.claim_message(
            message_id,
            runtime_profile=self.RUNTIME_PROFILE,
            runtime_session_id=self.RUNTIME_SESSION_ID,
            **kwargs,
        )

    def receipt(self, store, **kwargs):
        return store.record_receipt(
            runtime_profile=self.RUNTIME_PROFILE,
            runtime_session_id=self.RUNTIME_SESSION_ID,
            **kwargs,
        )

    def test_schema_is_profile_local_configured_and_persists_across_reopen(self):
        with patch.object(store_module, "get_hermes_home", return_value=self.home):
            first = store_module.ControlStore()
            self.addCleanup(first.close)
            self.assertEqual(
                first.path,
                self.home / "agent-dock" / "control-plane.sqlite3",
            )
            self.assertEqual(first.schema_version, store_module.SCHEMA_VERSION)
            self.assertEqual(first.connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(first.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(
                first.connection.execute("PRAGMA busy_timeout").fetchone()[0],
                store_module.BUSY_TIMEOUT_MS,
            )
            run = first.attach_run(
                profile="jarvis",
                session_id="session-1",
                runtime_profile=self.RUNTIME_PROFILE,
                runtime_session_id=self.RUNTIME_SESSION_ID,
                source="desktop-session",
                objective="Repair the reply path",
            )
            message = first.enqueue_message(
                message_id="msg-persist-001",
                run_id=run["run_id"],
                kind="ask",
                body="What changed?",
            )
            claimed = self.claim(first, message["message_id"], dispatch_token="persist-dispatch")
            receipt = self.receipt(first,
                message_id=message["message_id"],
                stage="accepted",
                verification_state="reported",
                source="hermes-gateway",
                dispatch_token=claimed["dispatch_token"],
                receipt_id="receipt-persist-accepted",
                detail={"status": "queued"},
            )
            first.close()

            reopened = store_module.ControlStore()
            self.addCleanup(reopened.close)
            selected = reopened.get_run(run["run_id"])

        self.assertEqual(selected["run_id"], run["run_id"])
        self.assertEqual(selected["profile"], "jarvis")
        self.assertEqual(selected["messages"][0]["message_id"], message["message_id"])
        self.assertEqual(selected["messages"][0]["state"], "accepted")
        self.assertEqual(selected["receipts"][0]["receipt_id"], receipt["receipt_id"])
        self.assertEqual(selected["events"], reopened.list_events(run_id=run["run_id"]))

    def test_runtime_rebind_is_exact_persistent_and_rejects_stale_runtime_authority(self):
        store = self.open_store()
        run = store.attach_run(
            profile="jarvis",
            session_id="stable-rebind-session",
            runtime_profile="jarvis",
            runtime_session_id="runtime-old",
            source="desktop-session",
            objective="private objective must not enter rebound events",
        )
        historical = store.enqueue_message(
            message_id="msg-rebind-history",
            run_id=run["run_id"],
            kind="nudge",
            body="private historical message body",
        )
        old_claim = store.claim_message(
            historical["message_id"],
            runtime_profile="jarvis",
            runtime_session_id="runtime-old",
            dispatch_token="old-history-token",
        )
        store.record_receipt(
            message_id=historical["message_id"],
            runtime_profile="jarvis",
            runtime_session_id="runtime-old",
            stage="accepted",
            verification_state="observed",
            source="hermes-gateway",
            dispatch_token=old_claim["dispatch_token"],
            receipt_id="receipt-rebind-history",
        )
        pending = store.enqueue_message(
            message_id="msg-rebind-pending",
            run_id=run["run_id"],
            kind="nudge",
            body="deliver only to the rebound runtime",
        )

        invalid_rebinds = (
            {"profile": "default"},
            {"session_id": "wrong-stable-session"},
            {"old_runtime_session_id": "stale-runtime"},
            {"runtime_profile": "default"},
        )
        base = {
            "run_id": run["run_id"],
            "profile": "jarvis",
            "session_id": "stable-rebind-session",
            "old_runtime_profile": "jarvis",
            "old_runtime_session_id": "runtime-old",
            "runtime_profile": "jarvis",
            "runtime_session_id": "runtime-new",
            "permission_scope": "inherit-only",
        }
        for override in invalid_rebinds:
            with self.subTest(override=override), self.assertRaises(
                (store_module.BindingError, store_module.ConflictError)
            ):
                store.rebind_run_runtime(**(base | override))
        with self.assertRaises(store_module.ValidationError):
            store.rebind_run_runtime(**(base | {"permission_scope": "expanded"}))

        rebound = store.rebind_run_runtime(**base)
        self.assertEqual(rebound["runtime_session_id"], "runtime-new")
        self.assertEqual(
            len(store.list_events(run_id=run["run_id"], kind="run_rebound")),
            1,
        )
        with self.assertRaises(store_module.ConflictError):
            store.rebind_run_runtime(**base)
        with self.assertRaises(store_module.BindingError):
            store.claim_message(
                pending["message_id"],
                runtime_profile="jarvis",
                runtime_session_id="runtime-old",
            )

        new_claim = store.claim_message(
            pending["message_id"],
            runtime_profile="jarvis",
            runtime_session_id="runtime-new",
            dispatch_token="new-runtime-token",
        )
        with self.assertRaises(store_module.BindingError):
            store.record_receipt(
                message_id=pending["message_id"],
                runtime_profile="jarvis",
                runtime_session_id="runtime-old",
                stage="accepted",
                verification_state="observed",
                source="hermes-gateway",
                dispatch_token=new_claim["dispatch_token"],
                receipt_id="receipt-stale-runtime",
            )
        store.record_receipt(
            message_id=pending["message_id"],
            runtime_profile="jarvis",
            runtime_session_id="runtime-new",
            stage="accepted",
            verification_state="observed",
            source="hermes-gateway",
            dispatch_token=new_claim["dispatch_token"],
            receipt_id="receipt-new-runtime",
        )
        store.close()

        reopened = self.open_store()
        self.addCleanup(reopened.close)
        selected = reopened.get_run(run["run_id"])
        self.assertEqual(selected["runtime_session_id"], "runtime-new")
        self.assertEqual(
            {row["message_id"] for row in selected["messages"]},
            {"msg-rebind-history", "msg-rebind-pending"},
        )
        self.assertEqual(
            {row["receipt_id"] for row in selected["receipts"]},
            {"receipt-rebind-history", "receipt-new-runtime"},
        )
        rebound_events = reopened.list_events(run_id=run["run_id"], kind="run_rebound")
        self.assertEqual(len(rebound_events), 1)
        serialized = json.dumps(rebound_events, sort_keys=True)
        self.assertNotIn("private objective", serialized)
        self.assertNotIn("private historical message body", serialized)
        self.assertNotIn("deliver only to the rebound runtime", serialized)

    def test_empty_or_legacy_startup_is_additive_and_sets_schema_version(self):
        path = self.home / "agent-dock" / "control-plane.sqlite3"
        path.parent.mkdir(parents=True)
        import sqlite3

        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '0')")
        connection.commit()
        connection.close()

        store = store_module.ControlStore(db_path=path)
        self.addCleanup(store.close)
        self.assertEqual(store.schema_version, store_module.SCHEMA_VERSION)
        tables = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue({"runs", "messages", "receipts", "events"}.issubset(tables))

    def test_attach_is_idempotent_only_for_exact_profile_session_run_binding(self):
        store = self.open_store()
        self.addCleanup(store.close)

        first = store.attach_run(
            profile="Jarvis",
            session_id="same-session",
            runtime_session_id="runtime-1",
            subagent_id="child-1",
            kanban_task_id="task-1",
            source="subagent",
            status="working",
        )
        duplicate = store.attach_run(
            profile="jarvis",
            session_id="same-session",
            runtime_session_id="runtime-1",
            subagent_id="child-1",
            kanban_task_id="task-1",
            source="subagent",
            status="working",
        )
        with self.assertRaises(store_module.ConflictError):
            store.attach_run(
                profile="jarvis",
                session_id="same-session",
                runtime_session_id="runtime-2",
                subagent_id="child-1",
                kanban_task_id="task-1",
                source="subagent",
                status="working",
            )
        other_profile = store.attach_run(
            profile="default",
            session_id="same-session",
            subagent_id="child-1",
            kanban_task_id="task-1",
            source="subagent",
        )
        other_child = store.attach_run(
            profile="jarvis",
            session_id="same-session",
            subagent_id="child-2",
            kanban_task_id="task-1",
            source="subagent",
        )

        self.assertEqual(first["run_id"], duplicate["run_id"])
        self.assertNotEqual(first["run_id"], other_profile["run_id"])
        self.assertNotEqual(first["run_id"], other_child["run_id"])
        self.assertEqual(
            len(store.list_events(run_id=first["run_id"], kind="run_attached")),
            1,
        )
        with self.assertRaises(store_module.BindingError):
            store.get_run(first["run_id"], profile="default", session_id="same-session")

    def test_message_kinds_are_bounded_and_confirmation_is_required(self):
        store = self.open_store()
        self.addCleanup(store.close)
        run = store.attach_run(profile="jarvis", session_id="s-1", source="desktop-session")

        for kind in ("ask", "nudge"):
            row = store.enqueue_message(
                message_id=f"msg-{kind}-001",
                run_id=run["run_id"],
                kind=kind,
                body="hello",
            )
            self.assertEqual(row["kind"], kind)
        for kind in ("redirect", "stop"):
            with self.assertRaises(store_module.ConfirmationRequired):
                store.enqueue_message(
                    message_id=f"msg-{kind}-001",
                    run_id=run["run_id"],
                    kind=kind,
                    body="change course",
                )
            confirmed = store.enqueue_message(
                message_id=f"msg-{kind}-002",
                run_id=run["run_id"],
                kind=kind,
                body="change course",
                confirmed=True,
                now=100,
            )
            self.assertEqual(confirmed["confirmed_at"], 100)
        with self.assertRaises(store_module.ValidationError):
            store.enqueue_message(
                message_id="msg-invalid-kind",
                run_id=run["run_id"],
                kind="pause",
                body="not supported",
            )
        with self.assertRaises(store_module.ValidationError):
            store.enqueue_message(
                message_id="msg-too-long",
                run_id=run["run_id"],
                kind="ask",
                body="x" * (store_module.MAX_MESSAGE_CHARS + 1),
            )

    def test_message_id_is_idempotent_and_conflicts_are_rejected(self):
        store = self.open_store()
        self.addCleanup(store.close)
        run = store.attach_run(profile="jarvis", session_id="s-2", source="desktop-session")
        first = store.enqueue_message(
            message_id="msg-idempotent-001",
            run_id=run["run_id"],
            kind="nudge",
            body="keep going",
        )
        duplicate = store.enqueue_message(
            message_id="msg-idempotent-001",
            run_id=run["run_id"],
            kind="nudge",
            body="keep going",
        )
        self.assertEqual(duplicate, first)
        self.assertEqual(
            len(store.list_events(run_id=run["run_id"], message_id=first["message_id"])),
            1,
        )
        with self.assertRaises(store_module.ConflictError):
            store.enqueue_message(
                message_id="msg-idempotent-001",
                run_id=run["run_id"],
                kind="nudge",
                body="a different request",
            )

    def test_state_transitions_require_valid_receipts_and_terminal_state_is_immutable(self):
        store = self.open_store()
        self.addCleanup(store.close)
        run = store.attach_run(
            profile="jarvis",
            session_id="s-3",
            runtime_profile=self.RUNTIME_PROFILE,
            runtime_session_id=self.RUNTIME_SESSION_ID,
            source="desktop-session",
        )
        message = store.enqueue_message(
            message_id="msg-transition-001",
            run_id=run["run_id"],
            kind="nudge",
            body="checkpoint",
        )
        with self.assertRaises(store_module.TransitionError):
            store.transition_message(message["message_id"], "applied")

        claimed = self.claim(store, message["message_id"], now=10, lease_seconds=30)
        self.assertEqual(claimed["state"], "dispatching")
        self.receipt(store,
            message_id=message["message_id"],
            stage="accepted",
            verification_state="reported",
            source="hermes-gateway",
            dispatch_token=claimed["dispatch_token"],
            receipt_id="receipt-transition-accepted",
            detail={"status": "accepted"},
            now=11,
        )
        self.receipt(store,
            message_id=message["message_id"],
            stage="delivered",
            verification_state="observed",
            source="agent-dock",
            receipt_id="receipt-transition-delivered",
            detail={"checkpoint": "tool-result"},
            now=12,
        )
        applied = self.receipt(store,
            message_id=message["message_id"],
            stage="applied",
            verification_state="observed",
            source="verification-ledger",
            receipt_id="receipt-transition-applied",
            detail={"message_id": message["message_id"]},
            now=13,
        )
        self.assertEqual(applied["message_state"], "applied")
        with self.assertRaises(store_module.TransitionError):
            self.receipt(store,
                message_id=message["message_id"],
                stage="rejected",
                verification_state="failed",
                source="hermes-gateway",
                receipt_id="receipt-transition-late-rejected",
                detail={"reason": "late"},
            )
        self.assertEqual(store.get_message(message["message_id"])["state"], "applied")

    def test_acceptance_requires_current_lease_and_receipts_are_idempotent(self):
        store = self.open_store()
        self.addCleanup(store.close)
        run = store.attach_run(
            profile="jarvis",
            session_id="s-lease-receipt",
            runtime_profile=self.RUNTIME_PROFILE,
            runtime_session_id=self.RUNTIME_SESSION_ID,
            source="desktop-session",
        )
        message = store.enqueue_message(
            message_id="msg-lease-receipt-001",
            run_id=run["run_id"],
            kind="nudge",
            body="checkpoint",
        )
        with self.assertRaises(store_module.ValidationError):
            self.receipt(store,
                message_id=message["message_id"],
                stage="unknown",
                verification_state="unverified",
                source="agent-dock",
                receipt_id="   ",
            )
        with self.assertRaises(store_module.LeaseError):
            self.receipt(store,
                message_id=message["message_id"],
                stage="accepted",
                verification_state="observed",
                source="hermes-gateway",
                receipt_id="receipt-no-claim",
            )
        claimed = self.claim(
            store, message["message_id"], dispatch_token="current-token", now=100, lease_seconds=30
        )
        with self.assertRaises(store_module.LeaseError):
            self.receipt(store,
                message_id=message["message_id"],
                stage="accepted",
                verification_state="observed",
                source="hermes-gateway",
                dispatch_token="stale-token",
                receipt_id="receipt-stale-claim",
                now=101,
            )
        accepted = self.receipt(store,
            message_id=message["message_id"],
            stage="accepted",
            verification_state="observed",
            source="hermes-gateway",
            dispatch_token=claimed["dispatch_token"],
            receipt_id="receipt-current-claim",
            detail={"status": "queued"},
            now=102,
        )
        duplicate = self.receipt(store,
            message_id=message["message_id"],
            stage="accepted",
            verification_state="observed",
            source="hermes-gateway",
            dispatch_token=claimed["dispatch_token"],
            receipt_id="receipt-current-claim",
            detail={"status": "queued"},
            now=103,
        )
        self.assertEqual(accepted["receipt_id"], duplicate["receipt_id"])
        self.assertEqual(len(store.list_receipts(run_id=run["run_id"])), 1)

    def test_single_store_is_safe_across_worker_threads_and_terminal_runs_do_not_regress(self):
        store = self.open_store()
        self.addCleanup(store.close)
        run = store.attach_run(profile="jarvis", session_id="s-thread", source="desktop-session")
        errors: list[Exception] = []

        def read_from_worker() -> None:
            try:
                store.get_run(run["run_id"], profile="jarvis", session_id="s-thread")
            except Exception as exc:  # pragma: no cover - assertion captures the exact failure.
                errors.append(exc)

        worker = threading.Thread(target=read_from_worker)
        worker.start()
        worker.join(timeout=5)
        self.assertEqual(errors, [])
        store.observe_run(run["run_id"], profile="jarvis", session_id="s-thread", status="completed")
        with self.assertRaises(store_module.TransitionError):
            store.observe_run(run["run_id"], profile="jarvis", session_id="s-thread", status="working")

    def test_claim_lease_has_one_winner_and_expired_lease_can_be_reclaimed(self):
        store = self.open_store()
        self.addCleanup(store.close)
        run = store.attach_run(
            profile="jarvis",
            session_id="s-4",
            runtime_profile=self.RUNTIME_PROFILE,
            runtime_session_id=self.RUNTIME_SESSION_ID,
            source="desktop-session",
        )
        message = store.enqueue_message(
            message_id="msg-lease-001",
            run_id=run["run_id"],
            kind="nudge",
            body="wait for checkpoint",
        )
        first = self.claim(
            store,
            message["message_id"],
            dispatch_token="dispatch-a",
            now=100,
            lease_seconds=10,
        )
        self.assertEqual(first["dispatch_token"], "dispatch-a")
        with self.assertRaises(store_module.LeaseError):
            self.claim(
                store,
                message["message_id"],
                dispatch_token="dispatch-b",
                now=105,
                lease_seconds=10,
            )
        reclaimed = self.claim(
            store,
            message["message_id"],
            dispatch_token="dispatch-b",
            now=111,
            lease_seconds=10,
        )
        self.assertEqual(reclaimed["dispatch_token"], "dispatch-b")
        self.assertEqual(reclaimed["claim_count"], 2)

        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def claim_from_other_store(token: str) -> None:
            other = self.open_store()
            try:
                barrier.wait()
                try:
                    self.claim(
                        other,
                        message["message_id"],
                        dispatch_token=token,
                        now=200,
                        lease_seconds=10,
                    )
                except store_module.LeaseError:
                    outcomes.append("lost")
                else:
                    outcomes.append("won")
            finally:
                other.close()

        # Finish the current lease so both concurrent callers contend for a queued message.
        store.reclaim_expired(now=222)
        left = threading.Thread(target=claim_from_other_store, args=("dispatch-c",))
        right = threading.Thread(target=claim_from_other_store, args=("dispatch-d",))
        left.start()
        right.start()
        left.join(timeout=5)
        right.join(timeout=5)
        self.assertEqual(sorted(outcomes), ["lost", "won"])

    def test_details_are_redacted_bounded_and_event_search_is_scoped_and_bounded(self):
        store = self.open_store()
        self.addCleanup(store.close)
        run = store.attach_run(
            profile="jarvis",
            session_id="s-5",
            runtime_profile=self.RUNTIME_PROFILE,
            runtime_session_id=self.RUNTIME_SESSION_ID,
            source="desktop-session",
            objective="token=super-secret " + ("objective " * 100),
        )
        message = store.enqueue_message(
            message_id="msg-search-001",
            run_id=run["run_id"],
            kind="ask",
            body="private transcript body",
        )
        secret = "api_key=super-secret-value password=hunter2 C:\\Users\\Alice\\private\\file.txt"
        claimed = self.claim(store, message["message_id"], dispatch_token="redaction-dispatch")
        self.receipt(
            store,
            message_id=message["message_id"],
            stage="accepted",
            verification_state="reported",
            source="hermes-gateway",
            dispatch_token=claimed["dispatch_token"],
            receipt_id="receipt-redaction-accepted",
            source_id="C:\\Users\\Alice\\private\\gateway.sqlite",
            detail={"diagnostic": secret, "padding": "x" * 20_000},
        )
        events = store.search_events(
            run_id=run["run_id"],
            q="accepted",
            kind="receipt_recorded",
            limit=2,
        )
        self.assertLessEqual(len(events), 2)
        self.assertTrue(events)
        serialized = json.dumps(events, sort_keys=True)
        self.assertNotIn("super-secret-value", serialized)
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("private\\file.txt", serialized)
        self.assertNotIn("private transcript body", serialized)
        self.assertLessEqual(
            max(len(str(event["detail"])) for event in events),
            store_module.MAX_DETAIL_CHARS,
        )
        self.assertNotIn("super-secret", store.get_run(run["run_id"])["objective"])

    def test_observation_state_validation_and_unknown_reconciliation_records_are_preserved(self):
        store = self.open_store()
        self.addCleanup(store.close)
        run = store.attach_run(profile="jarvis", session_id="s-6", source="desktop-session")
        observed = store.observe_run(
            run["run_id"],
            profile="jarvis",
            session_id="s-6",
            status="working",
            heartbeat_at=50,
            detail={"current_action": "tool call"},
        )
        self.assertEqual(observed["status"], "working")
        with self.assertRaises(store_module.TransitionError):
            store.observe_run(run["run_id"], status="bogus")

        result = store.reconcile_runs(
            [
                {
                    "run_id": "unknown-run",
                    "profile": "jarvis",
                    "session_id": "unknown-session",
                    "status": "working",
                }
            ],
            now=60,
        )
        self.assertEqual(result["unknown"], ["unknown-run"])
        self.assertIsNotNone(store.get_run(run["run_id"]))
        self.assertTrue(
            store.search_events(run_id=run["run_id"], kind="run_observed")
        )
        self.assertTrue(
            store.search_events(kind="unknown_observation", q="unknown-run")
        )


if __name__ == "__main__":
    unittest.main()
