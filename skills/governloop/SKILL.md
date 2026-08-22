---
name: governloop
description: >-
  Universal GovernLoop protocol skill. Use it to create a task/session-level
  review loop between the local coding agent and a ChatGPT conversation:
  detect repo + task, generate a session id, bind a ChatGPT conversation URL
  once per session, report the five review checkpoints (NEW_BLOCKER /
  UNEXPECTED_STATE / BEFORE_DESTRUCTIVE_ACTION / REVIEW_REQUIRED /
  FINAL_VERIFICATION) with evidence attachments through the Neutral Relay,
  and clean up temporary state on end. Agent-agnostic: agent-specific
  installation and invocation may be provided by agent-specific adapters;
  existing integrations remain in their current locations during the Phase 1A
  transition.
---

# GovernLoop — Universal Protocol Skill

GovernLoop connects a local coding agent (execution side) to a ChatGPT
conversation (reasoning/review side). This file is the **protocol-only**
entrypoint: it defines the session model, checkpoint protocol, relay
interaction, authorization boundary, and fail-closed rules that every agent
follows. It contains **no agent-specific installation or command binding**.
Agent-specific installation and invocation may be provided by agent-specific
adapters; existing integrations remain in their current locations during the
Phase 1A transition.

Two layers, one shared model:

1. **Session manager CLI** (agent-agnostic): `new` / `bind` / `checkpoint` /
   `status` / `end`. This is the recommended entry point for every agent.
2. **Neutral Relay transport**: the CDP transport the session manager invokes
   under the hood; may be used directly for low-level flows.

---

## 1. Core philosophy

- **Two-agent loop, not one self-review loop.** The local agent executes;
  ChatGPT reasons and reviews. GovernLoop keeps the two contexts separate.
- **Send decisions, not logs.** Ordinary progress stays local. Only state
  that changes the next decision crosses the bridge, via the five checkpoints.
- **Transport is not authority.** Relay success, review PASS, or test PASS
  never authorize repository mutation, PR creation, merge, release, or deploy.

Canonical positioning and design principles: `README.md`.

## 2. Component discovery

No per-agent permanent routing config is required. The session-manager
executable lives in the GovernLoop checkout, but **session-manager commands
run with the TARGET PROJECT as the working directory** — repo/task detection
uses `os.getcwd()`. Run from the GovernLoop checkout only when GovernLoop
itself is the target project.

- **Session manager CLI** — canonical implementation:
  `skills/workbuddy/governloop/scripts/governloop_session.py`
  (physical location is transitional; see `docs/AGENT_INTEGRATIONS.md`).
  An agent may discover this path itself (e.g. from the repo checkout);
  that is agent-side discovery state, not a GovernLoop runtime environment
  variable.
- **Neutral Relay** — canonical executable:
  `tools/neutral-relay/neutral_relay.py`
  (overridable via `GOVERLOOP_RELAY_PATH`).

Environment (all injectable):

| Variable | Purpose | Default |
|---|---|---|
| `GOVERLOOP_STATE_DIR` | temp session state dir | `/tmp` |
| `GOVERLOOP_CDP_PORT` | CDP port | `9233` |
| `GOVERLOOP_RELAY_PATH` | path to `neutral_relay.py` | installation-specific absolute path in the current runtime; override when it does not match the local GovernLoop checkout |
| `LINEAR_ISSUE_ID` / `GITHUB_ISSUE_ID` / `ISSUE_ID` / `TASK_ID` / `GOVERLOOP_TASK` | task identity (highest priority) | — |

For portable/cold-start use: the current runtime has an installation-specific
default for the relay path. Discover the local GovernLoop checkout and set
`GOVERLOOP_RELAY_PATH` explicitly when the default does not match.

## 3. Session lifecycle

```text
repo → task → session → ChatGPT conversation → checkpoints → evidence → end
```

| Command | Behavior |
|---|---|
| `new` | Detect repo (`remote.origin.url` → `owner/repo`) and task (env issue id → branch → `--title` → generated slug); auto-generate session id `<PROJECT>-<TASK>-<YYYY-MM-DD>`. If no conversation URL is bound, print `USER_CONVERSATION_SELECTION_REQUIRED` (exit 3) — ask the user once. |
| `status` | Show repo, task, session id, conversation bound (yes/no), last checkpoint, temp state path. |
| `bind <conversation-url>` | Store the URL in **temp session state only** (never the canonical config). |
| `checkpoint <TYPE> [--message ...\|--message-file ...] [--attach PATH ...]` | Report a checkpoint (text + evidence attachments) to the bound conversation. |
| `end [--final] [--attach ...]` | Send `FINAL_VERIFICATION` if `--final` and bound, then remove temp session state. |

Normal workflow (run from the target project directory):

```text
python3 <session-manager> new
python3 <session-manager> bind <user-provided-url>     # only when asked once
# work normally; report checkpoints as they occur
python3 <session-manager> checkpoint <TYPE> --message "<concise status>" --attach <evidence...>
python3 <session-manager> end --final                  # when the task ends
```

Session rules (mandatory):

- The conversation URL is **task/session-level state**. Ask the user **once
  per session**; reuse it for every checkpoint in the session; never write it
  to `~/.governloop/relay/config.json`.
