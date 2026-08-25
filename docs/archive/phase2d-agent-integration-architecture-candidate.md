# GovernLoop Phase 2D — Agent Integration / Onboarding Architecture Candidate v0

- **Status:** Architecture Research Only (no implementation authorized)
- **Owner:** GovernLoop Architecture Research Lead
- **Date:** 2026-08-23
- **Scope gate:** Implementation (Phase 2D coding, adapters, installer/CLI/skill
  changes, PATH integration, Chrome/CDP integration) begins ONLY after
  Architecture Review + Product Owner approval.

---

## 1. Executive Summary

GovernLoop today is a **proven, checkout-independent, agent-agnostic transport
stack** (Phase 2A Installer → 2B Runtime Bundle → 2C Stable CLI + Doctor →
Qclaw cold-user acceptance). The next open question is purely architectural:
**how should external coding agents (Qclaw, WorkBuddy, Claude Code, OpenCode,
Codex) discover and use GovernLoop correctly?**

This candidate recommends:

1. **One stable invocation contract, zero required adapters.** Agents call the
   installed CLI at the stable, documented path
   (`~/.governloop/bin/governloop`, overridable via `GOVERLOOP_HOME`), never a
   PATH-visible ambient binary and never a checkout-relative script.
2. **Discovery = agent-native mechanism + a universal capability contract.**
   Each agent already has a native skill/instruction surface (WorkBuddy skills,
   OpenCode skills, `CLAUDE.md`, `AGENTS.md`/`codex.md`). GovernLoop does not
   invent a parallel registry; it ships **one** installed universal skill
   (`~/.governloop/current/skills/governloop/SKILL.md`) that all agents
   reference, plus `doctor` as the deterministic readiness probe.
3. **The protocol is already universal; do not fork it per agent.** Session,
   checkpoint, reviewer-binding, and task-lifecycle contracts live in the
   session-manager CLI and the installed skill — not in agents. Thin
   presentation-layer sugar (a slash command or skill wrapper) is optional and
   must only format invocations, never reimplement protocol.
4. **Security boundary stays at the agent.** GovernLoop is transport; it must
   remain authority-free (no stored tokens, no ambient PATH authority, no
   direct main mutation capability, relay-only CDP). Repo mutation,
   merge/deploy, and credential handling stay the agent's / user's
   responsibility per `AGENTS.md` + `AGENT_SAFETY_CONTRACT.md`.
5. **Onboarding is agent-owned, GovernLoop-provided.** The agent drives the
   first-run flow using deterministic, idempotent GovernLoop primitives
   (install → `doctor` → `new` → `bind`); only conversation-URL binding and
   installation require explicit user confirmation.

The recommendation is **Option A (no adapters) as the protocol baseline + thin
optional adapters as UX sugar**, with **explicit rejection of deep
agent-specific plugins (Option C)**.

---

## 2. Current Phase 2C Architecture Baseline

Proven (all merged on `main`):

```text
Agent
  ↓
GovernLoop CLI (~/.governloop/bin/governloop → current/bin/governloop)
  ↓
Installed Runtime (~/.governloop/current/runtime/{governloop_session.py, neutral_relay.py})
  ↓
Session Manager (new / status / bind / checkpoint / end)
  ↓
Neutral Relay (CDP transport, fail-closed delivery confirmation)
  ↓
ChatGPT Reviewer (bound conversation)
```

Proven capabilities (Phase 2C acceptance):

- Install independently from a source checkout; runtime bundle is an explicit
  allowlist, not a repo mirror.
- Stable CLI discovery + version resolution through `current`; caller cwd
  preserved (repo/task detection belongs to the target project).
- `doctor` — read-only diagnostics (home, stable CLI, `current` pointer with
  installer-aligned validation, runtime files, skill, contracts, relay, cwd
  detection), never mutates session state.
- Session lifecycle: `new` (repo+task → `<PROJECT>-<TASK>-<DATE>`), `bind`
  (URL once per session, temp state only), `checkpoint` (five types + evidence
  with secret-scan), `end` (optional FINAL_VERIFICATION + cleanup).
