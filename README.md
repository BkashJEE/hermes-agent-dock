# Hermes Agent Dock

A native Hermes Desktop floating card by default for direct chat with configured specialist profiles—even when the active orchestrator is occupied. The visible **Dock**/**Undock** control switches between the public floating-pane and bottom-workspace contribution modes, and the selected mode persists in plugin-scoped storage. On Hermes builds that expose the public pet action area, a plain click on the in-window pet opens or closes the card; the persistent status-bar and command-palette launchers remain reliable fallbacks. Normal chat stays conversational; messages become lifecycle-tracked Kanban work only when **Assign task** is explicitly enabled.

> Community project. Not an official Nous Research release.

## What it does

- Adds a compact Hermes-themed contribution as a floating card by default (`placement: 'floating'`, `anchor: 'top-right'`, `380px × 540px`) and reuses the official BrandMark already bundled with the host app.
- The card's **Dock**/**Undock** control switches only between supported public `PANES_AREA` contributions: floating mode stays above the workspace; docked mode uses the native bottom workspace tile and divider, reflowing Browser and the main workspace while Files remains independent.
- Discovers actual Hermes profiles; it does not invent agents or pretend one profile is every specialist.
- Uses a compact agent picker and a model selector populated from the selected profile's configured/authenticated Hermes provider. It exposes that provider's available alternatives—not an unrelated global provider catalog—and labels each model with a deterministic **Workload tier**.
- Starts or resumes a profile-scoped Hermes session without routing through the focused orchestrator.
- Preserves a separate session ID and compact local message history per profile. Every new bubble stores and displays the viewer's local date, time to the second, and timezone; legacy messages without stored time are labeled `Date unavailable`.
- Keeps one independent active job and draft per profile, so another agent can be opened and messaged immediately while earlier agents continue in the background. Hermes Desktop notifies when each job finishes or fails.
- Replaces generic loading spinners with an adapted 20 px **Rubik/solving** state from Jakub Antalik's MIT-licensed Thinking Orbs: a theme-aware six-color palette (a local Agent Dock adaptation because the upstream painter is monochrome), visible only while real profile jobs are active, static under reduced-motion preferences, and paused offscreen or in hidden tabs.
- Accepts up to four local PNG, JPEG, GIF, WebP, or BMP images (10 MB each, 25 MB total) and passes them through Hermes's native `chat(..., images=...)` path. Image bytes are job-scoped and removed after completion, failure, or cancellation.
- Displays each selected agent's final reply or a bounded actionable failure inside the Dock transcript.
- Creates a real card on the `executive-organization` Hermes Kanban board only when **Assign task** is active; ordinary chat never creates a card.
- Keeps the linked card synchronized with the Dock job and settles captured responses as `blocked / needs_input` for Dad/CEO verification rather than falsely declaring the task complete.
- Optionally plays a deduplicated achievement unlock chime; first load establishes a silent baseline and a persistent mute control is available.

## Important boundaries

- A dock message launches a **real Hermes agent session** and can consume model tokens.
- The selected profile keeps its normal tools, policies, and approval gates. Agent Dock never adds `--yolo` or `--accept-hooks`.
- v0.2 polls a background job and displays the final response; token-by-token streaming is not yet exposed by the public cross-profile Desktop plugin API.
- Transient status-read failures keep the active job visible and continue reconciliation every ten seconds; only an explicit terminal response or structured not-found result clears the reservation.
- Lost POST responses are reconciled with the stable `request_id`; an accepted backend job remains visible instead of being orphaned or duplicated.
- Recent dock messages are best-effort renderer-local data in Hermes namespaced plugin storage—not a backup system.
- Pending image bytes remain only in renderer memory until submission. History stores attachment names/metadata, never base64 payloads, and the backend deletes each job's temporary image directory in `finally`.
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

### Install through your Hermes agent

Instead of rebuilding or adapting Agent Dock, give your Hermes agent the verified release and ask it to install the exact published files:

