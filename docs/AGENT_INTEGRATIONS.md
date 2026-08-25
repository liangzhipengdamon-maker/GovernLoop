# AGENT INTEGRATIONS

GovernLoop is **agent-agnostic**. The same session model drives every entry
point:

```text
repo → task → session → conversation → checkpoints → evidence → end
```

Rules that hold for **every** agent:

- **One universal protocol skill.** Agent integrations expose the installed
  `~/.governloop/current/skills/governloop/` skill rather than forking protocol
  logic per agent.
- **One stable execution interface.** The skill drives the installed
  `~/.governloop/bin/governloop` CLI; the CLI drives the shared session manager
  and Neutral Relay.
- **No per-agent permanent routing config.** A conversation URL is session-level
  temporary state and is never inherited across unrelated sessions or repos.
- **Five checkpoints** are reported consistently:
  `NEW_BLOCKER`, `UNEXPECTED_STATE`, `BEFORE_DESTRUCTIVE_ACTION`,
  `REVIEW_REQUIRED`, `FINAL_VERIFICATION`.
- **Native first.** When an agent/runtime already has the required native plugin
  or skill mechanism, GovernLoop adapts that mechanism instead of duplicating
  runtime behavior.

## Universal installation

From a GovernLoop checkout:

```bash
sh install.sh
```

The user-facing installer first installs GovernLoop Core, then asks which agents
the user wants to integrate. For skill-based agents it creates a native user
skill link to the same installed universal skill.

The installer is fail-closed around existing user state: it never overwrites an
existing skill directory or a different symlink.

## WorkBuddy

Native skill path:

```text
~/.workbuddy/skills/governloop
```

After installation, use the GovernLoop skill normally; WorkBuddy may expose it
as `/governloop`.

## OpenCode

Native skill path:

```text
~/.config/opencode/skills/governloop
```

The installed path points to the same universal GovernLoop skill used by the
other skill-based agents.

## Claude Code

Native skill path:

```text
~/.claude/skills/governloop
```

Open the target project in Claude Code and ask it to use GovernLoop. The skill
uses the stable installed CLI underneath; no project-local copy of the GovernLoop
protocol is required.

## Codex

Native skill path:

```text
~/.codex/skills/governloop
```

Open the target project in Codex and ask it to use GovernLoop. The same universal
skill and stable CLI are used.

## DeepSeek Harness

DeepSeek Harness already provides a native plugin mechanism. GovernLoop therefore
does **not** install a generic skill into DSH. Use the GovernLoop-DSH adapter:

```bash
dsh plugin --profile <name> add governloop-dsh@0.1.1
```

This keeps DSH runtime/session/plugin responsibilities native while GovernLoop
provides only the bridge to GPT Web.

## Other local coding agents

If another agent supports a user-level skill directory compatible with the
GovernLoop `SKILL.md` contract, expose
`~/.governloop/current/skills/governloop/` through that native mechanism.
Otherwise the agent can invoke the stable installed CLI directly:

```text
~/.governloop/bin/governloop
```

Direct CLI use is the fallback execution interface, not the preferred normal
user experience.

## Shared session model

Regardless of the agent, the lifecycle remains:

```text
new            # detect repo + task, generate session identity
bind <url>     # conversation URL once, temp state only
checkpoint     # five types, evidence attachments, same bound conversation
status         # diagnostics
end            # optional FINAL_VERIFICATION + temp state cleanup
```

Switching agents does not create a different GovernLoop protocol or a separate
routing authority. The bridge remains one installed runtime, one universal skill,
and one task/session-level conversation binding.