- Checkpoint relay to ChatGPT with strong delivery confirmation and fail-closed
  attachment policy.

Existing (Phase 1A-era) integration surface — **NOT assumed correct**:

- WorkBuddy `/governloop` skill (`skills/workbuddy/governloop/`).
- OpenCode skill (`skills/opencode/governloop/SKILL.md`).
- Claude Code / Codex via checkout-relative CLI invocation
  (`docs/AGENT_INTEGRATIONS.md`).
- `.agent-bridge/` — historical AgentOps-era artifacts (Agent Runner prompt,
  Linear guide). Per `AGENTS.md`, the historical AgentOps lifecycle /
  authority / runtime stack must NOT be reintroduced; `.agent-bridge` is a
  legacy reference only.

The universal protocol skill (`skills/governloop/SKILL.md`) is
agent-agnostic by design and ships inside every installed bundle
(`skills/governloop/` + `contracts/`).

---

## 3. Agent Discovery Model Options

### Option D1 — PATH-based discovery (`governloop` on PATH)

| Dimension | Assessment |
|---|---|
| Mechanics | `governloop` resolves via shell PATH after install mutates profile |
| Pros | Familiar; shortest command |
| Cons | PATH mutation is user-owned state (explicitly out of Phase 2B/C scope); version/shadowing conflicts; non-deterministic which install wins; adds an ambient authority surface |
| Verdict | **Reject for v0.** PATH mutation remains a product decision, not an architecture requirement. |

### Option D2 — Fixed stable path (Phase 2C baseline)

| Dimension | Assessment |
|---|---|
| Mechanics | Agents probe/execute `~/.governloop/bin/governloop` (or `$GOVERLOOP_HOME/bin/governloop`); `doctor` verifies readiness |
| Pros | Already proven; deterministic; cwd-preserving; no shell mutation; single source of truth through `current` |
| Cons | Not on PATH (agents must know the path — solved by the universal skill + docs) |
| Verdict | **Adopt.** This is the canonical invocation contract. |

### Option D3 — Agent-specific adapter layer (per-agent skills/plugins)

| Dimension | Assessment |
|---|---|
| Mechanics | Each agent installs its own skill/plugin that wraps the CLI |
| Pros | Best UX per agent (slash commands); can add agent-native affordances |
| Cons | Duplication/drift risk; each adapter must track the protocol; maintenance grows linearly with agent count |
| Verdict | **Adopt only as thin presentation sugar** (see §4 Option B); never as protocol reimplementation. |

### Option D4 — Universal capability contract

| Dimension | Assessment |
|---|---|
| Mechanics | GovernLoop declares a machine-readable capability contract (the installed SKILL.md frontmatter + CLI surface + env contract) that any agent's discovery mechanism can reference |
| Pros | One artifact describes how to invoke; agent-agnostic; works with native skill marketplaces (WorkBuddy/OpenCode) and instruction files (CLAUDE.md / AGENTS.md) |
| Cons | Requires agents to have *some* skill/instruction mechanism (all five targets do) |
| Verdict | **Adopt.** The installed universal skill IS the capability contract. |

**Discovery decision matrix (Qclaw / WorkBuddy / Claude Code / OpenCode /
Codex):** every target already has a native mechanism to point at the
installed skill and stable path — none require PATH. PATH is therefore
optional product sugar, never a prerequisite.

---

## 4. Adapter Boundary Analysis

### Option A — No adapters (agents call `governloop` CLI directly)

```text
agent ──► governloop CLI ──► session manager ──► relay ──► reviewer
```

- **Maintenance cost:** lowest — one CLI, one protocol, zero per-agent code.
- **Security boundary:** cleanest — the agent boundary is the only boundary;
  GovernLoop exposes no agent-specific surface to attack.
