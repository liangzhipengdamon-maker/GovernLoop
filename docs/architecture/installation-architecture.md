# GovernLoop Installation Architecture

Status: Proposed architecture for GitHub Issue #103  
Scope: Phase 0 installation audit + Phase 1 installation contract  
Implementation status: **documentation only; no installer or runtime changes in this document**

## 1. Purpose

GovernLoop already has a stable agent-agnostic protocol entrypoint in
`skills/governloop/SKILL.md`, plus a shared session model and Neutral Relay
transport. The remaining installation problem is not the protocol itself; it is
how a fresh user installs GovernLoop once and how any local coding agent finds
and invokes it from an arbitrary project without knowing a machine-specific
GovernLoop checkout path.

Target user experience:

```text
Install GovernLoop once.

cd <any-project>
# in the local coding agent:
Use GovernLoop for this task.
```

The agent should then be able to discover GovernLoop, create or resume a
session, request a ChatGPT conversation URL when required, deliver checkpoints
and evidence through the Neutral Relay, read the ChatGPT response back, and
complete `FINAL_VERIFICATION` without manual copy/paste.

This document separates **CURRENT FACT** from **PROPOSED DESIGN** so future
installation work does not accidentally present unimplemented behavior as the
current runtime.

---

## 2. Architecture constraints

The installation design MUST preserve these existing boundaries:

- GovernLoop remains transport/review infrastructure, not an authority engine.
- No change to relay, session, checkpoint, evidence, or fail-closed semantics is
  implied by installation work.
- A ChatGPT conversation URL remains task/session-level temporary state and MUST
  NOT become permanent canonical configuration.
- There is one universal protocol skill; agent-specific integrations stay thin.
- Existing `skills/workbuddy/governloop/` and `skills/opencode/governloop/`
  remain supported during migration.
- Current WorkBuddy and OpenCode verified paths must not be removed or broken
  before cold-start migration validation succeeds.
- Installation must not require per-project GovernLoop setup.

---

## 3. Phase 0 — Current installation audit

### 3.1 Current acquisition and installation model

**CURRENT FACT**

There is no formal universal installer on `main` today. The repository documents
an install-once user model, but generic-agent invocation still points to files
inside a GovernLoop Git checkout.

The current generic session-manager form is effectively:

```bash
python3 <governloop-checkout>/skills/workbuddy/governloop/scripts/governloop_session.py <command>
```

Therefore the current runtime still depends on either:

1. the user retaining a GovernLoop checkout and the agent discovering it, or
2. manual copying plus explicit path overrides.

There is no canonical `install.sh` yet.

**PROPOSED DESIGN**

A checkout should be needed only as an installation/development source. An
installed GovernLoop runtime should continue to work after the installation
checkout is removed.

### 3.2 Universal protocol skill

**CURRENT FACT**

Canonical universal protocol skill:

```text
skills/governloop/SKILL.md
```

It is intentionally agent-agnostic and defines the shared session model,
checkpoint protocol, relay interaction, authorization boundary, and fail-closed
rules. It does not define one agent's installation path as universal protocol.

### 3.3 Session manager

**CURRENT FACT**

Canonical implementation currently lives at:

```text
skills/workbuddy/governloop/scripts/governloop_session.py
```

Its behavior is agent-agnostic, but its physical location is WorkBuddy-specific
and transitional.

The session manager executes against the **target project working directory**;
repo/task discovery depends on the current working directory, not on running
inside the GovernLoop checkout.

The current implementation also contains an installation-specific default relay
path. That means a cold-start install on another machine cannot rely on the
current source default without overriding `GOVERLOOP_RELAY_PATH`.

**PROPOSED DESIGN**

The installed session manager must become part of a canonical GovernLoop runtime
bundle and be discoverable through a stable GovernLoop command. The first
installer PR does not need to move the source file in the repository.

### 3.4 Neutral Relay

**CURRENT FACT**

Canonical transport:

```text
tools/neutral-relay/neutral_relay.py
```

The relay communicates with an already-open ChatGPT conversation through Chrome
DevTools Protocol (CDP), uploads evidence attachments, sends the checkpoint
message, waits for the correlated assistant response, and writes that response
to a local output file.

The relay is Python-based and currently imports `websockets`; dependency
availability is therefore an installation/doctor concern.

