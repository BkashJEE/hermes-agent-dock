# Agent Dock current architecture

> **Historical baseline — not current acceptance authority.** This file captures the pre-control-plane architecture. Agent Dock v0.3.0 subsequently added the durable SQLite control store, exact run/runtime bindings, queued interventions, leases, receipts, and privacy-reduced events. Use `PRODUCT_SPEC.md`, `backend/dashboard/control_store.py`, and `docs/agent-chat-dock/v0.4-limitations-roadmap.md` for current behavior and active work.

## Scope

This document describes the repository at the pre-control-plane baseline. It separates observed behavior from proposed behavior.

## Observed topology

```text
Hermes Desktop
  plugin.js (build-free ESM)
    ├─ public PANES_AREA floating/bottom contribution
    ├─ ctx.storage: profile UI history, drafts, session IDs, active job reservations
    └─ ctx.rest('/...')
          ↓ authenticated plugin-scoped HTTP
backend/dashboard/plugin_api.py
    ├─ Hermes profile inventory
    ├─ profile/provider/model validation
    ├─ process-local _JOBS and _REQUEST_JOBS maps
    ├─ optional executive-organization Kanban card
    └─ subprocess.Popen(shell=False)
          ↓ exact JSON over stdin
backend/dashboard/dock_runner.py
    └─ profile-scoped HermesCLI.chat(...), optionally resuming a stored session ID
```

Sources: `plugin.js`; `backend/dashboard/plugin_api.py`; `backend/dashboard/dock_runner.py`.

## Existing identity

A Dock execution currently has several partially related identifiers:

- `profile`: validated Hermes profile name and profile-home boundary.
- `request_id`: renderer-generated retry key, unique only within a profile while retained in process memory.
- `job.id`: process-local Agent Dock job ID.
- `session_id`: Hermes persisted conversation ID returned after a CLI job starts or resumes.
- `kanban_task_id`: optional durable task identity when Assign task is enabled.

There is no single durable run record binding all five identifiers. The selected profile is an authority/configuration boundary, but it is not proof of attachment to a currently live Desktop or delegated run.

## Existing execution behavior

`POST /jobs` starts a new `dock_runner.py` child. The runner creates `HermesCLI`, optionally resumes the supplied persisted session, calls `chat()`, returns one final response, and closes the CLI. It does not attach to an in-memory gateway session. The current Dock is therefore a real profile-scoped session launcher/resumer, not a live-turn control surface.

Jobs are retained in `_JOBS`; idempotency reservations are retained in `_REQUEST_JOBS`. Both are in memory and disappear with the backend process. The UI polls `GET /jobs/{id}` and stores compact renderer history in `ctx.storage`, which is best-effort localStorage rather than authoritative operational history.

## Existing safe-intervention primitives in Hermes

The installed Hermes Desktop gateway exposes:

- `session.active_list`: live in-process Desktop/TUI sessions.
- `session.steer`: queues text through `AIAgent.steer`; Hermes injects it at a tool-result boundary without interrupting the in-flight tool call. The RPC returns `queued` or `rejected`.
- `session.redirect`: redirects an active model turn while preserving completed work/context; returns `redirected`, `queued`, or `rejected`.
- `session.interrupt`: cooperatively interrupts the active turn, clears queued prompts, and denies pending approvals for that session.
- `subagent.steer`: equivalent checkpoint queue for a live delegated child, with a possible `missed_steer` race reported on child completion.
- `verification.status`: best-known coding verification evidence for a session/workspace.
- `approval.respond`: resolves Hermes's existing session-scoped approval gate.

These are Desktop gateway JSON-RPC methods invoked with `host.request`. A standalone dashboard backend cannot call them directly through `ctx.rest`.

## Current safe checkpoints

Observed Hermes steer delivery points:

1. Before a new provider API call, pending steer is drained into the latest tool-role result when one exists.
2. After a tool batch, pending steer can be appended to the tool result for the next iteration.
3. A steer arriving before any tool result remains pending rather than being injected as a synthetic user message, preserving role alternation.

A successful `session.steer` response proves queue acceptance, not that the model changed its plan. The Dock must not label it `applied` without a later runtime/agent receipt.

## Persistence and proof

Durable sources already available:

- Hermes SessionDB: conversation sessions and messages.
- Hermes Kanban SQLite: tasks, runs, events, heartbeats, comments, attachments, summaries.
- Verification evidence ledger exposed by `verification.status`.
- Optional Assurance Harness: privacy-reduced hash-chained policy/tool-result ledger.

The Agent Dock currently persists none of its job/control receipts in a server-side database.

## Permission boundary

The existing runner inherits the selected profile's Hermes configuration and uses Hermes's approval hooks. It never adds `--yolo` or `--accept-hooks`. The child environment uses Hermes's audited subprocess policy when available and strips gateway/account controls in fallback mode.

A control-plane intervention must remain a message/control request only. It may not alter profile configuration, enabled tools, model authority, write roots, approval mode, credentials, or task contracts.

## Orchestrator synchronization

Only explicit Assign task creates a durable `executive-organization` Kanban card. A normal Dock conversation has no orchestrator writeback. Completed assigned jobs append a comment and block the card for Dad/CEO verification rather than auto-completing it.

There is no existing durable event for an ASK, NUDGE, or REDIRECT directed at a live run. Consequently the orchestrator can remain unaware of task-affecting intervention.

## Gaps

1. No durable canonical binding among profile, live session, run, Dock job, and Kanban task.
2. No live-run attachment in the UI.
3. No durable intervention queue or run-scoped history.
4. No idempotent intervention receipts.
5. No ASK/NUDGE/confirmed REDIRECT semantic contract.
6. No application receipt beyond RPC acceptance.
7. No mandatory orchestrator event for task-affecting messages.
8. No aggregated truthful task, heartbeat, blocker, and proof view.
9. No restart reconciliation for active Dock jobs.
10. No verified per-run Pause/Resume API. Stop/interrupt exists, but has consequential queue/approval effects.