- **Portability:** highest — identical behavior across all five agents.
- **Scalability:** linear with zero marginal cost per new agent; new agents
  only need a one-line "call this CLI" instruction.

### Option B — Thin adapters (agent → adapter → CLI)

```text
agent ──► adapter (UX sugar) ──► governloop CLI ──► ...
```

- **Maintenance cost:** low if the adapter is a pure invocation formatter
  (slash command / skill that maps `/governloop X` → `governloop X` and parses
  output). Must be version-pinned to the CLI contract, not the protocol.
- **Security boundary:** unchanged if the adapter adds no authority (never
  stores tokens, never bypasses secret-scan, never adds ambient config).
- **Portability:** preserved — adapters are presentation only.
- **Scalability:** acceptable for a small, stable set (WorkBuddy, OpenCode);
  guard against adapter proliferation via a review gate.

### Option C — Deep agent-specific plugins (agent plugins that reimplement/embed protocol)

- **Maintenance cost:** high — every plugin tracks session/checkpoint/relay
  semantics independently; drift is inevitable.
- **Security boundary:** worst — plugins run inside the agent's privileged
  context and could bypass fail-closed delivery, secret-scan, or cwd
  preservation.
- **Portability:** broken — per-agent forks.
- **Scalability:** poor — N agents × deep maintenance.
- **Verdict:** **Reject.** This is the historical adapter-fork trap.

**Recommendation:** **A as the baseline contract** (agents invoke the CLI; the
installed universal skill is the reference), **B allowed as optional
presentation-layer sugar** (current WorkBuddy `/governloop` and the OpenCode
skill already fit this shape and may be retained/refreshed), **C explicitly
out of bounds.**

---

## 5. First-run Experience Proposal

### Ownership

- **The agent owns the onboarding UX** (it lives in the agent's context and
  tooling).
- **GovernLoop provides deterministic, idempotent, non-destructive
  primitives** and zero magic: `install` (documented, user-invoked), `doctor`
  (readiness), `new`/`bind` (session start). GovernLoop never auto-installs,
  never auto-mutates profiles, never auto-binds.

### Proposed first-run flow

```text
1. Agent starts in a target project (repo/task detection is the project's job).
2. Probe: stable CLI present?   test -x "$GOVERLOOP_HOME/bin/governloop"
   ├─ NO  → suggest installation (show install command; USER CONFIRMS)
   └─ YES → go to 3
3. doctor (read-only readiness):
   ├─ FAIL → surface failures, stop (do not proceed with a broken install)
   └─ PASS/WARN → continue
4. new        (auto: detect repo+task, generate session id — automatic)
5. bind <url> (requires the user to paste a ChatGPT conversation URL —
               the ONLY per-session user-confirmation point)
6. work; report the five checkpoints; end --final when done.
```

### Automation vs. confirmation

| Step | Automatic? | User confirmation? |
|---|---|---|
| Repo/task/session-id detection | Yes | No |
| Session creation (`new`) | Yes | No |
| Installation into `~/.governloop` | No | **Yes** (writes to user home; must be explicit) |
| Conversation URL binding | No | **Yes** (once per session — existing contract) |
| Checkpoint reporting | Yes | No |
| Repo mutation / PR / merge / deploy | No | Per `AGENT_SAFETY_CONTRACT` — explicit scoped authorization |

### Error-state guidance

- No install → tell the user how to install; never install silently.
- Broken install (`doctor` FAIL) → do not create sessions against it; surface
  the failing check.
- No bound URL → print `USER_CONVERSATION_SELECTION_REQUIRED` (exit 3); ask
  once, never auto-inherit.

---

## 6. Universal Protocol Proposal

The protocol already exists and is agent-agnostic. Phase 2D should **ratify and
document it as the canonical contract**, not design a new one:

1. **Session contract** — `new` / `status` / `bind` / `checkpoint` / `end`;
   session id `<PROJECT>-<TASK>-<YYYY-MM-DD>`; URL is session-level state in
   temp storage only; same-repo ambiguity rules.
