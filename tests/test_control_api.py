from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agent_dock_control_api",
    ROOT / "backend" / "dashboard" / "plugin_api.py",
)
api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(api)


class ControlApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        self.addCleanup(self.temp_dir.cleanup)
        self.home_patch = patch.object(api, "get_hermes_home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.profiles_patch = patch.object(api, "_allowed_profiles", return_value={"default", "jarvis"})
        self.profiles_patch.start()
        self.addCleanup(self.profiles_patch.stop)
        if api._CONTROL_STORE is not None:
            api._CONTROL_STORE.close()
        api._CONTROL_STORE = None
        api._CONTROL_STORE_HOME = None
        self.addCleanup(self.close_store)

    def close_store(self) -> None:
        if api._CONTROL_STORE is not None:
            api._CONTROL_STORE.close()
            api._CONTROL_STORE = None
            api._CONTROL_STORE_HOME = None

    def attach(self):
        return api.attach_control_run(
            api.AttachRunRequest(
                request_id="request-control-001",
                profile="jarvis",
                runtime_profile="jarvis",
                runtime_session_id="runtime-session-1",
                session_id="20260809_210000_stable",
                title="Live implementation",
                objective="Continue the existing objective",
                permission_scope="inherit-only",
                status="working",
            )
        )

    def test_attach_requires_exact_runtime_profile_binding(self):
        with self.assertRaises(HTTPException) as raised:
            api.attach_control_run(
                api.AttachRunRequest(
                    profile="jarvis",
                    runtime_profile="default",
                    runtime_session_id="runtime-1",
                    session_id="stable-1",
                )
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_control_lifecycle_is_durable_idempotent_and_receipted(self):
        run = self.attach()
        request = api.ControlMessageRequest(
            message_id="message-control-001",
            run_id=run["run_id"],
            profile="jarvis",
            session_id="20260809_210000_stable",
            kind="nudge",
            body="Keep the same objective; verify the focused tests.",
            confirmed=False,
            permission_scope="inherit-only",
        )
        queued = api.enqueue_control_message(request)
        duplicate = api.enqueue_control_message(request)
        self.assertEqual(queued["message_id"], duplicate["message_id"])
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["orchestrator_sync"]["state"], "unavailable")

        claimed = api.claim_control_message(
            queued["message_id"],
            api.ClaimMessageRequest(
                dispatcher_id="desktop:jarvis",
                profile="jarvis",
                session_id="20260809_210000_stable",
                runtime_profile="jarvis",
                runtime_session_id="runtime-session-1",
                lease_seconds=30,
            ),
        )
        self.assertEqual(claimed["state"], "dispatching")
        receipt = api.record_control_receipt(
            queued["message_id"],
            api.ReceiptRequest(
                receipt_id="receipt-control-accepted",
                state="accepted",
                source="hermes-gateway",
                verification="observed",
                profile="jarvis",
                session_id="20260809_210000_stable",
                runtime_profile="jarvis",
                runtime_session_id="runtime-session-1",
                dispatch_token=claimed["dispatch_token"],
                detail={"method": "session.steer", "gateway_status": "accepted"},
            ),
        )
        self.assertEqual(receipt["message_state"], "accepted")

        selected = api.control_run(run["run_id"], "jarvis", "20260809_210000_stable")
        self.assertEqual(selected["runtime_session_id"], "runtime-session-1")
        self.assertEqual(selected["messages"][0]["state"], "accepted")
        self.assertEqual(selected["receipts"][0]["verification_state"], "observed")
        self.assertTrue(selected["events"])

    def test_runtime_rebind_revokes_stale_claim_and_receipt_authority(self):
        run = self.attach()
        rebound = api.rebind_control_run(
            run["run_id"],
            api.RebindRunRequest(
                profile="jarvis",
                session_id="20260809_210000_stable",
                old_runtime_profile="jarvis",
                old_runtime_session_id="runtime-session-1",
                runtime_profile="jarvis",
                runtime_session_id="runtime-session-2",
                permission_scope="inherit-only",
            ),
        )
        self.assertEqual(rebound["runtime_session_id"], "runtime-session-2")
        queued = api.enqueue_control_message(
            api.ControlMessageRequest(
                message_id="message-after-rebind",
                run_id=run["run_id"],
                profile="jarvis",
                session_id="20260809_210000_stable",
                kind="nudge",
                body="Use only the new runtime.",
            )
        )
        with self.assertRaises(HTTPException) as stale_claim:
            api.claim_control_message(
                queued["message_id"],
                api.ClaimMessageRequest(
                    dispatcher_id="desktop:jarvis",
                    profile="jarvis",
                    session_id="20260809_210000_stable",
                    runtime_profile="jarvis",
                    runtime_session_id="runtime-session-1",
                ),
            )
        self.assertEqual(stale_claim.exception.status_code, 404)

        claimed = api.claim_control_message(
            queued["message_id"],
            api.ClaimMessageRequest(
                dispatcher_id="desktop:jarvis",
                profile="jarvis",
                session_id="20260809_210000_stable",
                runtime_profile="jarvis",
                runtime_session_id="runtime-session-2",
            ),
        )
        stale_receipt = api.ReceiptRequest(
            receipt_id="receipt-after-rebind-stale",
            state="accepted",
            source="hermes-gateway",
            verification="observed",
            profile="jarvis",
            session_id="20260809_210000_stable",
            runtime_profile="jarvis",
            runtime_session_id="runtime-session-1",
            dispatch_token=claimed["dispatch_token"],
        )
        with self.assertRaises(HTTPException) as rejected_receipt:
            api.record_control_receipt(queued["message_id"], stale_receipt)
        self.assertEqual(rejected_receipt.exception.status_code, 404)

        accepted = api.record_control_receipt(
            queued["message_id"],
            stale_receipt.model_copy(
                update={
                    "receipt_id": "receipt-after-rebind-current",
                    "runtime_session_id": "runtime-session-2",
                }
            ),
        )
        self.assertEqual(accepted["message_state"], "accepted")
        selected = api.control_run(run["run_id"], "jarvis", "20260809_210000_stable")
        self.assertEqual(selected["runtime_session_id"], "runtime-session-2")
        self.assertEqual(
            len([event for event in selected["events"] if event["kind"] == "run_rebound"]),
            1,
        )

    def test_redirect_confirmation_and_permission_monotonicity_are_enforced(self):
        run = self.attach()
        with self.assertRaises(HTTPException) as missing_confirmation:
            api.enqueue_control_message(
                api.ControlMessageRequest(
                    message_id="message-redirect-001",
                    run_id=run["run_id"],
                    profile="jarvis",
                    session_id="20260809_210000_stable",
                    kind="redirect",
                    body="Change the objective",
                    confirmed=False,
                )
            )
        self.assertEqual(missing_confirmation.exception.status_code, 409)

        with self.assertRaises(HTTPException) as permission_expansion:
            api.enqueue_control_message(
                api.ControlMessageRequest(
                    message_id="message-permission-001",
                    run_id=run["run_id"],
                    profile="jarvis",
                    session_id="20260809_210000_stable",
                    kind="ask",
                    body="Use a new credential",
                    confirmed=False,
                    permission_scope="admin",
                )
            )
        self.assertEqual(permission_expansion.exception.status_code, 400)

        confirmed = api.enqueue_control_message(
            api.ControlMessageRequest(
                message_id="message-redirect-002",
                run_id=run["run_id"],
                profile="jarvis",
                session_id="20260809_210000_stable",
                kind="redirect",
                body="Change the objective",
                confirmed=True,
            )
        )
        self.assertIsNotNone(confirmed["confirmed_at"])

    def test_unknown_profiles_and_cross_session_mutations_are_rejected(self):
        with self.assertRaises(ValueError):
            api.ReceiptRequest(
                receipt_id="   ",
                state="accepted",
                source="hermes-gateway",
                verification="observed",
                profile="jarvis",
                session_id="stable",
                runtime_profile="jarvis",
                runtime_session_id="runtime-session-1",
            )
        with self.assertRaises(HTTPException) as unknown:
            api.attach_control_run(
                api.AttachRunRequest(
                    profile="fabricated",
                    runtime_profile="fabricated",
                    runtime_session_id="runtime-invented",
                    session_id="stable-invented",
                )
            )
        self.assertEqual(unknown.exception.status_code, 404)

        run = self.attach()
        with self.assertRaises(HTTPException) as mismatch:
            api.enqueue_control_message(
                api.ControlMessageRequest(
                    message_id="message-cross-session-001",
                    run_id=run["run_id"],
                    profile="jarvis",
                    session_id="another-session",
                    kind="ask",
                    body="Read another session",
                    confirmed=False,
                )
            )
        self.assertEqual(mismatch.exception.status_code, 404)

        legacy = api._control_store().attach_run(
            profile="retired",
            runtime_profile="retired",
            runtime_session_id="legacy-runtime",
            session_id="legacy-session",
            source="desktop-session",
        )
        with self.assertRaises(HTTPException) as retired_observation:
            api.observe_control_run(
                legacy["run_id"],
                api.ObserveRunRequest(
                    profile="retired",
                    session_id="legacy-session",
                    status="working",
                ),
            )
        self.assertEqual(retired_observation.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
