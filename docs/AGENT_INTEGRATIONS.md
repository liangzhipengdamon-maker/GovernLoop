# AGENT INTEGRATIONS

GovernLoop is **agent-agnostic**. The same session model drives every entry
point:

```text
repo → task → session → conversation → checkpoints → evidence → end
```

Rules that hold for **every** agent:

- **One session model, one session manager.** All entry points wrap the same
  `tools`/`skills/.../governloop_session.py` CLI. There is no per-agent
  fork.
- **No per-agent permanent routing config.** A conversation URL is
  **session-level state**: asked once per session, stored in temporary
  session state, never written to `~/.governloop/relay/config.json`, never
  inherited across sessions or repos.
- **Five checkpoints** are reported the same way from every agent:
  `NEW_BLOCKER`, `UNEXPECTED_STATE`, `BEFORE_DESTRUCTIVE_ACTION`,
  `REVIEW_REQUIRED`, `FINAL_VERIFICATION`. Ordinary progress is never sent.
- **Evidence delivery** follows one attachment policy (exists → relevant →
  secret scan → record; fail-closed on any refusal).

## Install-time agent skill activation (Phase 2E)

The installer can register the installed skill into an agent's skill directory
so the agent is usable right after installation — no manual skill setup:

```bash
./scripts/install.sh --agents=all                    # install + auto-detect agents
./scripts/install.sh --agents=codex,claude,workbuddy # install + explicit list
./scripts/install.sh --register-agents=codex         # register later (no reinstall)
./scripts/install.sh --unregister-agents=all         # remove registered skill links
```

| Agent | Registered skill link | Skill content |
|---|---|---|
| Codex | `~/.codex/skills/governloop` → installed universal skill | description-triggered: user says "Use GovernLoop for this task" |
| Claude Code | `~/.claude/skills/governloop` → installed universal skill | description-triggered: user says "Use GovernLoop for this task" |
| WorkBuddy | `~/.workbuddy/skills/governloop` → installed WorkBuddy flavor | `/governloop` slash command (first-class) |

Registration is **opt-in** and writes only a symlink into the agent's skill
directory (the link resolves into `~/.governloop/current/skills/...`, so it
follows version upgrades automatically). It never overwrites an existing
user-owned skill directory or a symlink it does not manage — conflicts fail
closed. The registration manifest is written to
`~/.governloop/metadata/agent-skills.json`. Agent skill roots can be redirected
for tests/CI via `GOVERLOOP_CODEX_SKILLS_DIR`, `GOVERLOOP_CLAUDE_SKILLS_DIR`,
`GOVERLOOP_WORKBUDDY_SKILLS_DIR`.

Manual installation (the sections below) remains fully supported and
equivalent — Phase 2E is a thin UX wrapper, not a different integration.

## WorkBuddy — `/governloop` slash command (fastest UX)

The first-class command entrypoint. Install the skill into
`~/.workbuddy/skills/governloop/`, then:

```text
cd <project>
/governloop          # create/resume session; asks ChatGPT URL once if missing
/governloop status
/governloop end
```

The agent runs `scripts/governloop_session.py` under the hood — you never see
the CLI. See `docs/QUICK_START.md`.

## OpenCode — GovernLoop skill

A minimal skill is maintained in:

```text
skills/opencode/governloop/SKILL.md
```

It documents the Neutral Relay workflow for OpenCode agents (request file →
relay → response read-back). For the higher-level session model (checkpoints,
session ids, URL-once-per-session), invoke the same session manager CLI from
the skill.

## Claude Code — invoke the local session manager CLI

No special plugin needed. The agent (or a CLAUDE.md instruction) calls the
session manager directly:

```bash
python3 <repo>/skills/workbuddy/governloop/scripts/governloop_session.py new
python3 <repo>/skills/workbuddy/governloop/scripts/governloop_session.py bind https://chatgpt.com/c/<id>
python3 <repo>/skills/workbuddy/governloop/scripts/governloop_session.py checkpoint REVIEW_REQUIRED --message "..." --attach <evidence...>
python3 <repo>/skills/workbuddy/governloop/scripts/governloop_session.py end
```

Recommended: add a short instruction to the project's `CLAUDE.md` telling the
agent to run `new` at task start, report the five checkpoints with
`--attach`, and run `end` on completion — exactly the behavior the WorkBuddy
skill encodes, but invoked explicitly.

## Codex — invoke the local session manager CLI

Same as Claude Code: call the CLI directly (e.g. from a `AGENTS.md` /
`codex.md` instruction or a one-liner):

```bash
python3 <repo>/skills/workbuddy/governloop/scripts/governloop_session.py new
python3 <repo>/skills/workbuddy/governloop/scripts/governloop_session.py status
python3 <repo>/skills/workbuddy/governloop/scripts/governloop_session.py checkpoint BEFORE_DESTRUCTIVE_ACTION --message "..." --attach <evidence>
python3 <repo>/skills/workbuddy/governloop/scripts/governloop_session.py end --final
```

## Any generic local coding agent

Two levels:

1. **Session manager (recommended)** — the full model above: session ids,
   URL-once, checkpoints, evidence, cleanup. Just invoke the same CLI.
2. **Neutral Relay (low-level)** — raw request → response transport with a
   route config: `tools/neutral-relay/neutral_relay.py --request-file ...`.
   This is the underlying API; you normally do not need it directly. See the
   "Neutral Relay" section in the README.

## Shared session model

Regardless of entry point, a session lifecycle looks like:

```text
new            # detect repo + task, generate <PROJECT>-<TASK>-<YYYY-MM-DD>
bind <url>     # conversation URL asked once, temp state only
checkpoint     # five types, evidence attachments, same bound conversation
status         # repo/task/session/URL/last checkpoint
end            # optional FINAL_VERIFICATION + temp state cleanup
```

Every agent reports to the **same** conversation it was bound to, follows the
**same** attachment policy, and cleans up in the **same** way — so switching
agents mid-project does not fork the session or the routing state.
