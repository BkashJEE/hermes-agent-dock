## Goal

Close the collaboration loop between Hermes and the CEO/executive agents. Agent Dock could launch a job and (optionally) create a Kanban card up front, but it had no way to act on a job *after* it finished: no durable job list to inspect, no way to promote a finished run to tracked work, and no safe retry of a failed one. This adds a small, local, privacy-reduced job-management API so the Dock's jobs can be listed, assigned to the board, and re-run under a stable identity.

## Changes

- `plugin_api.py`
  - `GET /jobs?limit=N` — durable job ledger, newest-first, bounded (default 50, max 200); privacy-reduced public projection (metadata + bounded response/error; never prompt/image bytes).
  - `POST /jobs/{id}/assign` — link a terminal job to the `executive-organization` Kanban board *without* re-running it. Idempotent: the stable job id is the idempotency key, so a repeat returns the existing card rather than creating a duplicate. `404` unknown job, `409` while active, `400` if Kanban is unavailable.
  - `POST /jobs/{id}/retry` — re-run a terminal job under its stable identity with a fresh attempt. Preserves job id, profile, and `request_id`; re-validates model/provider/session against the profile catalog; respects the 4-active-session cap (`429`); terminal jobs only (`409`).
  - Shared `_create_kanban_task_for_job` helper so assign-after and up-front assign build identical cards.
- `job_store.py`
  - `reset_attempt` — race-safe re-arm of a terminal job (fresh attempt token, clears error/finished, transitions back to `starting`).
  - `link_kanban_terminal` — record a Kanban card + board on an already-finished job.
- `README.md` — "Job management API" section (endpoints, idempotency, status codes) + updated What-it-does / boundaries.

## Verification

- [x] JavaScript syntax check passes.
- [x] Node tests pass.
- [x] Python tests and compilation pass.
- [x] `git diff --check` passes.
- [x] Behavior changes have focused tests.
- [ ] UI changes were checked in floating and docked modes where applicable.

Commands and results:

```text
node --input-type=module --check < plugin.js      # OK (syntax)
node --test tests/test_dock_state.mjs             # tests 34, pass 34, fail 0
python -m unittest discover -s tests              # Ran 110 tests in 1.4s, OK
python -m py_compile backend/dashboard/*.py install.py uninstall.py   # OK
git diff --check                                  # clean
```

New tests: 9 API-level (ledger shape/bounds/privacy, assign-after idempotency + 404/409 + private-path redaction, retry active/limit/404, and an end-to-end retry→run→done that asserts the fresh message is what executes) + 6 store-level (reset_attempt fresh-token re-arm and fail-closed, link_kanban_terminal on/off terminal status). The existing `test_sqlite_ledger_contains_metadata_only` sentinel still passes — no prompt/response/image bytes in the durable ledger.

## Security and privacy

- [x] No credentials, private conversations, session titles, personal profile data, local paths, databases, telemetry, or unrelated notifications are included.
- [x] No hidden telemetry, broad filesystem scanning, or unreviewed network calls were introduced.
- [x] Screenshots and logs are privacy-sanitized.

The durable ledger stores metadata only — retry and assign-after both take their message/task from the caller by design, so no prompt is ever persisted. Kanban failures are surfaced with private paths redacted; active jobs are rejected (`409`) rather than silently re-run.

## Compatibility and risk

- Additive API on the existing FastAPI router; no route, schema, or existing behavior removed.
- Target: Windows 10 + Hermes Desktop (matches the release baseline). No migration; no release tag or metadata touched.
- Rollback: revert the single commit; the three routes disappear, prior behavior unchanged.

## Visual evidence

Not applicable — backend API + ledger only, no UI change.

## Checklist

- [x] The diff is focused and contains no unrelated refactor or formatting churn.
- [x] Documentation and product boundaries are updated when behavior changed.
- [x] I inspected and understand the complete submitted diff, including AI-assisted changes.
- [x] I did not move an existing release tag or change release metadata without maintainer direction.