- Reuse an existing session only when: same repo + same task/session + valid
  temp state exists. Never inherit a conversation URL across unrelated
  sessions or repos.
- **Ambiguous session reuse:** if an existing session's provenance is
  ambiguous — especially during concurrent/multi-agent use — do not silently
  inherit its conversation binding. Stop and ask the user whether that
  session should be reused.
- Temp state lives at `/tmp/governloop-session-<SESSION_ID>.json`
  (override: `GOVERLOOP_STATE_DIR`). `end` removes it; the canonical routing
  config is never modified.

Full contract: `docs/architecture/neutral-relay-checkpoint-delivery.md` §1,
`skills/workbuddy/governloop/references/policy.md` §1.

## 4. Checkpoint protocol

Report a checkpoint (concise text **and** supporting evidence attachments to
the same bound conversation) when any of these occur:

- `NEW_BLOCKER`
- `UNEXPECTED_STATE`
- `BEFORE_DESTRUCTIVE_ACTION`
- `REVIEW_REQUIRED`
- `FINAL_VERIFICATION`

Ordinary progress must NOT be sent (avoid noise).

Evidence attachment policy (before each file): file exists → relevant →
secret scan → record filename/size/sha256. The session manager refuses
missing files and secret-bearing files (PATs, `sk-`, `AKIA`, `Bearer`, ...) —
for secret-bearing evidence, create a `.redacted` copy and attach only that.
Never attach `.env`, tokens, credential backups, browser profiles, caches,
`node_modules`, secret configs, or irrelevant raw logs.

Full contract: `docs/architecture/neutral-relay-checkpoint-delivery.md` §2–§5,
`skills/workbuddy/governloop/references/policy.md` §2–§5.

## 5. Relay interaction

- **Recommended entry:** the session manager CLI (section 3). It builds the
  request, invokes the relay with a temp session config, and validates
  delivery.
- **Low-level fallback:** invoke the Neutral Relay directly:

  ```bash
  python3 tools/neutral-relay/neutral_relay.py \
    --request-file request.txt \
    --output-file response.md \
    --conversation-url <session-url> \
    --attachment evidence.md
  ```

  A request contains `REVIEW_REQUEST_ID`, `REPO`, and the ordinary
  natural-language task. Do not add or require `PR`, `HEAD`, `ACK`, `RESULT`,
  or `FINAL` unless the task explicitly needs them.

- **Transport success** requires all of the following: relay exit code `0`;
  stdout contains `Success: Wrote response to ...`; the relay created the
  output file; the output contains the complete assistant response. External
  CDP probes are diagnostic only and do not substitute for relay read-back.
- **Delivery confirmation:** a send is confirmed only when the composer
  clears AND the thread's user-turn count increments (PRIMARY), or — while
  SEND_PENDING — a guarded new assistant turn appears with no assistant
  streaming before the send (AUXILIARY). Once the composer clears, never
  re-click / re-upload / re-inject (duplicate-delivery risk).

Full contract: `docs/architecture/neutral-relay-checkpoint-delivery.md` §6,
`tools/neutral-relay/README.md`.

## 6. Authorization boundary

Before performing repository or lifecycle actions, read and follow
`docs/ops/AGENT_SAFETY_CONTRACT.md` and the repository-level `AGENTS.md`.

In particular:

- GovernLoop is **transport only**. Transport success does not authorize
  repository mutation, PR creation, merge, release, or deployment.
- Implementation → commit/push → PR → Ready → merge → deploy/release are
  **separate authorization stages**. Never infer a later-stage authorization
  from review PASS, relay success, test PASS, mergeability, Ready state, task
  completion, or an earlier-stage authorization.
- Before Ready, merge, deploy, or release: verify the current remote state
  and the exact target/HEAD; stop if the state drifted.
- Do not directly push/rewrite/force-push `main` without explicit
  authorization for that exact action.

## 7. Fail-closed rules

- A local path written in the text is NOT evidence delivery. Success =
  `TEXT_RELAY: PASS` AND all required attachments delivered; any attachment
  failure → `CHECKPOINT_DELIVERY_INCOMPLETE` (never a false COMPLETE), and
  the relay is not invoked.
- Upload failure is fail-closed: missing file / no file input / upload error /
  not visible in composer → abort with non-zero exit; request text is never
  sent.
- `SEND_NOT_CONFIRMED` / `SEND_PENDING_TIMEOUT`: never auto-resend. Manual
  recovery guidance never instructs re-running the same send path.
- Exit codes: `0` success, `1` error (incl. `CHECKPOINT_DELIVERY_INCOMPLETE`),
  `3` `USER_CONVERSATION_SELECTION_REQUIRED`.

## 8. Adapter boundary (what is NOT here)

- **Per-agent installation paths** (e.g. `~/.workbuddy/skills/`,
  `~/.config/opencode/skills/`) → agent-specific integration/adapters when
  present.
- **Command binding / UX** (e.g. `/governloop` slash command) →
  agent-specific integration/adapters when present.
- **Agent-specific invocation conventions** → agent-specific
  integration/adapters when present.

This file must stay agent-agnostic: if a rule applies only to one agent, it
belongs in that agent's integration/adapters when present, not here. During
the Phase 1A transition no adapters directory exists yet — existing
integrations stay in their current locations.