### 3.5 Persistent config

**CURRENT FACT**

Canonical config path:

```text
~/.governloop/relay/config.json
```

This path is already established by the current relay/session-manager runtime
and should remain stable.

The canonical installed configuration must not acquire a permanent ChatGPT
conversation binding.

### 3.6 Temporary session state

**CURRENT FACT**

Default session state path:

```text
/tmp/governloop-session-<SESSION_ID>.json
```

Override:

```text
GOVERLOOP_STATE_DIR
```

Session state carries data such as repo, task, session id, conversation URL,
CDP port, checkpoint history, timestamps, and status.

A session's ChatGPT conversation URL belongs here, not in permanent canonical
configuration.

### 3.7 Conversation URL boundary

**CURRENT FACT — MUST BE PRESERVED**

The conversation URL is:

```text
task/session state
!= permanent project state
!= global installation state
```

The user selects the URL once per session. All checkpoints in that session reuse
it. A new unrelated session must not inherit it. GovernLoop must not auto-pick
the most recent browser tab or another project's conversation.

At session end, temporary routing state is removed; canonical config retains no
conversation binding.

### 3.8 Chrome/CDP assumptions

**CURRENT FACT**

The Neutral Relay does not currently own Chrome startup. It assumes a suitable
ChatGPT conversation is already open in a CDP-enabled Chrome instance.

Current session-manager CDP default is `9233`, with environment/config
resolution before the hard default.

The runtime connects to host loopback (`127.0.0.1:<port>`).

There is not yet a complete installation contract for:

- Chrome executable discovery;
- dedicated GovernLoop Chrome profile creation;
- a launcher;
- port collision diagnosis;
- host-vs-sandbox localhost accessibility;
- doctor checks for these conditions.

### 3.9 Current agent integrations

#### WorkBuddy

**CURRENT FACT**

Current user skill location:

```text
~/.workbuddy/skills/governloop/
```

Repository integration:

```text
skills/workbuddy/governloop/
├── SKILL.md
├── QUICK_START.md
├── references/
└── scripts/governloop_session.py
```

This verified integration is not yet thin because it contains protocol/docs,
policy references, and the shared session-manager runtime.

#### OpenCode

**CURRENT FACT**

Repository skill:

```text
skills/opencode/governloop/SKILL.md
```

Common install path:

```text
~/.config/opencode/skills/governloop/
```

The current skill still references runtime files in the GovernLoop checkout.
It therefore does not yet provide checkout-independent runtime discovery.

#### Claude Code

**CURRENT FACT**

No dedicated GovernLoop plugin is required today. The documented path is to
invoke the shared session-manager Python file, commonly with a short project
instruction such as `CLAUDE.md`.

That path still requires knowledge of the GovernLoop checkout.

#### Codex

**CURRENT FACT**

Same runtime model as Claude Code: invoke the shared session manager directly,
for example from project instructions. The current path is checkout-dependent.

#### Generic local agent

**CURRENT FACT**

Recommended entry is the session manager CLI; Neutral Relay may be invoked
directly for low-level transport. Both are currently file-path based rather than
a stable installed command.

### 3.10 Root `scripts/` boundary

**CURRENT FACT**

The repository root `scripts/` directory contains scripts that are not all part
of the universal Minimal Transport runtime. In particular, historical or
project-specific adapter logic must not be included merely because it is under
`scripts/`.

**PROPOSED DESIGN**

The installer must copy only an explicit allowlist of runtime artifacts, not an
entire repository directory by convention.

### 3.11 Current audit conclusion

The current architecture is logically close to universal but operationally
checkout-coupled:

```text
GovernLoop Git checkout
├── universal protocol skill            universal
├── WorkBuddy subtree
│   └── session manager                  shared behavior, transitional location
├── tools/neutral-relay
│   └── neutral_relay.py                 canonical transport
└── ~/.governloop/relay/config.json      canonical user config path
```

The installation gap is therefore primarily:

```text
runtime identity
runtime location
runtime discovery
agent discovery
Chrome bootstrap/diagnostics
upgrade ownership
```

---

## 4. Phase 1 — Installation product boundary

### 4.1 Rejected final model: permanent Git checkout

