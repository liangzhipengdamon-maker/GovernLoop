# GovernLoop

GovernLoop is a lightweight local-agent ↔ ChatGPT review loop for real project
work. It is **agent-agnostic**: the same session model works from WorkBuddy,
OpenCode, Claude Code, Codex, or any local coding agent.

The current stable baseline focuses on one capability: a local agent sends a
natural-language request through the Neutral Relay to an already-open ChatGPT
conversation over Chrome DevTools Protocol (CDP), waits for the assistant turn
to finish streaming, and writes the complete response to a local output file —
wrapped in a session manager that handles repo/task detection, session ids,
conversation binding, review checkpoints, and evidence delivery.

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

## Works with

| Agent | Entry point |
|---|---|
| **WorkBuddy** | `/governloop` slash command (fastest UX) |
| **OpenCode** | GovernLoop skill (`skills/opencode/governloop/`) |
| **Claude Code** | invoke the local session manager CLI |
| **Codex** | invoke the local session manager CLI |
| **Any local coding agent** | invoke the local session manager CLI or the Neutral Relay directly |

All agents share **one session model** — repo → task → session → conversation →
checkpoints → evidence → end — and the same rules: no per-agent permanent
routing config, conversation URLs stay session-level. See
`docs/AGENT_INTEGRATIONS.md` for per-agent setup.

## Current status

**v0.1.2 — Reliable attachment-message delivery confirmation.**

v0.1.2 is a reliability patch for Neutral Relay message delivery, especially review/checkpoint messages carrying evidence attachments. It strengthens send confirmation, introduces a three-state delivery model (SEND_PENDING after the composer clears, safe retry only while the draft remains in the composer, and duplicate-send protection), and clarifies manual-recovery semantics. Released as `v0.1.2` after the full relay and session-manager test suite passed on `main`.

Verified path:

```text
Local Agent
  → GovernLoop Neutral Relay
  → ChatGPT Web over CDP
  → natural-language assistant response
  → relay read-back
  → local output file
```

The transport does not require ChatGPT to return `PR`, `HEAD`, `ACK`, `RESULT`, or `FINAL` fields.

## Quick Start

Install GovernLoop once, then use it from any project.

### WorkBuddy fast path (`/governloop`)

```text
cd <your-project>

/governloop          # creates a session for this repo, asks for the ChatGPT
                     # conversation URL once — then just work normally
/governloop status   # optional: repo / task / session / bound URL / last checkpoint
/governloop end      # when done: optional FINAL_VERIFICATION + temp state cleanup
```

`/governloop` automatically:

- detects the current git repo and derives the task (issue id → branch → title);
- generates the session id `<PROJECT>-<TASK>-<YYYY-MM-DD>` — no manual session
  ids, no per-project routing config to maintain;
- binds the ChatGPT conversation URL **once per session** (temporary state
  only; the canonical config is never modified);
- reports the five review checkpoints — `NEW_BLOCKER`, `UNEXPECTED_STATE`,
  `BEFORE_DESTRUCTIVE_ACTION`, `REVIEW_REQUIRED`, `FINAL_VERIFICATION` — with
  evidence attachments to that conversation. Ordinary progress is not sent.

### Generic agent path (session manager CLI)

Any agent — Claude Code, Codex, OpenCode, or a plain local script — invokes the
**same** session manager directly:

```bash
python3 <repo>/skills/workbuddy/governloop/scripts/governloop_session.py new
python3 <repo>/skills/workbuddy/governloop/scripts/governloop_session.py bind https://chatgpt.com/c/<conversation-id>
python3 <repo>/skills/workbuddy/governloop/scripts/governloop_session.py checkpoint REVIEW_REQUIRED --message "..." --attach <evidence>
python3 <repo>/skills/workbuddy/governloop/scripts/governloop_session.py end
```

Identical session model, identical rules — same repo/task detection, auto
session id, URL once per session, five checkpoints, evidence delivery, temp
state cleanup.

Guides: `docs/QUICK_START.md` (3 commands, incl. the 8 most common questions),
`docs/USAGE.md` (full reference), `docs/AGENT_INTEGRATIONS.md` (per-agent
setup), `docs/MULTI_PROJECT_WORKFLOW.md` (using GovernLoop across many
projects).