2. **Checkpoint contract** — the five types (`NEW_BLOCKER`,
   `UNEXPECTED_STATE`, `BEFORE_DESTRUCTIVE_ACTION`, `REVIEW_REQUIRED`,
   `FINAL_VERIFICATION`); ordinary progress never sent; evidence policy
   (exists → relevant → secret scan → record; fail-closed refusal).
3. **Reviewer binding contract** — one conversation per session; temp routing
   config only; canonical `~/.governloop/relay/config.json` never carries a
   conversation binding; delivery confirmation is strong (composer cleared +
   user-turn +1, or guarded auxiliary signal) and never auto-resends.
4. **Task lifecycle contract** — `repo → task → session → conversation →
   checkpoints → evidence → end`; `end` optionally sends
   `FINAL_VERIFICATION` then removes temp state.

**Can different agents share one protocol without custom implementations?**
Yes. The protocol is implemented in the CLI + installed skill, which any agent
invokes identically:

```text
WorkBuddy:   checkpoint → governloop CLI
Claude Code: checkpoint → governloop CLI
OpenCode:    checkpoint → governloop CLI
Qclaw:       checkpoint → governloop CLI
```

A thin adapter (Option B) may reformat invocation/UX, but the protocol
semantics (session id, URL-once, checkpoint types, evidence, cleanup) are
never re-implemented. **One protocol, N presentation layers.**

---

## 7. Security Boundary Model

### Principle

**GovernLoop is transport. Transport success is not authority.** (Per
`AGENTS.md` §transport, `AGENT_SAFETY_CONTRACT.md`.)

### What GovernLoop enforces (its boundary)

- **Transport integrity:** fail-closed relay delivery (composer-clear +
  user-turn confirmation; `SEND_NOT_CONFIRMED` / `SEND_PENDING_TIMEOUT` never
  auto-resend); attachment secret-scan refuses secret-bearing evidence
  (`.redacted` copies only).
- **State ownership:** conversation URL is temp session state only; canonical
  routing config never mutated by sessions; `end` removes temp state.
- **Read-only diagnostics:** `doctor` performs zero filesystem mutation
  (verified by regression tests incl. `GOVERLOOP_STATE_DIR` non-creation).
- **Installer immutability & ownership:** immutable INSTALL_IDs, duplicate
  protection, dirty-tree fail-closed, strict `current` ownership validation,
  atomic activation, fail-closed rollback (upgrade + first-install).
- **Minimal ambient surface:** no PATH mutation, no auto-install, no stored
  credentials, no ad-hoc CDP from agents — the relay is the only CDP consumer,
  driven by explicit session state.

### What remains the agent's / user's responsibility

- **Repo mutation authorization** (commit/push/PR/merge/deploy) — GovernLoop
  has no such capability and never grants it; the agent enforces the scoped
  authorization model and stops on material drift/P0/P1/conflict.
- **Credential handling** — agents must not attach secrets; the secret-scan is
  a backstop, not a license.
- **Browser/CDP hygiene** — the user keeps Chrome open on the bound
  conversation; agents must not drive CDP ad hoc.
- **Main protection** — no force push / direct `main` rewrite without explicit
  authorization for that exact action.

### Threat analysis (summary)

| Threat | GovernLoop posture | Agent responsibility |
|---|---|---|
| Malicious agent | Cannot gain authority from GovernLoop (no tokens, no ambient config, no main access) | Agent sandbox/tooling boundary |
| Accidental direct `main` mutation | GovernLoop has no main-mutation surface | Pre-PR state verification, stop-on-drift |
| Unauthorized merge/deploy | Transport PASS never authorizes it | Scoped authorization + explicit stage grants |
| Credential exposure | Secret-scan fail-closed on attachments; no credentials stored | Never attach `.env`/tokens; `.redacted` policy |
| Browser automation risk | Relay-only CDP, strong delivery confirmation | Keep only the bound conversation open |

---

## 8. Migration Strategy