A long-lived Git checkout as the runtime authority is not the desired product
boundary because it creates:

- machine-specific absolute paths;
- checkout-location discovery requirements for agents;
- breakage when the checkout moves;
- upgrade semantics coupled to Git operations;
- development-tree and installed-runtime concerns in the same namespace;
- poor fresh-agent discoverability.

A Git checkout remains valuable as a transparent source for installation,
development, debugging, and upgrade staging. It should not be required after a
successful user installation.

### 4.2 Recommended model

```text
INSTALLATION_MODEL =
versioned local runtime bundle
+ universal skill bundle
+ stable CLI facade
+ thin agent integrations
```

This combines the useful properties of a local runtime installation and a skill
bundle without prematurely requiring PyPI, Homebrew, npm, or another package
registry.

The user-facing concept becomes:

```text
GovernLoop is installed
```

rather than:

```text
The GovernLoop repository is at /Users/.../GovernLoop/...
```

---

## 5. Canonical filesystem architecture

Recommended installed layout:

```text
~/.governloop/
├── versions/
│   ├── <version>/
│   │   ├── runtime/
│   │   │   ├── governloop_session.py
│   │   │   └── neutral_relay.py
│   │   ├── skills/
│   │   │   └── governloop/
│   │   │       └── SKILL.md
│   │   └── bin/
│   │       └── governloop
│   └── ...
├── current -> versions/<version>/
├── bin/
│   └── governloop -> ../current/bin/governloop
├── relay/
│   └── config.json
├── chrome-profile/
├── integrations/
├── cache/
└── install/
    └── metadata.json
```

Temporary state remains outside the versioned installation tree:

```text
/tmp/governloop-session-<SESSION_ID>.json
```

or the path selected through `GOVERLOOP_STATE_DIR`.

### 5.1 Immutable/versioned runtime

Installer-owned executable/runtime artifacts:

```text
~/.governloop/versions/<version>/runtime/
~/.governloop/versions/<version>/bin/
```

They should not contain user-mutable state.

### 5.2 Universal skill

Installed versioned skill:

```text
~/.governloop/versions/<version>/skills/governloop/SKILL.md
```

Stable logical path:

```text
~/.governloop/current/skills/governloop/SKILL.md
```

`skills/governloop/SKILL.md` remains the source authority in the repository.

### 5.3 Persistent configuration

Persistent user configuration remains:

```text
~/.governloop/relay/config.json
```

The installer may initialize missing safe defaults but MUST preserve an existing
user config.

The installer MUST NOT put a conversation URL in this file.

### 5.4 Mutable browser state

Dedicated browser profile:

```text
~/.governloop/chrome-profile/
```

It is mutable user/browser state, not versioned runtime. Upgrade must not replace
or delete it.

### 5.5 Temporary state

Session-level state remains temporary and isolated from persistent installation
metadata.

Conversation URLs may exist in:

- the active process;
- CLI arguments for the current operation;
- temporary session state.

They MUST NOT be written to:

```text
~/.governloop/relay/config.json
~/.governloop/install/metadata.json
~/.governloop/integrations/*
~/.governloop/cache/*
agent skill files
shell startup files
```

### 5.6 Cache

```text
~/.governloop/cache/
```

is explicitly disposable. No authority or session routing state may depend on
cache survival.

### 5.7 Agent integration metadata

```text
~/.governloop/integrations/
```

may record installation facts such as adapter version, destination path, and
link/copy mode. It must not carry task/session routing data.

### 5.8 Git checkout after install

A successful install MUST NOT require the original Git checkout to remain.
Deleting the installer checkout should not break normal GovernLoop use.

---

## 6. Runtime discovery contract

### 6.1 Stable entrypoint

The primary runtime discovery contract is a command named:

```text
governloop
```

Future normal invocation:

```bash
governloop new
governloop bind <conversation-url>
governloop status
governloop checkpoint <TYPE> ...
governloop end --final
governloop doctor
```

An agent receiving:

```text
Use GovernLoop for this task.
```

should not search the filesystem for a repository checkout. It should resolve
the stable installed entrypoint.

### 6.2 Discovery order

Recommended contract:

```text
1. command -v governloop
2. ~/.governloop/bin/governloop
3. otherwise fail closed: GovernLoop is not installed/discoverable
```

