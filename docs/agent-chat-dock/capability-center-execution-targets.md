# ADR-004: Profile Capability Center and execution targets

**Status:** Accepted for the v0.5 feature branch

**Date:** 2026-08-20

**Decider:** Repository owner

## Context

Agent Dock can discover and run any profile registered with Hermes, but it does
not give an operator one safe place to inspect the profile's effective model,
skills, toolsets, MCP servers, approval posture, or terminal isolation. Reading
those values in the Desktop process would also risk leaking one profile's
environment into another profile.

## Decision

Add a Capability Center backed by a short-lived helper process whose
`HERMES_HOME` is set to the selected profile home. The helper uses Hermes'
configuration APIs, emits an allowlisted JSON projection, and never emits
credential values, environment values, filesystem paths, MCP commands, or MCP
arguments.

Execution targets initially support:

- `host`: Hermes' local terminal backend.
- `docker`: Hermes' Docker terminal backend with an optional bounded image
  name. Docker volumes, forwarded environment variables, and extra arguments
  are not editable from Agent Dock. A Host-to-Docker change fails closed when
  privileged Docker-only settings already exist; the UI reports only boolean
  risk categories and directs the operator to review them in Hermes.

Changing a target requires a JSON boolean `confirmed: true`, an exact profile
from the installed Hermes inventory, and one of the two allowed targets. The
write is performed by Hermes' `load_config` / `save_config` APIs inside the
profile-scoped helper. Agent Dock reports that new sessions use the change; it
does not claim a running session moved between environments.

## Options considered

### Read YAML directly in the Desktop process

Lower implementation cost, but it duplicates Hermes merge semantics and risks
cross-profile global state. Rejected.

### Call the existing profile editor RPC

It already exposes useful data, but it is a Desktop-host RPC rather than a
stable standalone-plugin boundary and it does not provide a narrow target
mutation contract. Rejected for this backend.

### Profile-scoped helper process

Slightly more process overhead, but isolation is explicit and testable. Chosen.

## Security and privacy contract

- Credential state is a boolean only.
- MCP output contains server names and enabled state only.
- Docker output contains availability, selected image, safe resource limits,
  and boolean privileged-setting categories; never mounts, forwarded
  environment names/values, or extra args.
- Target writes do not start Docker, restart Hermes, or alter live sessions.
- Paid/remote VMs, SSH, Modal, Daytona, and arbitrary container arguments are
  out of scope.

## Consequences

- Any valid Hermes profile can be scanned through one endpoint.
- Windows and macOS use the same Python and Desktop-plugin paths; Docker is
  shown as unavailable until its executable is present.
- A follow-up can add verified container launch receipts without changing the
  inventory response contract.