> Install Hermes Agent Dock v0.2.0 from https://github.com/BkashJEE/hermes-agent-dock for my current Hermes home. Do not recreate, rewrite, or expand the project. Inspect the release README, SECURITY.md, LICENSE, THIRD_PARTY_NOTICES.md, and `proof/live-verification.json`; confirm the repository, `v0.2.0` tag, and version metadata; run the documented tests; run `python install.py`; verify `hermes plugins list --plain --no-bundled` reports `hermes-agent-dock` enabled at v0.2.0; compare the five runtime files with the hashes in the local install manifest; and report the exact result plus whether Hermes Desktop must restart. Do not read or copy conversation content, memories, credentials, or unrelated files, and do not modify any existing profile, model, provider, tool, or approval policy.

This is an install-and-verify workflow, not a code-generation prompt. The published installer keeps a timestamped rollback backup and restores the previous desktop and backend components if replacement fails.

### Existing-agent discovery and privacy

Agent Dock does not crawl the user's Hermes workspace. Its backend calls Hermes's official `hermes_cli.profiles.list_profiles()` inventory and reads only the profile name, default-profile status, gateway status, saved model/provider, and bounded description. It does not enumerate conversation bodies, prompts, memories, skills, credentials, or arbitrary profile files.

The picker shows the profiles already configured on that Hermes installation. Selecting one starts or resumes a session under that profile's isolated Hermes home, so the profile keeps its existing tools, policies, memory, model configuration, and approval gates. Agent Dock does not create, merge, or silently reconfigure agents.

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
2. Plain-click the in-window Hermes pet to open or close the floating card on hosts that support `pet.actions`. Drag and Shift-click retain Hermes's native pet behavior. The persistent **Agent Dock** status-bar control and **Agent Dock: Toggle specialist pane** command remain fallbacks on every supported host.
3. Use **Dock** in the card header to switch to the native bottom workspace tile, or **Undock** to return to the floating card. This mode is persisted in plugin-scoped storage and is restored the next time the Dock opens.
4. Pick an agent from the dropdown. The model selector starts on that profile's saved model and offers the alternatives Hermes reports for the same configured provider/auth setup.
5. Optionally use **Attach image** for up to four supported local images, then send a message or an image-only analysis request. `Enter` sends; `Shift+Enter` inserts a newline.
6. Leave **Assign task** off for normal conversation. Enable it before sending only when the message must create and track a real Kanban card.
7. Use **New conversation** to stop resuming that profile's previous Dock session.
8. Use the speaker icon to mute or enable unlock sounds.

By default the Dock is a floating card and does not change workspace geometry. In docked mode, Browser and the main workspace reflow around the bottom tile, which is resized with Hermes's native horizontal divider; Files remains independent.

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
  ├─ public PANES_AREA floating/docked contributions + pet/status/palette toggles
  ├─ persistent `dock-mode` in ctx.storage; floating default, bottom workspace on demand
  ├─ ctx.storage: profile sessions, compact histories/attachment metadata, drafts, active jobs, mute + unlock baseline
  ├─ renderer-memory-only image selection and compact previews
  └─ ctx.rest('/...')
       ↓ same-origin, plugin-scoped API
Hermes plugin_api.py
  ├─ hermes_cli.profiles.list_profiles()
  ├─ strict booleans, profile/saved-model/image validation + request idempotency
  ├─ signature-checked job-scoped image files with unconditional cleanup
  ├─ exact JSON request over stdin to dock_runner.py (`shell=False`)
  ├─ profile-keyed jobs + cancellation checks before and after process spawn
  ├─ one-hour/200-terminal-job retention
  ├─ opt-in `executive-organization` Kanban creation/lifecycle sync
  └─ bounded sanitized response/error projection
dock_runner.py
  └─ invokes profile-scoped Hermes `chat(..., images=...)` and emits one bounded JSON result
```

The backend launches a fixed runner path with `shell=False` and sends the request as exact JSON over `stdin=subprocess.PIPE`. Profile names come from Hermes's registry, provider/model pairs must match the profile's saved setup, session IDs are format-validated, message/response/image sizes are bounded, and the API accepts at most four concurrent direct jobs.

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
- `LICENSE` — Agent Dock's MIT license grant.
- `THIRD_PARTY_NOTICES.md` — attribution and license text for the adapted Thinking Orbs painter.

## Roadmap

- Public SDK support for cross-profile streamed gateway events instead of final-response polling.
- Permission-request handoff inside the dock when Hermes exposes a safe plugin contract for it.
- Public SDK support for durable backend job persistence across a full Desktop process restart.

## License

[MIT](LICENSE) © 2026 BkashJEE