Repository discovery is not a normal runtime discovery mechanism.

### 6.3 Environment variables

Environment overrides remain useful for testing/development, but should not be
normal user installation requirements.

In particular, `GOVERLOOP_RELAY_PATH` may remain a diagnostic/development
override, but the installed CLI should resolve its bundled relay from its own
installation root.

A future `GOVERLOOP_HOME` could be an advanced override if required, but the
normal canonical location remains `~/.governloop`.

### 6.4 Symlinks

Symlinks are an installer implementation detail, not part of the agent-facing
contract. Agents depend on `governloop`, not on the internal `current` symlink
shape.

---

## 7. Universal skill installation contract

### 7.1 One protocol authority

There must be one complete universal protocol skill:

```text
skills/governloop/SKILL.md
```

Installed stable view:

```text
~/.governloop/current/skills/governloop/SKILL.md
```

Agent integrations should reference or expose that protocol rather than copy the
full protocol into separate long-lived agent forks.

### 7.2 WorkBuddy

Current verified integration remains supported:

```text
skills/workbuddy/governloop/
~/.workbuddy/skills/governloop/
```

Migration target: a thin WorkBuddy command/skill adapter that maps `/governloop`
to the installed `governloop` command and references the universal protocol.

Whether WorkBuddy can safely use a direct symlink to the universal skill must be
validated on a fresh installation before becoming a contract.

### 7.3 OpenCode

Current verified integration remains supported:

```text
skills/opencode/governloop/
~/.config/opencode/skills/governloop/
```

Migration target: a thin OpenCode skill that invokes `governloop` and does not
hard-code the WorkBuddy session-manager path or relay checkout path.

Whether symlink or copy is the most reliable OpenCode installation mode must be
validated before standardization.

### 7.4 Claude Code

No full duplicate protocol should be copied into every project. The minimal
integration should use the installed GovernLoop command plus the native/global
skill mechanism available and validated for Claude Code.

Project-specific files such as `CLAUDE.md` should contain only minimal invocation
or project governance instructions when required; they should not become a copy
of the GovernLoop universal protocol.

### 7.5 Codex

Same principle as Claude Code: prefer an installed/global skill mechanism plus
the `governloop` CLI. Project `AGENTS.md` remains responsible for project-level
instructions, not installation ownership of the GovernLoop protocol.

### 7.6 Adapter rule

Agent-specific integration owns only:

- agent discovery conventions;
- command binding/UX;
- sandbox/host accessibility guidance where specific to the agent.

It does not own a fork of session/checkpoint/relay semantics.

---

## 8. Chrome / CDP installation model

### 8.1 Dedicated profile

Retain a dedicated GovernLoop Chrome profile at:

```text
~/.governloop/chrome-profile/
```

This keeps GovernLoop CDP/browser state distinct from the user's normal Chrome
profile and gives installer/doctor a stable path to inspect.

Browser profiles are sensitive/mutable state and must never be attached as
checkpoint evidence.

### 8.2 CDP port

Keep `9233` as the v1 canonical default because the current session manager
already uses it. Installation work should not introduce a port change merely as
part of path portability.

Allow explicit override through the existing runtime/config mechanisms.

### 8.3 Browser startup ownership

Neutral Relay should remain transport-only and should not absorb browser
process management.

A future installation/runtime facade may provide a command such as:

```text
governloop browser start
```

or an internal launcher with equivalent responsibility.

Launcher responsibilities may include:

- locating Chrome;
- using the dedicated `user-data-dir`;
- selecting the configured remote debugging port;
- starting Chrome without changing the user's ordinary Chrome setup.

This launcher is installation/runtime orchestration, not a change to relay
protocol semantics.

### 8.4 Doctor checks

`governloop doctor` should diagnose at least:

```text
installed runtime integrity
Python availability
required Python dependency availability
CLI discovery
universal skill presence
relay presence
config readability
Chrome executable discovery
dedicated profile accessibility
CDP host/port reachability
supported agent integration presence
```

Doctor is read-only. It must not silently start Chrome, modify config, install
packages, change links, bind conversations, or remove sessions.

### 8.5 Host/sandbox boundary

Installation owns the host endpoint and host browser setup.

