# Hermes Agent Dock — product contract

## Main goal

Give a Hermes Desktop user a fast, direct route to any configured specialist profile without waiting for the active orchestrator. The interface is a compact native right-sidebar contribution that participates in workspace reflow and the host pane stack.

## Acceptance evidence

1. A disk desktop plugin loads without modifying Hermes core.
2. The widget uses the public `PANES_AREA` contribution with `data: { placement: 'right' }`, opens above Files, preserves Hermes's native vertical and horizontal dividers, reflows the workspace, and remains keyboard-accessible.
3. The backend enumerates actual Hermes profiles rather than hard-coding Dad's agents, and the compact dropdown exposes every discovered profile without horizontal clipping.
4. A user can select a profile and one of the models already configured for that profile's available providers, start a direct session, receive its final response in the Dock, and continue using the returned session ID.
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

## Supported specialist routing

- Profile discovery: backend calls Hermes' profile registry.
- Direct execution: the fixed `dock_runner.py` imports Hermes CLI `chat()` under the selected profile context.
- Transport: the backend writes one exact JSON request to runner stdin and reads one bounded JSON result.
- Continuation: the validated session ID is supplied to Hermes `chat()` for the selected profile.
- Each send runs outside the active orchestrator session, so a busy default profile does not serialize the specialist call.
- No `--yolo`, no implicit hook approval, no token/credential logging, and no arbitrary command strings.

## Widget information architecture

- Closed: the persistent native status-bar launcher shows Agent Dock and profile-keyed activity.
- Open header: Agent Dock, selected profile, busy/ready state, and mute.
- Agent picker: compact dropdown containing every discovered profile.
- Model picker: per-profile session override limited to model IDs already configured for that profile's provider; “Profile default” keeps the profile's own model.
- Conversation: compact user/assistant bubbles with persistent local date/time metadata; final responses only in v0.1.
- Composer: multiline input, Enter sends, Shift+Enter inserts a newline, explicit **Assign task** opt-in, and cancel while running.
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
- The dock keeps only per-profile session IDs, local UI history with message timestamps, mute state, and the last-seen achievement timestamp in plugin storage. The backend retains at most 200 terminal job records for up to one hour to support bounded retry idempotency.
- Achievement integration reads only the local `hermes-achievements/scan_snapshot.json` if present.
- Explicitly assigned messages also create a linked local Hermes Kanban card on `executive-organization`.
- No telemetry, analytics, or independent external service is added by the plugin itself; selected profiles retain their configured model-provider behavior and costs.

## Non-goals for v0.1

- Floating overlay or OS-level always-on-top window.
- Streaming partial tokens.
- Files, voice input, public posting, payments, or automatic approvals.
- Automatic task inference from ordinary conversation or automatic completion without Dad/CEO verification.
