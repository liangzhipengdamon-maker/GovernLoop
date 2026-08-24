# GovernLoop

GovernLoop is a lightweight local-agent ↔ ChatGPT review loop for real project
work. It is **agent-agnostic**: the same session model works from WorkBuddy,
OpenCode, Claude Code, Codex, DeepSeek Harness (via
[GovernLoop-DSH](https://github.com/liangzhipengdamon-maker/GovernLoop-DSH)), or
any local coding agent.

## Product boundary

**Borrow only what the bridge needs. Nothing beyond the bridge.**

GovernLoop only adds the governance capabilities strictly required to bridge a
local Agent with ChatGPT Web safely and reliably. Governance outside this bridge
boundary is out of scope.

If the target Agent/runtime already provides a required bridge capability
natively, GovernLoop uses or adapts that native capability instead of duplicating
it. Native runtime responsibilities such as session lifecycle, agent lineage,
transcripts, sandboxing, approvals, or multi-agent coordination remain native
unless a concrete bridge requirement proves a missing capability.

## Status

**v0.1.x — stable bridge; delivery reconciliation and complete-response
recovery are verified in the current `main`.**

A local agent sends a request through the Neutral Relay to an already-open
ChatGPT conversation over Chrome DevTools Protocol (CDP), waits for the
assistant turn, and writes the complete response to a local output file — with
session/task detection, conversation binding, review checkpoints, and evidence
delivery. Delivery is confirmed by request-correlated read-back; reply
completion is gated on the ChatGPT completion UI (stop button gone + copy/rate
icons present), with a system-auto token-free screenshot fallback on truncation.

## Works with

| Agent | Entry point |
|---|---|
| **WorkBuddy** | `/governloop` slash command (`skills/workbuddy/governloop/`) |
| **OpenCode** | skill (`skills/opencode/governloop/`) |
| **Claude Code / Codex / any local agent** | session manager CLI or Neutral Relay directly |
| **DeepSeek Harness** | via GovernLoop-DSH adapter |

All agents share one session model: repo → task → session → conversation →
checkpoints → evidence → end. See `docs/AGENT_INTEGRATIONS.md`.

## Quick Start

Install once, then use it from any project:

```bash
./scripts/install.sh                 # Phase 2B installer (runtime bundle + /governloop)
cd <your-project> && /governloop     # new session + bind ChatGPT URL once
/governloop status                   # optional
/governloop end                      # FINAL_VERIFICATION + temp state cleanup
```

Generic agents invoke the same session manager directly:

```bash
python3 skills/workbuddy/governloop/scripts/governloop_session.py new
python3 skills/workbuddy/governloop/scripts/governloop_session.py bind https://chatgpt.com/c/<id>
python3 skills/workbuddy/governloop/scripts/governloop_session.py checkpoint REVIEW_REQUIRED --message "..." --attach <evidence>
python3 skills/workbuddy/governloop/scripts/governloop_session.py end
```

Prerequisites: Chrome running with CDP (`--remote-debugging-port=9233`) and an
open ChatGPT conversation. Guides: `docs/QUICK_START.md` (3 commands + FAQ),
`docs/USAGE.md` (full reference), `docs/MULTI_PROJECT_WORKFLOW.md`.

## Neutral Relay

Canonical implementation: `tools/neutral-relay/neutral_relay.py`
(`--request-file`, `--output-file`, `--config-file`, `--wait-timeout`,
`--attachment`, `--conversation-url`, `--cdp-port`; diagnostics
`--sse-diag`, screenshot fallback dir `--screenshot-dir`). A transport run
succeeds only when the relay exits 0, prints `Success: Wrote response to …`,
and writes the complete assistant response. Full contract:
`docs/architecture/neutral-relay-checkpoint-delivery.md`.

## Tests

```bash
python3 -m unittest discover -s tools/neutral-relay/tests   # relay + session-manager suite
```

## License

[Apache-2.0](LICENSE). See `AGENTS.md` for working rules.
