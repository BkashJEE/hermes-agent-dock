# Hermes Agent Dock

A native Hermes Desktop right-sidebar pane for direct chat with configured specialist profiles—even when the active orchestrator is occupied. Normal chat stays conversational; messages become lifecycle-tracked Kanban work only when **Assign task** is explicitly enabled.

> Community project. Not an official Nous Research release.

## What it does

- Adds a compact Hermes-themed contribution to the native right-sidebar pane stack, above Files, and reuses the official BrandMark already bundled with the host app.
- Reflows the workspace instead of covering the main transcript and keeps Hermes's native horizontal pane divider available.
- Discovers actual Hermes profiles; it does not invent agents or pretend one profile is every specialist.
- Uses compact agent and model dropdowns so every configured profile is reachable without horizontal scrolling. Model choices are limited to real model IDs already configured for the selected profile's provider.
- Starts or resumes a profile-scoped Hermes session without routing through the focused orchestrator.
- Preserves a separate session ID and compact local message history per profile. Every new bubble stores and displays the viewer's local date, time to the second, and timezone; legacy messages without stored time are labeled `Date unavailable`.
- Keeps one independent active job and draft per profile, so another agent can be opened and messaged immediately while earlier agents continue in the background. Hermes Desktop notifies when each job finishes or fails.
- Displays each selected agent's final reply or a bounded actionable failure inside the Dock transcript.
- Creates a real card on the `executive-organization` Hermes Kanban board only when **Assign task** is active; ordinary chat never creates a card.
- Keeps the linked card synchronized with the Dock job and settles captured responses as `blocked / needs_input` for Dad/CEO verification rather than falsely declaring the task complete.
- Optionally plays a deduplicated achievement unlock chime; first load establishes a silent baseline and a persistent mute control is available.

## Important boundaries

- A dock message launches a **real Hermes agent session** and can consume model tokens.
- The selected profile keeps its normal tools, policies, and approval gates. Agent Dock never adds `--yolo` or `--accept-hooks`.
- v0.1 polls a background job and displays the final response; token-by-token streaming is not yet exposed by the public cross-profile Desktop plugin API.
- Transient status-read failures keep the active job visible and continue reconciliation every ten seconds; only an explicit terminal response or structured not-found result clears the reservation.
- Lost POST responses are reconciled with the stable `request_id`; an accepted backend job remains visible instead of being orphaned or duplicated.
- Recent dock messages are best-effort renderer-local data in Hermes namespaced plugin storage—not a backup system.
- Explicit assignment requires the local `executive-organization` Kanban board and a valid `request_id`. The card records the accountable profile, stable Dock job identity, and idempotency key. Request retries reuse the existing job/card during the bounded retention window; terminal jobs are retained for up to one hour, subject to a 200-job cap, while active jobs are never evicted.
- Achievement integration is optional. It reads the local `hermes-achievements/scan_snapshot.json` and intentionally omits evidence, session IDs, and session titles.

## Requirements

- Hermes Agent with Hermes Desktop.
- At least one configured Hermes profile (`hermes profile list`).
- Python 3.10+ available as `python`.
- Optional: the Hermes achievements backend for achievement cards.

## Install

```bash
git clone https://github.com/BkashJEE/hermes-agent-dock.git
cd hermes-agent-dock
python install.py
```

Fully restart Hermes Desktop after the first install and whenever backend Python files change so the Desktop process mounts the installed backend. Frontend-only `plugin.js` changes can hot-reload.

The installer:

1. Backs up an existing Agent Dock installation under `$HERMES_HOME/backups/hermes-agent-dock/`.
2. Copies `plugin.js` to `$HERMES_HOME/desktop-plugins/hermes-agent-dock/`.
3. Copies the local API to `$HERMES_HOME/plugins/hermes-agent-dock/`.
4. Enables only `hermes-agent-dock` through the Hermes CLI.
5. Writes a local install manifest with SHA-256 hashes.