Agent-specific integration owns detection/reporting when an agent runtime is
sandboxed or containerized and cannot reach host `127.0.0.1:<cdp_port>`.

The fail-closed result should report that the host CDP endpoint is unreachable
from the agent runtime. Phase 1 does not define an automatic tunnel.

---

## 9. Config and state model

Three separate state classes are required.

### 9.1 Persistent config

```text
~/.governloop/relay/config.json
```

May contain stable non-session defaults such as a configured CDP port.

Must not contain a canonical conversation URL.

A minimal safe initial shape could be conceptually equivalent to:

```json
{
  "runtime": {
    "cdp_port": 9233
  }
}
```

Exact schema compatibility must be validated against the existing runtime before
an installer writes it.

### 9.2 Temporary session state

```text
/tmp/governloop-session-<SESSION_ID>.json
```

Contains task/session routing data, including the conversation URL selected for
that session.

This continues to follow the current session cleanup behavior.

### 9.3 Agent integration state

```text
~/.governloop/integrations/
```

Contains only installation/integration metadata. It never contains a
conversation URL or task-specific routing.

### 9.4 Relay path

The normal installed runtime should not require a persistent user-configured
relay path. The `governloop` facade resolves the bundled relay relative to the
active installed version.

### 9.5 Skill path

The installer owns the stable universal skill location and creates the necessary
agent-specific reference/copy/link according to validated adapter behavior.

---

## 10. Install / upgrade / uninstall / doctor contract

### 10.1 Install

Install must be idempotent and non-destructive.

Required behavior:

```text
preflight
-> stage versioned runtime
-> verify staged files
-> install version directory
-> atomically select current version
-> create missing persistent directories
-> initialize only missing safe config
-> install only validated thin agent integration(s)
-> run/read doctor diagnostics
-> print next action
```

Install MUST NOT:

- overwrite existing user config without explicit migration logic;
- remove browser profile data;
- delete temporary sessions/evidence;
- mutate target project repositories;
- create a permanent conversation binding.

### 10.2 Upgrade

Use a versioned install model so upgrade can fail closed:

```text
versions/0.1.x
versions/0.1.y
current -> selected version
```

Stage and verify the new version before changing `current`.

If validation fails, keep the previous active version.

At least the previous known-good version should remain available for rollback.

Upgrade must preserve persistent config, browser profile, and unrelated session
state.

### 10.3 Uninstall

Default uninstall removes installation-owned executable/runtime and agent
integration artifacts but preserves user-owned/mutable data unless the user
explicitly requests purge.

Conceptual modes:

```bash
governloop uninstall
governloop uninstall --purge
```

Default preserve candidates:

```text
~/.governloop/relay/config.json
~/.governloop/chrome-profile/
active temporary session/evidence state
```

A purge operation must be explicit and clearly report what will be destroyed.

### 10.4 Doctor

Doctor is diagnostics only.

It must not repair automatically. A future explicit `repair` command may be
considered separately.

---

## 11. Minimal installer UX

### 11.1 Recommended v1 channel

Use a transparent repository-based installer first:

```bash
git clone https://github.com/liangzhipengdamon-maker/GovernLoop.git
cd GovernLoop
./install.sh
```

This is preferred for the first implementation because it is:

- transparent;
- auditable before execution;
- easy to debug;
- low in packaging infrastructure;
- compatible with GovernLoop's current maturity.

After successful installation, removing the clone must not break GovernLoop.

### 11.2 Deferred channels

Do not make the first implementation depend on:

- `curl | sh`;
- Homebrew;
- `pipx`/PyPI;
- npm;
- another registry-specific package format.

Those may be evaluated after the local installer and cold-start contract are
validated.

---

## 12. Migration compatibility

### 12.1 WorkBuddy

Keep existing repository and installed WorkBuddy integration paths functional.
The new installed CLI is additive during migration.

### 12.2 OpenCode

Keep existing repository and installed OpenCode skill paths functional. Migrate
its runtime invocation only after the new CLI path is validated.

### 12.3 Claude Code / Codex

Existing direct-checkout session-manager invocation remains supported during the
migration window. New documentation may recommend `governloop` once the
installer exists, but old invocation must not disappear in the first phase.

### 12.4 Compatibility policy

For at least the initial migration release line:

```text
old paths = supported compatibility path
new installed CLI = recommended path
```

