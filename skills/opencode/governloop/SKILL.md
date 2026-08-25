---
name: governloop
description: >-
  Use when the user says "Use GovernLoop for this task": create a
  task/session-level review session for the current repository, bind a ChatGPT
  conversation URL once per session, and report review checkpoints
  (NEW_BLOCKER / UNEXPECTED_STATE / BEFORE_DESTRUCTIVE_ACTION /
  REVIEW_REQUIRED / FINAL_VERIFICATION) with evidence attachments to the bound
  conversation through the Neutral Relay.
---

# GovernLoop OpenCode Skill

GovernLoop has two layers:

1. **Session manager CLI** (shared, agent-agnostic): the same script the
   WorkBuddy `/governloop` command uses —
   `skills/workbuddy/governloop/scripts/governloop_session.py`. It handles
   session creation, routing, and checkpoint reporting.
2. **Neutral Relay transport**: `tools/neutral-relay/neutral_relay.py` — the
   CDP transport that the session manager invokes under the hood.

Use the session manager for the normal workflow; fall back to direct relay
transport only when a lower-level transport is needed.

## Session manager (recommended entry)

Invoke the shared CLI from the GovernLoop repository checkout (do not maintain
a second copy of the script):

```bash
python3 skills/workbuddy/governloop/scripts/governloop_session.py <command>
```

Commands:

| Command | Behavior |
|---|---|
| `new` | Detect repo (`remote.origin.url` → `owner/repo`) and task (env issue id → branch → `--title` → generated slug); auto-generate session id `<PROJECT>-<TASK>-<YYYY-MM-DD>`. If no conversation URL is bound, print `USER_CONVERSATION_SELECTION_REQUIRED` (exit 3) — ask the user once. |
| `status` | Show repo, task, session id, conversation bound (yes/no), last checkpoint, temp state path. |
| `bind <conversation-url>` | Store the URL in temp session state only (never the canonical config). |
| `checkpoint <TYPE> [--message ...\|--message-file ...] [--attach PATH ...]` | Report a checkpoint (text + evidence attachments) to the bound conversation. |
| `end [--final] [--attach ...]` | Send `FINAL_VERIFICATION` if `--final` and bound, then remove temp session state. |

Normal workflow:

```text
cd <repo>
/governloop equivalent: python3 .../governloop_session.py new
python3 .../governloop_session.py bind <user-provided-url>   # only when asked once
work normally
python3 .../governloop_session.py checkpoint <TYPE> --message "<concise status>" --attach <evidence...>
python3 .../governloop_session.py end --final               # when the task ends
```

Session rules (mandatory):

- The conversation URL is **task/session-level state**. Ask the user once per
  session; reuse it for every checkpoint in the session; never write it to
  `~/.governloop/relay/config.json`.
- Reuse an existing session only when same repo + same task/session + valid
  temp state exists. Never inherit a conversation URL across unrelated sessions
  or repos.
- Temp state lives at `/tmp/governloop-session-<SESSION_ID>.json`
  (override: `GOVERLOOP_STATE_DIR`). `end` removes it; the canonical routing
  config is never modified.

Checkpoint reporting:

- Only the five checkpoints are sent: `NEW_BLOCKER`, `UNEXPECTED_STATE`,
  `BEFORE_DESTRUCTIVE_ACTION`, `REVIEW_REQUIRED`, `FINAL_VERIFICATION`.
  Ordinary progress must not spam the conversation.
- Evidence attachments: file exists → relevant → secret scan → record
  filename/size/sha256. The script refuses missing files and secret-bearing
  files (PATs, `sk-`, `AKIA`, `Bearer`, ...) — for secret-bearing evidence,
  create a `.redacted` copy and attach only that. Never attach `.env`, tokens,
  credential backups, browser profiles, caches, `node_modules`, secret configs,
  or irrelevant raw logs.
- A local path written in the text is NOT delivery. Success requires
  `TEXT_RELAY: PASS` AND all required attachments delivered; any attachment
  failure → `CHECKPOINT_DELIVERY_INCOMPLETE` (never a false COMPLETE).

Environment: `GOVERLOOP_STATE_DIR` (default `/tmp`), `GOVERLOOP_CDP_PORT`
(default `9233`), `GOVERLOOP_RELAY_PATH` (default points at the repo's
`tools/neutral-relay/neutral_relay.py`).

Exit codes: `0` success, `1` error (incl. `CHECKPOINT_DELIVERY_INCOMPLETE`),
`3` `USER_CONVERSATION_SELECTION_REQUIRED`.

## Direct relay transport (lower level)

Canonical relay: `tools/neutral-relay/neutral_relay.py`.

Current relay arguments: `--request-file`, `--output-file`, `--config-file`,
`--wait-timeout` (default 900), `--dry-run`, `--attachment` (repeatable),
`--conversation-url`, `--cdp-port` (session-level overrides, never persisted).

A request contains `REVIEW_REQUEST_ID`, `REPO`, and the ordinary
natural-language task. Do not add or require `PR`, `HEAD`, `ACK`, `RESULT`, or
`FINAL` unless the user's task explicitly needs them.

Configure the target repository route with an already-open ChatGPT
`conversation_url` and its CDP port. A routing dry run may be used before real
transport.

Before real transport, confirm the target ChatGPT conversation. Do not guess or
silently reuse a conversation URL that the user has not selected or previously
authorized for this task. If no target conversation is already explicitly
established, ask the user for the ChatGPT conversation URL before sending.

For real transport, invoke the canonical relay with the request, output, config,
and wait timeout. Read the output only after the relay exits successfully.

Transport success requires all of the following: relay exit code 0; stdout
contains `Success: Wrote response to ...`; the relay created the output file;
and the output contains the complete assistant response. External CDP probes are
diagnostic only and do not substitute for relay read-back.

If relay transport fails, report the real failure point instead of bypassing the
relay and presenting probe-read content as a successful GovernLoop result.

This Minimal Transport Recovery baseline does not include the historical
`governloop start`, `governloop doctor`, `setup-task-scope`, host-confirm, or
lifecycle-authority workflows. Do not call those as part of this skill.

The Neutral Relay is transport only. It does not itself authorize repository
mutation, PR creation, merge, release, or deployment.

## Shared agent safety contract

Before performing repository or lifecycle actions, read and follow
`docs/ops/AGENT_SAFETY_CONTRACT.md` and the repository-level `AGENTS.md`.

In particular, implementation, commit/push, PR creation, Ready, merge, and
deploy/release are separate authorization stages. Never infer a later-stage
authorization from PASS, relay success, test success, mergeability, Ready state,
task completion, or an earlier-stage authorization.

For Ready, merge, deploy, or release, verify the current remote target and exact
HEAD where applicable, then require explicit user authorization for that stage.
If the next-stage authorization is absent, STOP and report the current state
rather than continuing automatically.

## Installation for opencode

Copy this directory to `~/.config/opencode/skills/governloop/` (user scope) or
`.opencode/skills/governloop/` (project scope). The session-manager script is
shared from the repo checkout
(`skills/workbuddy/governloop/scripts/governloop_session.py`); if you install
without the repo, copy that script next to this skill and set
`GOVERLOOP_RELAY_PATH` to a `neutral_relay.py` checkout.