> Need the low-level Neutral Relay instead (route config + `--request-file`)?
> That flow is documented in [Neutral Relay](#neutral-relay) below.

## See GovernLoop in action

Real workflow demo: a local coding agent sends a natural-language request to ChatGPT through GovernLoop, reads the complete assistant response back through the relay, and continues the local workflow automatically.

```text
Local Agent → GovernLoop → ChatGPT → relay read-back → Local Agent
```

This is a real recorded workflow, not a simulated demo.

[![GovernLoop live demo — click to watch the full 2-minute workflow](https://github.com/liangzhipengdamon-maker/GovernLoop/releases/download/v0.1.2/demo_poster.png)](https://liangzhipengdamon-maker.github.io/GovernLoop/assets/demo_v0.1.2.mp4)

*Click the image above to watch the full 2-minute recorded workflow on GitHub.*

## Neutral Relay

Canonical implementation:

```text
tools/neutral-relay/neutral_relay.py
```

Current CLI arguments:

```text
--request-file
--output-file
--config-file       # optional; default ~/.governloop/relay/config.json
--wait-timeout      # default: 900 seconds
--dry-run
--attachment        # evidence file(s) uploaded to the conversation before sending (repeatable)
--conversation-url  # session-level conversation override; never written to config
--cdp-port          # session-level CDP port override; never written to config
```

Required request routing fields:

```text
REVIEW_REQUEST_ID: <unique-id>
REPO: owner/repository

<ordinary natural-language task>
```

The target ChatGPT conversation must already be open in the CDP-enabled browser.

### Checkpoint evidence delivery

Review checkpoints (`NEW_BLOCKER`, `UNEXPECTED_STATE`, `BEFORE_DESTRUCTIVE_ACTION`,
`REVIEW_REQUIRED`, `FINAL_VERIFICATION`) deliver concise text **and** the supporting
evidence files as attachments to the same session-bound conversation. A local path
written in the text is not delivery. Every attachment is checked (exists -> relevant
-> secret scan -> filename/size/sha256) before upload; secret-bearing evidence is only
ever attached as a redacted copy. Any attachment failure aborts the run fail-closed —
never a false COMPLETE. Full contract: `docs/architecture/neutral-relay-checkpoint-delivery.md`.

Short real usage example (session-level target + evidence attachments):

```bash
python3 tools/neutral-relay/neutral_relay.py \
  --request-file request.txt \
  --output-file response.md \
  --conversation-url <session-url> \
  --attachment report.md \
  --attachment manifest.json
```

### Success condition

A transport run is successful only when the relay itself:

1. exits with code `0`,
2. prints `Success: Wrote response to ...`,
3. creates the output file, and
4. writes the complete assistant response to that file.

External CDP probes may be used for diagnosis, but do not substitute for relay read-back.

## OpenCode skill

A minimal OpenCode skill is maintained in:

```text
skills/opencode/governloop/SKILL.md
```

It documents the current Neutral Relay workflow only. Historical commands such as `governloop start`, `setup-task-scope`, and governance/authority workflows are not part of this recovery baseline.

## WorkBuddy skill (`/governloop`)

A first-class WorkBuddy command entrypoint is maintained in:

```text
skills/workbuddy/governloop/
├── SKILL.md                 # command contract (new/status/bind/checkpoint/end)
├── QUICK_START.md           # user-facing 3-command workflow
├── references/policy.md     # session routing + checkpoint + attachment policy
└── scripts/
    ├── governloop_session.py
    └── test_governloop_session.py
```

Normal user workflow: `cd <project>` → `/governloop` → work → `/governloop end`.
The skill auto-detects the repo and task, auto-generates the session id
`<PROJECT>-<TASK>-<YYYY-MM-DD>`, binds the ChatGPT conversation URL once per
session in temporary state only (never the canonical config), and reports the
five review checkpoints with evidence attachments through the Neutral Relay.
Install it into `~/.workbuddy/skills/governloop/` to activate the slash command.

Usage docs:

- `docs/QUICK_START.md` — user guide in 3 commands, including the 8 most
  common questions (switching projects, session ids, URLs, checkpoints,
  evidence, cleanup).
- `docs/USAGE.md` — full command/reference manual for the session manager.
- `docs/MULTI_PROJECT_WORKFLOW.md` — cross-project isolation rules (shared
  infrastructure; one session + one conversation per project).

## Local development convention

GovernLoop development follows a simple, runtime-free workflow:

- a single canonical `main` checkout is the source of truth;
- feature and fix work happens in Git worktrees, which are retired after merge;
- there is no second clone for normal development.

This repository ships as Minimal Transport — no AgentOps lifecycle runtime or
governance state machine. See `WORKTREE_LIFECYCLE.md` (local workspace) for the
full worktree convention.

## Release line

- `v0.1.0` — original public release.
- `v0.1.1` — Minimal Transport Recovery release; cross-project natural-language relay behavior verified before release.
- `v0.1.2` — current stable reliability patch for Neutral Relay delivery confirmation (strong send confirmation, SEND_PENDING, duplicate-send protection).

See `docs/ops/CURRENT_STATUS.md` and `docs/ops/RELEASE_NOTES_v0.1.2.md` for the release closure record.
