# Security policy

Hermes Agent Dock is a local Hermes Desktop extension. It does not add telemetry or contact an external service, but it can launch a configured Hermes profile and therefore inherits that profile's model provider, tools, approvals, and costs.

## Security boundaries

- The backend accepts only profile names returned by Hermes' own profile registry.
- Agent launches use a fixed runner argument vector with `shell=False`; the exact request is delivered as JSON over `stdin=subprocess.PIPE`, never interpolated into a shell command.
- The plugin never adds `--yolo`, automatic hook approval, or a permission bypass.
- Pet toggling uses only the public `pet.actions` contribution registry. The plugin does not query private pet markup, install document-level pointer interception, or override native drag/Shift-click behavior; older hosts safely ignore the optional contribution.
- Floating and docked presentation use only public `PANES_AREA` contributions. Dock/Undock disposes the current contribution and registers the other supported placement; the plugin does not inject a private overlay, patch host layout DOM, or create an unmanaged Electron window.
- Messages are length-bounded and session IDs are format-validated. Model/provider values must match the selected profile's single saved Hermes setup; authenticated-provider catalogs are not exposed as overrides.
- Image uploads are limited to four, 10 MB each and 25 MB total. The backend decodes strict base64, requires an image MIME data URL, verifies PNG/JPEG/GIF/WebP/BMP magic bytes, ignores user filenames for storage, and passes only generated paths under the selected profile's `images/agent-dock` directory to the runner.
- Image bytes are not written to plugin storage, job records, API responses, or Kanban comments. Job-scoped temporary files are removed in `finally` after success, error, timeout, or cancellation.
- Responses and runner diagnostics are bounded and sanitized before they cross into the renderer or enter a linked Kanban comment.
- Normal chat cannot create a task. A card is created on the local `executive-organization` board only when the renderer sends the strict JSON boolean `assign_task: true` plus a valid `request_id`; strings and integers are rejected rather than coerced.
- Stable request/job identities and a Kanban idempotency key prevent retry-created duplicate cards. Assignment without a `request_id` is rejected. Terminal job/request reservations are bounded to one hour and 200 retained terminal jobs; active jobs are never evicted.
- Cancellation is checked before process spawn and again before the spawned process is attached; a process that loses the race is terminated before it can receive the request payload.
- Transient status-read failures retain the active reservation and continue reconciliation so a live token-consuming process does not become invisible or uncancellable.
- Lost POST responses reconcile with the stable `request_id`, preserving one job/card identity while the response is uncertain.
- Uninstall is fail-closed: the compact plain inventory's exact Name column must report `hermes-agent-dock` as `not enabled` or `disabled`. Missing, malformed, similarly named, and description-only matches are not confirmation.
- Runner diagnostics fail closed: if Hermes's redactor cannot be imported or raises an error, the renderer receives generic local-log guidance instead of raw stderr.
- Achievement integration reads only the local `hermes-achievements/scan_snapshot.json` file when available. Evidence/session titles are not returned by Agent Dock.
- UI settings and recent dock messages, including epoch timestamps and attachment names/metadata, stay in Hermes Desktop's namespaced plugin storage. Image data URLs remain renderer-memory-only until submission.

## Cost and approval warning

Sending a message or image from the Dock starts or resumes a real Hermes agent session under the selected profile. That may consume model tokens and may request approval for tools. Agent Dock does not approve those actions for you. Explicit assignment records the message, selected profile, saved model/provider, job identity, and final bounded response in the local Kanban database; it does not store image bytes in the card.

## Reporting a vulnerability

Please do not open a public issue containing secrets, private prompts, session content, or exploit details. Use GitHub's private vulnerability reporting for this repository when available. Include the affected version, operating system, Hermes version, reproduction steps, and the narrowest safe proof.
