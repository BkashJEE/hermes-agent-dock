# Agent Dock restart-reconciliation contract

Status: implementation contract

## Main goal

After a complete Agent Dock backend or Hermes Desktop restart, an accepted job must remain discoverable under the same validated canonical profile and request identity. Work whose terminal outcome cannot be proven becomes `interrupted`; it is never silently replayed or presented as completed.

## User outcome

- A transport retry after restart resolves to the original durable job instead of creating a duplicate.
- The Dock stops showing an orphaned job as actively working.
- Uncertain work is shown as interrupted, with no claim that the process survived.
- Starting new work remains an explicit user action.

## Durable identity

The reservation key is the exact canonical `(profile_id, request_id)` pair produced by the API's existing validation boundary: profile IDs are trimmed, lowercased, and verified against the installed profile inventory; request IDs are trimmed and format-validated. The store does not perform presentation-oriented normalization. Display labels, titles, model names, and prompt similarity are not identity signals.

Each execution attempt has an internal random token. State transitions and terminal publication compare-and-swap against the exact current token so a stale worker cannot finalize a reconciled job.

Stable Hermes session identity, when known, is stored separately from job and attempt identity. It does not authorize automatic continuation.

## State contract

```text
starting -> running -> finalizing -> done
                 \-> error
starting|queued|running -> cancelling -> cancelled
starting|queued|running|finalizing --restart--> interrupted
cancelling --restart--> cancelled
```

`done`, `error`, `cancelled`, and `interrupted` are terminal and monotonic. No terminal state can return to an active state.

## Reservation ordering

1. Validate and derive the exact canonical profile-scoped request key.
2. In a SQLite transaction, return an existing reservation before fallible provider/model validation.
3. Validate a genuinely new request outside the reservation transaction.
4. In a second transaction, recheck the key and reserve only if still absent.
5. Start exactly one local worker only for the caller that created the reservation.

A new invalid request creates no durable row. An existing authoritative retry remains resolvable during a temporary catalog failure.

## Startup reconciliation

Opening the backend-owned store starts a new execution generation and transactionally reconciles unfinished rows:

- `starting`, `queued`, `running`, and `finalizing` become `interrupted`;
- `cancelling` becomes `cancelled`;
- the old attempt token is invalidated;
- reconciliation records bounded timestamps and a safe status summary;
- no worker is launched and no prompt is replayed.

A process that still holds an old token cannot publish completion or settle a linked Kanban task after reconciliation.

External Kanban calls never run while a SQLite write transaction is open. The
winning caller first commits the durable reservation, then creates the
idempotent Kanban task, then attaches its identity with exact attempt-token CAS.
Concurrent duplicates may wait for that bounded attachment but cannot repeat
the side effect. Assignment failure becomes a durable terminal `error`; it does
not erase the accepted reservation or start a worker.

## Privacy boundary

The ledger may store only bounded job metadata:

- job ID;
- validated canonical profile ID and request ID;
- provider/model selection;
- reasoning, fast, assignment, and image-count flags;
- status and timestamps;
- internal attempt token;
- stable session ID when known;
- safe error summary;
- Kanban identity when applicable.

The ledger must never store prompt/message text, response bodies, image bytes or data URLs, attachment paths, process commands, raw stdout/stderr, credentials, or private filesystem paths. Public API responses additionally omit attempt tokens, process handles, and internal fields.

## Crash windows

- **Before reservation commit:** no job was accepted; retry may create it.
- **After reservation commit but before worker start:** restart reconciliation marks it interrupted; retry returns the same job and does not start it.
- **During execution:** outcome is unknown; mark interrupted and do not replay.
- **During finalization before terminal commit:** terminal proof is absent; mark interrupted.
- **After terminal commit:** preserve the terminal state across restart.
- **During cancellation:** reconcile to cancelled and never relaunch.

## Explicit non-goals

This slice does not:

- resurrect an operating-system process;
- automatically retry or continue a model turn;
- recover an answer body that was never durably published;
- merge Telegram and Agent Dock transcripts;
- infer completion from a Hermes session file or elapsed time;
- weaken profile/provider authentication;
- add Pause/Resume or token streaming.

## Acceptance evidence

- Fresh store instance returns the original job.
- Every pre-restart active state reconciles truthfully without worker launch.
- Duplicate submission after restart bypasses fallible validation and creates no second thread.
- Two concurrent first submissions produce one reservation and one worker.
- The same request ID under different canonical profiles creates distinct jobs.
- Equivalent profile/request spelling at the API boundary resolves to the same canonical reservation; display labels never participate.
- Invalid new submissions create no reservation.
- Terminal states are monotonic.
- Stale attempt tokens cannot finalize or settle external state.
- Cancellation survives restart.
- `/jobs/{id}` falls back to durable state after process-memory dictionaries are cleared.
- Public payload and raw SQLite scans contain no prohibited content.
- Focused and full Python tests, Node tests when applicable, syntax checks, diff checks, privacy scan, and independent closeout all pass.
