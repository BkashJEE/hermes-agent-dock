# Hermes Agent Dock — product contract

## Main goal

Give a Hermes Desktop user a fast, direct route to any configured specialist profile and a safe way to communicate with an already-running Hermes session without restarting, replacing, or duplicating it. The Dock is an on-demand floating card by default, with a visible **Dock**/**Undock** control that switches between two supported public `PANES_AREA` contributions. The selected mode persists in plugin-scoped storage; only the docked mode occupies workspace height and participates in native workspace reflow.

## Acceptance evidence

1. A disk desktop plugin loads without modifying Hermes core.
2. The widget uses only the public `PANES_AREA` contribution. Its default mode is an explicit floating card with `data: { placement: 'floating', anchor: 'top-right', width: '380px', height: '540px', uncloseable: true }`; the visible **Dock**/**Undock** control disposes and re-registers the contribution as either floating or the supported docked `data: { placement: 'bottom', dock: { pane: 'workspace', pos: 'bottom' }, height: '42vh', minHeight: '18rem', maxHeight: '70vh', uncloseable: true }`. The selected mode persists, floating mode does not alter workspace geometry, and docked mode preserves the native divider, reflows Browser and the main workspace, leaves Files independent, remains keyboard-accessible, and keeps the message composer reachable at its compact limit.
3. The backend enumerates actual Hermes profiles rather than hard-coding Dad's agents, and the compact dropdown exposes every discovered profile without horizontal clipping.
4. A user can select a profile, start from the model saved during that profile's Hermes setup, choose another model that Hermes reports as available through the same configured/authenticated provider, start a direct session, receive its final response in the Dock, and continue using the returned session ID. The Dock never expands unrelated providers into a global catalog.
5. A user can switch to another profile and start a second job while the first profile keeps working; jobs, drafts, histories, sessions, cancellation, and completion notifications remain profile-scoped.
6. The implementation launches a fixed runner argv with `shell=False`, sends exact JSON over stdin, validates profile/provider/model/session inputs, and returns bounded sanitized diagnostics.
7. Normal conversation never creates a Kanban card.
8. Enabling **Assign task** sends a strict JSON boolean and valid `request_id`, creates exactly one idempotent card on `executive-organization`, links it to the Dock job and accountable profile, and synchronizes its lifecycle. Missing IDs and coercible non-boolean values are rejected.
9. A captured agent response is added to the card and settled as `blocked / needs_input` for CEO verification rather than auto-completed.
10. A generated Web Audio chime plays only for a newly observed unlock, never for historical achievements on first load. Mute is persistent; progress-only changes and relocks do not chime.
11. Uninstall first verifies that Hermes reports the plugin `not enabled` (or the legacy `disabled` status), then moves the plugin-specific install directories to a timestamped backup by default. Failure to confirm disablement leaves installed code untouched; permanent purge is explicit.
12. Repository is public under MIT only after static tests, backend/runner tests, isolated install/uninstall verification, live Desktop chat/assignment verification, artifact cleanup, and secret review pass.
13. Completed/error/cancelled jobs and request reservations have a defined retry window: retained for up to one hour, subject to a 200-terminal-job cap; active jobs are never evicted.
14. Every new conversation bubble persists and displays the viewer's local date, time to the second, and timezone. Legacy messages without a stored timestamp are labeled unavailable rather than assigned fabricated history.
15. Transient status-read failures retain active-job identity and continue reconciliation; the UI clears a reservation only after a terminal response or structured not-found result.
16. Lost POST responses reconcile through the stable `request_id`; redaction failures return generic local-log guidance rather than raw runner diagnostics.
17. A user can attach up to four signature-validated PNG/JPEG/GIF/WebP/BMP images (10 MB each, 25 MB total). Bytes remain ephemeral, runner paths stay under the selected profile's `images/agent-dock` directory, and cleanup runs after success, error, timeout, or cancellation.
18. The selected agent row and collapsed launcher render the compact 20 px **Working** orb only while one or more real jobs are active. Terminal/idle states remove it; reduced-motion users receive a static frame; hidden/offscreen instances stop animating.
19. On a host exposing the public `pet.actions` contribution area, a plain primary click on the in-window pet invokes the same idempotent Dock toggle as the native launchers. Pet drag and Shift-click pop-out remain host-owned, and older hosts ignore the optional pet contribution while preserving status-bar and command-palette access.
20. Blind cross-channel transcript merging is not part of v0.3.0. Telegram and Agent Dock retain channel-scoped transcripts; live attachment uses explicit profile/session/run identity rather than a matching display name.
21. Live attachment requires an exact `(runtime profile, stable session ID, runtime session ID, Dock run ID)` binding obtained from `session.active_list`; private session previews are not copied into Dock state.
22. The profile-local SQLite ledger runs in WAL mode with foreign keys, bounded busy timeout, additive schema versioning, stable message IDs, one-winner dispatch leases, restart recovery, and immutable terminal receipt states.
23. ASK dispatches only to an idle live session through `prompt.submit`; NUDGE uses `session.steer`; confirmed REDIRECT uses `session.redirect`; confirmed Stop uses `session.interrupt`. The UI never claims per-run Pause/Resume because Hermes has no verified contract for it.
24. REDIRECT and Stop require explicit confirmation. Every control request uses `inherit-only` authority and cannot expand tools, credentials, filesystem scope, approvals, or provider access.
25. `queued`, `dispatching`, `accepted`, `delivered`, and `applied` are distinct. The Dock does not label a gateway-accepted request as applied without an explicit consumer-originated receipt.
26. Attached history, privacy-reduced events, status observations, verification evidence, and optional Kanban synchronization are run-scoped and survive a Desktop backend restart.

