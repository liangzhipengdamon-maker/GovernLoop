# QUICK START — Install once, use the skill

GovernLoop is agent-agnostic. Install the Core runtime once, expose the same
universal GovernLoop skill to the coding agents you use, restart/reload those
agents once so they load the installed skill, then work from the agent normally.

## 1. Install GovernLoop

```bash
git clone https://github.com/liangzhipengdamon-maker/GovernLoop.git
cd GovernLoop
sh install.sh
```

The installer performs two layers in order:

1. installs the checkout-independent GovernLoop Core runtime under
   `~/.governloop/`;
2. asks which coding agents you use and exposes the same installed universal
   skill through their native user skill directories.

Interactive selection:

```text
Which agents do you use?
  1) WorkBuddy
  2) OpenCode
  3) Claude Code
  4) Codex
  5) DeepSeek Harness
```

For automation or a non-interactive install, the agent selection can be supplied
explicitly:

```bash
GOVERLOOP_INSTALL_AGENTS=workbuddy,codex sh install.sh
```

Supported skill destinations:

| Agent | Native user skill path |
|---|---|
| WorkBuddy | `~/.workbuddy/skills/governloop` |
| OpenCode | `~/.config/opencode/skills/governloop` |
| Claude Code | `~/.claude/skills/governloop` |
| Codex | `~/.codex/skills/governloop` |

Each destination points to the same installed skill at
`~/.governloop/current/skills/governloop`. The installer refuses to overwrite an
existing user-owned skill.

DeepSeek Harness is different: DSH already provides a native plugin mechanism,
so GovernLoop does not copy a generic skill into DSH. Install the adapter through
DSH instead:

```bash
dsh plugin --profile <name> add governloop-dsh@0.1.1
```

## 2. Restart/reload the selected coding agent once

After a first install **or an upgrade**, restart/reload WorkBuddy, OpenCode,
Claude Code, or Codex before using GovernLoop. A coding-agent session that
performed the install may still have the previous skill cached in memory even
though the new files are already on disk.

The installer prints this explicitly as:

```text
AGENT_RELOAD_REQUIRED
Restart or reload each selected coding agent before using GovernLoop.
```

Do not continue GovernLoop setup in the same agent session that performed the
install or upgrade.

## 3. Open your project and use GovernLoop

After the reload, open any project in the selected agent and say:

```text
Use GovernLoop for this task.
```

The skill uses the installed `governloop` CLI underneath. Normal users do not
need to invoke the session manager or Neutral Relay by path.

## 4. Bind the ChatGPT conversation when asked

A new session starts without inheriting another project's conversation. When
GovernLoop asks for a reviewer conversation, provide the exact ChatGPT
conversation URL shown in the browser address bar.

Supported common forms include both ordinary conversations and ChatGPT
Project/custom-GPT conversations:

```text
https://chatgpt.com/c/<conversation-id>
https://chatgpt.com/g/<project-or-gpt-id>/c/<conversation-id>
```

Do not manually rewrite the Project/custom-GPT URL. The skill passes the exact
URL to the currently installed Core and treats the CLI result as runtime truth.

That URL is temporary session state. It is not written to permanent canonical
configuration and is not reused by an unrelated session.

## Runtime prerequisites

GovernLoop expects:

- Chrome running with CDP, normally `--remote-debugging-port=9233`;
- an already logged-in ChatGPT Web session;
- the target ChatGPT conversation open and available for binding.

GovernLoop does not own Chrome startup or browser credentials.

## What the skill does

The universal skill drives one shared lifecycle:

```text
repo → task → session → conversation → checkpoints → evidence → end
```

It detects the repository/task, creates or resumes the session, binds the
conversation once when needed, reports the five review checkpoints, and cleans
up temporary session state at the end.

The five checkpoints are:

- `NEW_BLOCKER`
- `UNEXPECTED_STATE`
- `BEFORE_DESTRUCTIVE_ACTION`
- `REVIEW_REQUIRED`
- `FINAL_VERIFICATION`

Ordinary progress is not sent.

## CLI (diagnostics / automation)

The stable installed execution interface is:

```text
~/.governloop/bin/governloop
```

The skill uses it for `new`, `bind`, `checkpoint`, `status`, `end`, and `doctor`.
You can invoke it directly for diagnostics or automation, but the primary user
experience is skill-first: install once, reload the agent once, open the project,
use GovernLoop.
