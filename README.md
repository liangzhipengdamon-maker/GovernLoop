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

## Quick Start

Install GovernLoop Core and expose the same universal GovernLoop skill to the
coding agents you use:

```bash
git clone https://github.com/liangzhipengdamon-maker/GovernLoop.git
cd GovernLoop
sh install.sh
```

The installer first installs the checkout-independent Core runtime under
`~/.governloop/`, then asks which agents you use:

```text
Which agents do you use?
  1) WorkBuddy
  2) OpenCode
  3) Claude Code
  4) Codex
  5) DeepSeek Harness
```

For WorkBuddy, OpenCode, Claude Code, and Codex, the installer exposes the
**same installed universal skill** through the agent's native user skill
directory. It does not create per-agent protocol forks and it refuses to
overwrite an existing user-owned skill.

**After a first install or any upgrade, restart/reload the selected coding agent
once before using GovernLoop.** A session that performed the install may still
have an older skill cached. The installer prints `AGENT_RELOAD_REQUIRED` when
this applies; do not continue GovernLoop setup in that same agent session.

DeepSeek Harness uses its native plugin mechanism instead of a copied generic
skill. The installer prints the adapter command:

```bash
dsh plugin --profile <name> add governloop-dsh@0.1.1
```

After restarting/reloading the selected agent, open your coding project and ask
it to use GovernLoop:

```text
Use GovernLoop for this task.
```

The skill drives the installed `governloop` CLI internally. Normal users do not
need to manage session-manager or Neutral Relay paths manually.

Prerequisites: Chrome running with CDP (`--remote-debugging-port=9233`) and an
open ChatGPT conversation. The first session asks for the exact ChatGPT
conversation URL when required. Common supported forms include:

```text
https://chatgpt.com/c/<conversation-id>
https://chatgpt.com/g/<project-or-gpt-id>/c/<conversation-id>
```

The Project/custom-GPT URL should be passed as-is; the skill must not rewrite or
pre-reject it based on cached instructions. Conversation binding remains
temporary session state.

## Agent integrations

| Agent | User-facing entry point |
|---|---|
| **WorkBuddy** | GovernLoop skill / `/governloop` |
| **OpenCode** | GovernLoop skill |
| **Claude Code** | GovernLoop skill |
| **Codex** | GovernLoop skill |
| **DeepSeek Harness** | GovernLoop-DSH native adapter |

All agents share one session model: repo → task → session → conversation →
checkpoints → evidence → end. See `docs/AGENT_INTEGRATIONS.md`.

## CLI (under the skill)

The stable installed execution interface is `~/.governloop/bin/governloop`.
Skills and agents use it for `new`, `bind`, `checkpoint`, `status`, `end`, and
`doctor`. It remains available for diagnostics and automation, but it is not the
primary day-to-day user interface.

Guides: `docs/QUICK_START.md`, `docs/USAGE.md`,
`docs/MULTI_PROJECT_WORKFLOW.md`.

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
python3 -m unittest discover -s tools/neutral-relay/tests
python3 -m unittest tests.test_install_agent_skills
```

## License

[Apache-2.0](LICENSE). See `AGENTS.md` for working rules.
