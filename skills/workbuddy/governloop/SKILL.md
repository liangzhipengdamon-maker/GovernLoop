---
name: governloop
description: >-
  First-class GovernLoop command entrypoint. Run "/governloop" to auto-create a
  session for the current repository and task (session id <PROJECT>-<TASK>-<DATE>
  is generated automatically), bind a ChatGPT conversation URL once per session,
  and report review checkpoints (NEW_BLOCKER / UNEXPECTED_STATE /
  BEFORE_DESTRUCTIVE_ACTION / REVIEW_REQUIRED / FINAL_VERIFICATION) with evidence
  attachments to the bound conversation through the GovernLoop Neutral Relay.
  Use when the user types /governloop (new|status|bind|end) or needs a GovernLoop
  review session / checkpoint reporting / session routing for the current repo.
agent_created: true
---

# GovernLoop session manager + checkpoint reporter

Normal user workflow (user types only two commands):

```text
cd <project>
/governloop          # creates/resumes the session, asks once for the conversation URL if missing
work normally        # (agent reports checkpoints automatically)
/governloop end      # optional FINAL_VERIFICATION + removes temp session state
```

For agents that expose GovernLoop as a native skill rather than a slash command,
the equivalent user request is simply: `Use GovernLoop for this task.`

## Commands

| Command | Behavior |
|---|---|
| `/governloop` | Create or resume a session for the current repo. Detects repo (git origin → `owner/repo`), detects task (env issue id → branch → `--title` → deterministic slug), and generates the session id `<PROJECT>-<TASK>-<YYYY-MM-DD>`. If no conversation URL is bound, print `USER_CONVERSATION_SELECTION_REQUIRED` and ask the user once. |
| `/governloop status` | Show repo, task, session id, conversation bound (yes/no), last checkpoint, temp state path. |
| `/governloop bind <conversation-url>` | Store the ChatGPT URL in the temp session state only. Never writes the canonical config. Optionally CDP-verifies the conversation is open. |
| `/governloop checkpoint <TYPE> [--message ...\|--message-file ...] [--attach PATH ...]` | Report a review checkpoint (text + evidence attachments) to the bound conversation via the Neutral Relay. |
| `/governloop end [--final] [--attach ...]` | Send `FINAL_VERIFICATION` if `--final` and bound, then remove the temp session state. Never modifies the canonical config. |

## Session rules (mandatory)

1. The conversation URL is **task/session-level state**. Ask the user **once per
   session**; reuse it for every checkpoint in the same session; never write it
   to `~/.governloop/relay/config.json`.
2. Reuse an existing session only when: same repo + same task/session + valid
   temp state exists. Never inherit a conversation URL across unrelated sessions
   or repos (new session starts unbounded).
3. Session state lives at `/tmp/governloop-session-<SESSION_ID>.json`
   (override with `GOVERLOOP_STATE_DIR`). Request/response/config temp files for
   checkpoints are also written there.
4. `/governloop end` removes the temp state; the canonical routing config is
   never touched.

## Conversation URL runtime truth

Do not pre-reject a user-provided ChatGPT conversation URL from remembered or
cached instructions. Pass the exact URL to the currently installed GovernLoop
CLI and treat that CLI result as runtime truth.

Current Core accepts both common conversation shapes:

```text
https://chatgpt.com/c/<conversation-id>
https://chatgpt.com/g/<project-or-gpt-id>/c/<conversation-id>
```

The second form is used by ChatGPT Project/custom-GPT conversation pages. Keep
the full URL intact; do not rewrite it to `/c/<id>` before binding. If the CLI
rejects a URL, surface its actual error instead of inventing an older workaround.

## Install / upgrade reload rule

If installation or upgrade output includes `AGENT_RELOAD_REQUIRED`, do not
continue GovernLoop setup in that same running agent session. Tell the user to
restart/reload the agent once, then continue from `Use GovernLoop for this task.`
The files may already be updated on disk while the current agent session still
has an older skill cached in memory.

## Checkpoint reporting

Automatically report (text + relevant evidence attachments to the bound
conversation) when any of these occur:

- `NEW_BLOCKER`
- `UNEXPECTED_STATE`
- `BEFORE_DESTRUCTIVE_ACTION`
- `REVIEW_REQUIRED`
- `FINAL_VERIFICATION`

Ordinary progress must NOT be sent to the conversation (avoid noise). Only the
five checkpoint types are sent.

Evidence attachment policy (see `references/policy.md` for the full contract):

- Before attaching: file exists → relevant → secret scan → record
  filename/size/sha256. The script refuses missing files and files containing
  secret patterns (PATs, `sk-`, `AKIA`, `Bearer`, ...) — for secret-bearing
  evidence, create a `.redacted` copy and attach only that.
- Never attach: `.env`, tokens, credential backups, browser profiles, caches,
  `node_modules`, secret configs, irrelevant raw logs.
- A local path written in the text does NOT count as attachment delivery.
- Success = `TEXT_RELAY: PASS` AND all required attachments delivered; any
  attachment failure → `CHECKPOINT_DELIVERY_INCOMPLETE` (never a false
  COMPLETE), and the relay is not invoked.

## Invocation

The agent runs the bundled script directly (do not make the user do this):

```bash
python3 <skill-dir>/scripts/governloop_session.py <subcommand> [args]
```

The installed stable CLI `~/.governloop/bin/governloop` is also valid and should
be preferred when the universal installed skill is being used by OpenCode,
Claude Code, Codex, or another agent integration.

Environment:

- `GOVERLOOP_STATE_DIR` — session state dir (default `/tmp`)
- `GOVERLOOP_CDP_PORT` — CDP port (default `9233`, falls back to the canonical
  config's `runtime.cdp_port`, then 9233)
- `GOVERLOOP_RELAY_PATH` — path to `neutral_relay.py` (default
  `~/Documents/02_other_projects/GovernLoop-workspace/repos/GovernLoop/tools/neutral-relay/neutral_relay.py`)
- `LINEAR_ISSUE_ID` / `GITHUB_ISSUE_ID` / `ISSUE_ID` / `TASK_ID` — task identity
  from the current task context (highest priority)

Exit codes: `0` success, `1` error (incl. `CHECKPOINT_DELIVERY_INCOMPLETE`),
`3` `USER_CONVERSATION_SELECTION_REQUIRED`.

## Workflow for the agent

1. On `/governloop` (or equivalent native-skill request): run `new`. If it prints
   `USER_CONVERSATION_SELECTION_REQUIRED`, ask the user for the exact ChatGPT
   conversation URL **once**, then run `bind <url>`. Do not pre-validate against
   cached URL rules; use the currently installed CLI result. Confirm CDP target
   open before the first real checkpoint.
2. During work: when a checkpoint type occurs, run
   `checkpoint <TYPE> --message "<concise status>" --attach <evidence...>`
   (attach only relevant, secret-safe evidence; max a few files).
3. On `/governloop end`: run `end --final --attach <final-report> <manifest>`
   if a final report is appropriate, otherwise `end`. Verify the temp state file
   is gone and the canonical config was untouched.

See `QUICK_START.md` for the user-facing guide and `references/policy.md` for
the full session-routing / checkpoint / attachment-delivery contract.
