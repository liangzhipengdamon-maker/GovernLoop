# GovernLoop Installation Architecture Addendum

Status: Proposed clarification for PR #104 review feedback
Scope: Documentation only

This addendum closes two architecture-contract gaps identified during PR #104 review.

## 1. Installed Universal Skill Dependency Contract

The installed universal skill MUST NOT depend on the original Git checkout after installation.

The installer MUST NOT copy the entire GovernLoop repository by default.

The installed runtime bundle MUST contain an explicit allowlist of normative contracts required by the installed universal skill.

Recommended installed contract bundle:

```text
~/.governloop/current/
├── skills/
│   └── governloop/
│       ├── SKILL.md
│       └── contracts/
│           ├── neutral-relay-checkpoint-delivery.md
│           ├── AGENT_SAFETY_CONTRACT.md
│           └── policy.md
└── runtime/
```

The allowlist is intentionally explicit. It is not a mirror of:

```text
README.md
docs/
skills/
tools/
AGENTS.md
```

Only documents that are normative dependencies of the installed skill are packaged.

Installation contract rules:

- Every normative reference made by the installed universal skill MUST resolve inside the installed bundle.
- Removing the original Git checkout MUST NOT create broken skill references.
- Agent integrations MUST reference the installed universal skill, not repository-relative documentation paths.
- Future self-contained skill formats may replace this contract, but repository copying is not the default model.

## 2. Immutable INSTALL_ID Contract

The directory name under:

```text
~/.governloop/versions/<INSTALL_ID>/
```

is an immutable installation identity, not a human display version only.

INSTALL_ID MUST uniquely identify the exact source/runtime provenance.

Contract:

```text
INSTALL_ID = <release-id>.<source-commit-short-sha>
```

Examples:

Tagged release:

```text
v0.2.0.25d7385a
```

Untagged checkout/main install:

```text
main.25d7385a
```

Local development checkout:

```text
local.<commit-short-sha>
```

Rules:

1. Two different source commits MUST never share the same INSTALL_ID.
2. INSTALL_ID directories are immutable after creation.
3. `current` points to exactly one INSTALL_ID.
4. Rollback means moving `current` back to a previously verified INSTALL_ID.
5. Installation metadata MUST record:

```text
INSTALL_ID
source ref/tag
source commit SHA
install timestamp
installer version
```

6. A new install from the same branch name but a different commit MUST create a new INSTALL_ID.

Therefore:

```text
main.aaaa1111
```

and:

```text
main.bbbb2222
```

are always different installation identities.

## 3. Scope Boundary

This addendum does not implement:

- installer behavior;
- runtime relocation;
- relay/session-manager movement;
- agent integration migration;
- dependency packaging.

It only freezes the architecture contracts required before implementation.