## Supported specialist routing

- Profile discovery: backend calls Hermes' profile registry.
- Direct execution: the fixed `dock_runner.py` imports Hermes CLI `chat()` under the selected profile context.
- Transport: the backend writes one exact JSON request to runner stdin and reads one bounded JSON result.
- Continuation: the validated session ID is supplied to Hermes `chat()` for the selected profile.
- Each send runs outside the active orchestrator session, so a busy default profile does not serialize the specialist call.
- No `--yolo`, no implicit hook approval, no token/credential logging, and no arbitrary command strings.

## Widget information architecture

- Closed: plain-clicking the in-window pet opens Agent Dock when `pet.actions` is supported; the persistent native status-bar launcher continues to show Agent Dock and profile-keyed activity on every host.
- Floating mode (default): when Agent Dock is opened, the card appears at the host-supported top-right anchor without changing workspace geometry. Its header exposes **Dock**; in docked mode the same control reads **Undock** and the bottom tile reflows Browser and the main workspace while Files remains independent.
- Mode persistence: `dock-mode` is stored through the plugin's namespaced `ctx.storage`; a close/reopen keeps the user's floating or docked choice.
- Open header: Agent Dock, selected profile, busy/ready state, mode control, and mute.
- Working indicator: one compact 20 px solving canvas in a single cyan hue, with contrast-adjusted dark/light variants, tied to actual active-job state; idle dots stay unchanged and the status launcher retains the concurrent-agent count.
- Agent picker: compact dropdown containing every discovered profile.
- Model selector: compact per-profile control that starts on the saved model, offers only alternatives from that profile's configured/authenticated Hermes provider, persists the Dock choice per profile, and shows a deterministic **Workload tier** for each model. Unknown provider models remain selectable by their authoritative Hermes IDs and capability metadata.
- Conversation: compact user/assistant bubbles with persistent local date/time metadata; launcher mode displays final responses only.
- Composer: multiline input, compact **Attach image** text control and previews, Enter sends, Shift+Enter inserts a newline, explicit **Assign task** opt-in, and cancel while running.
- Parallel work: each profile owns its draft and active job; switching profiles never cancels or blocks another profile, and host notifications announce completion or failure.
- Achievement notification: a newly unlocked achievement appears as a temporary tier-aware flashcard; historical achievement browsing stays in the standalone Achievements page.

## Tier visual grammar

All surfaces use Hermes theme variables; tier colors are created with `color-mix` against theme tokens rather than fixed hex values.

- Copper: warm mix from `--ui-warm`.
- Silver: neutral mix from `--ui-text-secondary`.
- Gold: accent mix from `--ui-accent`.
- Diamond: cool mix from `--ui-accent-secondary`.
- Olympian: strongest accent/foreground treatment.

## Data boundaries

- Messages and responses live in Hermes sessions owned by the selected profile.
- The dock keeps only per-profile session IDs, local UI history with message timestamps and attachment metadata, mode (`dock-mode`), mute state, and the last-seen achievement timestamp in plugin storage. Base64 image bytes are never persisted there. The backend retains at most 200 terminal job records for up to one hour to support bounded retry idempotency.
- Achievement integration reads only the local `hermes-achievements/scan_snapshot.json` if present.
- Explicitly assigned messages also create a linked local Hermes Kanban card on `executive-organization`.
- No telemetry, analytics, or independent external service is added by the plugin itself; selected profiles retain their configured model-provider behavior and costs.
- The control ledger stores exact operator intervention bodies because they are required for durable delivery, but event and receipt projections omit message bodies and redact secrets and private paths. The database is profile-local and never merges identities by display name.

## Non-goals for v0.3.0

- OS-level always-on-top window or private DOM overlay/interception.
- Streaming partial tokens.
- Non-image files, voice input, public posting, payments, or automatic approvals.
- Automatic task inference from ordinary conversation or automatic completion without Dad/CEO verification.
- Blind merging of Telegram, Agent Dock, group, DM, or thread transcripts based only on a matching profile display name.