Goal: move from checkout-era integration surface to the installed-runtime
contract without breaking existing users.

| Step | Action | Compatibility |
|---|---|---|
| 1 | Ratify the universal protocol + stable-path contract (this candidate) | None breaking |
| 2 | Refresh WorkBuddy `/governloop` skill to invoke the **installed** CLI via sibling/stable-path resolution (already sibling-first for the relay) | Checkout path still works during transition |
| 3 | Refresh OpenCode skill to the installed path + `doctor` first-run step | Backward-compatible |
| 4 | Update `AGENT_INTEGRATIONS.md` + QUICK_START to the stable-path contract and onboarding flow | Docs only |
| 5 | Add optional per-agent onboarding snippets (Claude `CLAUDE.md`, Codex `AGENTS.md`/`codex.md`) referencing the installed skill + `doctor` | Docs/instructions only |
| 6 | Qclaw cold-start re-test with the new onboarding flow | Acceptance gate |

Phase ordering: protocol ratification → skill refresh → docs → onboarding
snippets → Qclaw re-test. No production runtime change is required for the
migration itself (the CLI/runtime are already stable-path capable).

---

## 9. Risks

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| R1 | Agents embed checkout-relative paths (status quo in docs) instead of the stable path | High (legacy docs) | Migration step 4 + doc lint; installed skill is the only referenced artifact |
| R2 | Adapter drift: thin adapters grow into protocol reimplementations | Medium | Review gate on adapters; contract: adapters format invocations only |
| R3 | PATH mutation pressure re-enters scope via product requests | Medium | Keep PATH as explicit product decision, out of architecture baseline |
| R4 | Relay premature-settle / reviewer-side truncated responses (observed in this session: 3× truncated reviewer replies) | Observed | Keep fail-closed no-resend; surface `SEND_*` states; manual recovery guidance; consider longer conservative settle for reviewer threads |
| R5 | Session-id derivation inconsistency across environments (`GOVERLOOP-` vs `GOVERNLOOP-` variants observed) | Observed | Canonicalize project slug derivation; explicit `--session` for ambiguity |
| R6 | CDP dependency (Chrome must be open with the bound conversation) | Medium | `doctor` reports CDP/relay readiness; onboarding step includes "open the conversation" |
| R7 | Multi-agent concurrent sessions on one repo | Medium | Existing session-id + ambiguity rules (ask before inheriting); keep |
| R8 | Capability-registry ambitions (agent-side registry of GovernLoop) | Low | Defer; native skill discovery suffices for the five targets |

---

## 10. Recommendation

1. **Adopt Option A as the protocol baseline:** agents invoke the installed
   stable CLI (`~/.governloop/bin/governloop`); the installed universal skill
   is the single capability contract every agent references.
2. **Allow thin adapters (Option B) as UX sugar only** (WorkBuddy slash
   command, OpenCode skill) — formatting invocations, never reimplementing
   protocol. **Reject deep plugins (Option C).**
3. **Discovery = agent-native mechanisms + `doctor`:** no PATH requirement;
   `doctor` is the deterministic readiness probe; the installed skill is the
   discoverable contract.
4. **Onboarding = agent-owned flow over deterministic primitives:** install
   (user-confirmed) → `doctor` → `new` → `bind` (user-confirmed URL once) →
   checkpoints → `end`.
5. **Ratify the existing protocol as the universal contract** (session /
   checkpoint / reviewer-binding / task-lifecycle) with no per-agent forks.
6. **Security boundary: GovernLoop stays transport-only and authority-free;**
   authorization, credentials, and repo lifecycle remain the agent's /
   user's responsibility.
7. **Migrate the integration surface** (skills + docs + onboarding snippets)
   to the stable-path contract; re-run Qclaw cold-start as the acceptance gate.

**Explicit non-goals for v0:** no production code; no adapter creation; no
installer/CLI/skill modification; no PATH integration; no Chrome/CDP
integration; no capability registry. Implementation begins only after
Architecture Review + Product Owner approval.
