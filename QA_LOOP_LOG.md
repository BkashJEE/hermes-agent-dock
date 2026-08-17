# Agent Dock — QA Loop Log

Loop definition: `dock-qa-loop` skill. Cadence: daily via cron.
Standing rule: PR for every change; merge when tests green, without asking.

## Iteration 1 — 2026-08-16

### Baseline (bugs)
→ plugin.js syntax: OK · Node: 34/34 · Python: 110/110 · py_compile: OK · git diff --check: clean

### UI/UX (floating card, sanitized screenshot)
1. Model row unlabeled; value truncated ("5.6 Sol…") — low contrast, purpose must be inferred.
2. Agent row shows IDLE twice (status + dropdown side) — status-vs-action collision.
3. Assign-task rendered as a faint text link next to disabled Clear — reads as dead/broken; no on/off state.
4. Dock + speaker icon: no label, tooltip, or visible state.
5. Single-tab Chat strip adds hierarchy without navigation value; right-side context duplicates the selectors.
6. No profile-add/manage affordance visible; two status dots have no legend.
→ Verdict: top section under-labeled; bottom action row buries the two important toggles.

### Features (gaps vs own API surface)
→ Job-management API (GET /jobs, assign-after, retry) is backend-only — no UI surfaces in plugin.js (ledger view, assign button, retry button). Known gap; next build candidate.

### Comparison (competitors, 2026-08-16 scan)
→ OpenClaw: local-first, 100k+ stars, strongest messaging-platform integration; privacy posture = self-hosted.
→ Manus (Meta, ~$2B): long-horizon cloud tasks, multi-agent orchestration; no desktop access, closed source, highest lock-in.
→ Claude Code: CLI dev focus; multi-agent experimental/limited.
→ Dock differentiators: local profile routing without orchestrator, Kanban accountability (assign-after), durable privacy-reduced ledger, Desktop-native. Gaps to close: streaming events (roadmap), cross-channel continuity (roadmap), UI for job management (new).

### Actions taken
→ Fixed README: assign-after Kanban-failure status documented as 503 (was 400).
→ Created `dock-qa-loop` skill + scheduled daily cron.
→ Baseline committed on `feat/job-management-api` (pending push: no GitHub credential on host).

### Next iteration priorities
1. UI surfaces for ledger/assign-after/retry (biggest feature gap).
2. Model-row label + agent-row status cleanup (cheapest UX wins).
3. Assign-task toggle → real switch with visible state.
