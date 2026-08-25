---
name: governloop
description: >-
  First-class GovernLoop skill entrypoint. Create or resume a session for the
  current repository and task, bind a ChatGPT conversation URL once per session,
  and report review checkpoints (NEW_BLOCKER / UNEXPECTED_STATE /
  BEFORE_DESTRUCTIVE_ACTION / REVIEW_REQUIRED / FINAL_VERIFICATION) with evidence
  attachments to the bound conversation through the installed GovernLoop Core.
  Use when the user asks to use GovernLoop or needs GovernLoop review/session
  routing for the current repo.
agent_created: true
---

# GovernLoop session manager + checkpoint reporter

Normal user workflow:

```text
open project in coding agent
"Use GovernLoop for this task."
work normally
```

The skill drives the installed CLI at `~/.governloop/bin/governloop`. The user
should not need to invoke internal Python files or know runtime paths.

## Commands

| Command | Behavior |
|---|---|
| `governloop new` | Create or resume a session for the current repo/task. If no conversation URL is bound, ask the user once. |
| `governloop status` | Show repo, task, session id, conversation bound state, last checkpoint, and temp state path. |
| `governloop bind <conversation-url>` | Store the ChatGPT conversation URL in temporary session state only and optionally CDP-check that it is open. |
| `governloop checkpoint <TYPE> ...` | Report a review checkpoint through Neutral Relay. |
| `governloop end [--final] ...` | Optionally send FINAL_VERIFICATION, then remove temporary session state. |
| `governloop doctor` | Read-only diagnostics. |

## Session rules (mandatory)

1. The conversation URL is **task/session-level state**. Ask the user **once per
   session**; reuse it for every checkpoint in the same session; never write it
   to `~/.governloop/relay/config.json`.
2. Reuse an existing session only when: same repo + same task/session + valid
   temp state exists. Never inherit a conversation URL across unrelated sessions
   or repos.
3. Session state lives at `/tmp/governloop-session-<SESSION_ID>.json`
   (override with `GOVERLOOP_STATE_DIR`).
4. `governloop end` removes temporary session state; canonical routing config is
   never touched.

## ChatGPT conversation URLs

Do not pre-reject a user-provided ChatGPT URL based on remembered or cached
rules. Pass the exact URL to the **currently installed** `governloop bind`
command and treat that command's result as runtime truth.

Current Core accepts both common conversation shapes:

```text
https://chatgpt.com/c/<conversation-id>
https://chatgpt.com/g/<project-or-gpt-id>/c/<conversation-id>
```

The second form is used by ChatGPT Project/custom-GPT conversation pages. Keep
that full URL intact; do not rewrite it to `/c/<id>` before binding.

If `governloop bind` rejects a URL, report the CLI's actual error to the user.
Do not invent a workaround from an older skill version.

## Install / upgrade reload rule

When GovernLoop Core or this skill has just been installed or upgraded from
inside a running coding-agent session, that session may still have an older
skill cached. If the installer prints `AGENT_RELOAD_REQUIRED`, stop GovernLoop
setup in that session and tell the user to restart/reload the agent once. After
restart, continue from `Use GovernLoop for this task.`

## Checkpoint reporting

Automatically report only these checkpoint types:

- `NEW_BLOCKER`
- `UNEXPECTED_STATE`
- `BEFORE_DESTRUCTIVE_ACTION`
- `REVIEW_REQUIRED`
- `FINAL_VERIFICATION`

Ordinary progress must not be sent.

Evidence attachment rules:

- file exists → relevant → secret scan → attach;
- never attach `.env`, tokens, credential backups, browser profiles, caches,
  `node_modules`, secret configs, or irrelevant raw logs;
- a local path in text does not count as attachment delivery;
- any required attachment failure means `CHECKPOINT_DELIVERY_INCOMPLETE`.

## Workflow for the agent

1. Run `~/.governloop/bin/governloop new`.
2. If it reports `USER_CONVERSATION_SELECTION_REQUIRED`, ask the user once for
   the ChatGPT conversation URL and immediately run
   `~/.governloop/bin/governloop bind "<exact-user-url>"`.
3. Trust the current CLI result rather than prior session memory.
4. During work, report only the five defined checkpoints with concise status and
   relevant secret-safe evidence.
5. At the end, run `governloop end` or `governloop end --final` when appropriate.

Environment:

- `GOVERLOOP_STATE_DIR` — temp session state dir (default `/tmp`)
- `GOVERLOOP_CDP_PORT` — CDP port (default `9233`, with installed config fallback)
- `GOVERLOOP_RELAY_PATH` — optional Neutral Relay override
- `LINEAR_ISSUE_ID` / `GITHUB_ISSUE_ID` / `ISSUE_ID` / `TASK_ID` — task identity inputs

Exit codes: `0` success, `1` error (including
`CHECKPOINT_DELIVERY_INCOMPLETE`), `3` `USER_CONVERSATION_SELECTION_REQUIRED`.

See `QUICK_START.md` for the user-facing guide and `references/policy.md` for
full routing/checkpoint/evidence contracts.