Only after the cold-start validation matrix passes should deprecation be
considered. Removal is a separate later decision.

---

## 13. Proposed implementation phases

### Phase 0 — Installation architecture audit

**Input**

- current `main`;
- Issue #103;
- universal skill introduced by PR #102;
- current WorkBuddy/OpenCode/session/relay/docs contracts.

**File scope**

Read-only audit.

**Runtime changes**

None.

**Primary risk**

Treating desired/documented behavior as current runtime truth.

**Acceptance criteria**

- current runtime files identified;
- persistent vs temporary state identified;
- machine-specific path dependencies identified;
- checkout coupling identified;
- agent integration points identified;
- Chrome/CDP assumptions identified.

### Phase 1 — Installation contract

**Input**

Phase 0 audit.

**File scope**

Architecture/documentation only.

**Runtime changes**

None.

**Primary risk**

Over-specifying unvalidated agent-native skill mechanics.

**Acceptance criteria**

Freeze the portable installation root, runtime entrypoint, universal skill
authority, config/session boundary, Chrome ownership boundary, lifecycle
semantics, and compatibility strategy.

### Phase 2 — Portable runtime layout

**Input**

Approved installation contract.

**Likely file scope**

Installer/runtime packaging support plus tests. The first implementation may
copy current source files into the installed runtime without moving their
repository source locations.

**Runtime behavior changes**

None intended; path/discovery only.

**Primary risk**

Import/path resolution regressions or accidental behavior changes while
repackaging.

**Acceptance criteria**

- installed runtime works independently of checkout path;
- bundled relay resolved internally;
- session/checkpoint behavior remains equivalent;
- deleting the installer checkout does not break runtime discovery.

### Phase 3 — Minimal installer

**Input**

Portable runtime layout.

**Likely file scope**

`install.sh`, wrapper/bootstrap files, install tests/docs.

**Runtime behavior changes**

No relay/session/checkpoint semantic changes.

**Primary risk**

Destructive upgrades/config overwrites or shell/PATH assumptions.

**Acceptance criteria**

- repeat install is idempotent;
- existing config/profile/state preserved;
- stable `governloop` command available or documented fallback works;
- installed universal skill present;
- no permanent conversation binding generated.

### Phase 4 — Agent integration

**Input**

Installed CLI + universal skill.

**File scope**

Thin WorkBuddy/OpenCode/Claude Code/Codex integration changes and docs.

**Runtime behavior changes**

None intended.

**Primary risk**

Breaking already-verified agent paths or assuming unsupported symlink/global
skill behavior.

**Acceptance criteria**

Each supported agent can discover the same universal protocol/runtime without a
checkout path, while existing WorkBuddy/OpenCode compatibility paths remain
usable.

### Phase 5 — Doctor

**Input**

Installer + supported integration matrix.

**File scope**

Read-only doctor command and tests.

**Runtime behavior changes**

Diagnostics only.

**Primary risk**

Doctor accidentally becoming an implicit repair/mutation command.

**Acceptance criteria**

Doctor reports installation, dependency, browser/CDP, config, and integration
health without changing system state.

### Phase 6 — Cold-start validation

**Input**

Fresh user/agent environment with no GovernLoop checkout knowledge.

**File scope**

Validation evidence/tests/docs; bug fixes only as separately reviewed work.

**Runtime behavior changes**

None as a goal; validation may reveal later fixes.

**Primary risk**

Hidden dependence on previous local configuration/history.

**Acceptance criteria**

Starting from only:

```text
Use GovernLoop for this task.
```

the agent can:

```text
discover GovernLoop
-> create session
-> request conversation URL when required
-> bind session
-> deliver checkpoint + evidence
-> read ChatGPT response back
-> continue work
-> complete FINAL_VERIFICATION
-> end/clean session state
```

with:

```text
manual copy/paste = 0
machine-specific GovernLoop checkout knowledge = 0
```

---

## 14. First implementation PR boundary

The first implementation PR after this architecture should be intentionally
narrow.

Recommended scope:

```text
portable installation foundation only
```

Likely contents:

- `install.sh`;
- canonical `~/.governloop` installed layout;
- stable `governloop` wrapper;
- copy/install current session manager into the versioned runtime;
- copy/install current Neutral Relay into the versioned runtime;
- install the universal skill;
- initialize only safe missing config;
- basic non-destructive installation verification;
- focused docs/tests.