No npm, pip, post-install hook, admin elevation, or package lifecycle script is used.

### Custom/isolated Hermes home

```bash
python install.py --home /path/to/hermes-home
```

For a copy-only smoke test that does not change `plugins.enabled`:

```bash
python install.py --home /path/to/temp-home --copy-only
```

## Use

1. Open Hermes Desktop.
2. Use the persistent **Agent Dock** status-bar control to open the native right-sidebar pane.
3. Pick an agent from the dropdown, then keep its profile default model or choose another configured model.
4. Send a message. `Enter` sends; `Shift+Enter` inserts a newline.
5. Leave **Assign task** off for normal conversation. Enable it before sending only when the message must create and track a real Kanban card.
6. Use **New conversation** to stop resuming that profile's previous Dock session.
7. Use the speaker icon to mute or enable unlock sounds.

The Dock participates in Hermes's native workspace reflow. Resize the right sidebar with the native vertical divider and resize Agent Dock versus Files with the native horizontal pane divider.

## Uninstall

Reversible (default):

```bash
python uninstall.py
```

This reads Hermes's compact plain inventory and verifies that the exact Name column for `hermes-agent-dock` reports `not enabled` (or the legacy `disabled` label), then moves the plugin-specific install directories to a timestamped backup. A missing, malformed, or similarly named row is not accepted as proof. If disablement cannot be confirmed, installed code is left untouched. To delete the installed code instead after verified disablement:

```bash
python uninstall.py --purge
```

Restart Hermes Desktop once if the Python backend had already been mounted.

## Architecture

```text
Hermes Desktop plugin.js
  ├─ public PANES_AREA right-side contribution + persistent status control
  ├─ ctx.storage: profile sessions, compact histories, drafts, active jobs, mute + unlock baseline
  └─ ctx.rest('/...')
       ↓ same-origin, plugin-scoped API
Hermes plugin_api.py
  ├─ hermes_cli.profiles.list_profiles()
  ├─ strict booleans, profile/provider/model validation + request idempotency
  ├─ exact JSON request over stdin to dock_runner.py (`shell=False`)
  ├─ profile-keyed jobs + cancellation checks before and after process spawn
  ├─ one-hour/200-terminal-job retention
  ├─ opt-in `executive-organization` Kanban creation/lifecycle sync
  └─ bounded sanitized response/error projection
dock_runner.py
  └─ invokes profile-scoped Hermes `chat()` and emits one bounded JSON result
```

The backend launches a fixed runner path with `shell=False` and sends the request as exact JSON over `stdin=subprocess.PIPE`. Profile names come from Hermes's registry, provider/model pairs are allowlisted from configured catalogs, session IDs are format-validated, message/response sizes are bounded, and the API accepts at most four concurrent direct jobs.

## Verify from source

```bash
node --input-type=module --check < plugin.js
node --test tests/test_dock_state.mjs
python -m unittest discover -s tests -v
python -m py_compile backend/dashboard/plugin_api.py backend/dashboard/dock_runner.py install.py uninstall.py
```

## Project files

- `plugin.js` — build-free Hermes Desktop ESM runtime.
- `backend/plugin.yaml` — standalone Hermes backend descriptor.
- `backend/dashboard/plugin_api.py` — profile/model discovery, jobs, cancellation, diagnostics, and opt-in Kanban integration.
- `backend/dashboard/dock_runner.py` — JSON-stdin profile runner and bounded result serializer.
- `install.py` / `uninstall.py` — reversible, stdlib-only lifecycle.
- `PRODUCT_SPEC.md` — acceptance contract and explicit non-goals.
- `SECURITY.md` — trust boundaries and reporting guidance.

## Roadmap

- Public SDK support for cross-profile streamed gateway events instead of final-response polling.
- Permission-request handoff inside the dock when Hermes exposes a safe plugin contract for it.
- Public SDK support for durable backend job persistence across a full Desktop process restart.

## License

MIT © 2026 BkashJEE