Explicit non-scope:

- relay protocol changes;
- session/checkpoint behavior changes;
- source-file moves for session manager or relay;
- deleting or deprecating WorkBuddy/OpenCode paths;
- full agent integration migration;
- automatic browser orchestration complexity;
- Homebrew/PyPI/npm publishing.

---

## 15. Final architecture decision

```text
INSTALLATION_MODEL =
Versioned local runtime bundle + universal skill bundle + stable CLI facade + thin agent integrations

RUNTIME_LOCATION =
~/.governloop/versions/<version>/runtime/
(active version exposed through ~/.governloop/current)

SKILL_LOCATION =
~/.governloop/current/skills/governloop/SKILL.md

USER_COMMAND =
Use GovernLoop for this task.

AGENT_DISCOVERY =
agent skill/adapter -> command -v governloop -> ~/.governloop/bin/governloop -> fail closed

CHROME_PROFILE =
~/.governloop/chrome-profile/

CONFIG_LOCATION =
~/.governloop/relay/config.json

SESSION_STATE =
/tmp/governloop-session-<SESSION_ID>.json
(or GOVERLOOP_STATE_DIR override)

BACKWARD_COMPATIBILITY =
Keep existing WorkBuddy/OpenCode integrations and direct checkout invocation during migration; add the new installed CLI without removing old paths.

FIRST_IMPLEMENTATION_PR_SCOPE =
Portable installation foundation only: installer + canonical installed layout + stable CLI wrapper + universal skill installation + safe config initialization + non-destructive verification. No relay/session/checkpoint semantic changes and no existing agent-integration removal.
```

---

## 16. Decisions that can be made now

The following decisions do not require local cold-start validation:

1. A Git checkout should not be a long-term runtime dependency.
2. `~/.governloop/` is the canonical installation root.
3. `governloop` is the stable runtime entrypoint.
4. `skills/governloop/SKILL.md` remains the single universal protocol authority.
5. The agent-agnostic session manager must stop depending on a WorkBuddy-specific installed location.
6. Conversation URLs remain temporary session state only.
7. `~/.governloop/relay/config.json` must not become a permanent conversation-binding store.
8. The dedicated Chrome profile belongs under `~/.governloop/chrome-profile/`.
9. Install must be idempotent and non-destructive.
10. Upgrade should use staged/versioned fail-closed activation.
11. Doctor is read-only diagnostics.
12. Existing WorkBuddy/OpenCode paths remain during the first migration phases.
13. The first installer channel should be `git clone` + `./install.sh` rather than a package registry.

---

## 17. Decisions requiring real local validation

Do not freeze these until fresh-machine/agent testing provides evidence:

1. Whether WorkBuddy reliably supports a symlinked skill directory/file.
2. Whether OpenCode reliably supports symlinked universal skill installation.
3. The most reliable current global/native skill discovery mechanism for Claude Code.
4. The most reliable current global/native skill discovery mechanism for Codex.
5. Whether GUI agents inherit installer-added PATH entries in supported environments.
6. Whether `~/.local/bin` is reliably present in PATH on target macOS setups.
7. Cross-platform Chrome executable discovery details.
8. Dedicated profile + port `9233` real startup behavior across supported systems.
9. Port collision behavior with multiple Chrome/CDP instances.
10. Sandboxed/containerized agent access to host `127.0.0.1:9233`.
11. Whether Python dependencies should be satisfied by system Python, an installer-managed environment, or later package distribution.
12. Whether a truly fresh agent can discover and execute the loop from only `Use GovernLoop for this task.`

---

## 18. Architecture outcome

PR #102 established **protocol authority** by creating a universal protocol
skill. Issue #103 should now establish **installation authority and discovery
authority**.

The key migration is therefore not a Neutral Relay redesign. It is the change
from machine-specific execution such as:

```text
python3 /Users/<name>/.../GovernLoop/.../governloop_session.py
```

to the stable installed contract:

```text
governloop
```

After that change, an agent no longer needs to know where the GovernLoop source
repository lives. It only needs to know that GovernLoop is installed and how to
invoke its stable entrypoint.
