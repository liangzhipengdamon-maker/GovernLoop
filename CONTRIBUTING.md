# Contributing

GovernLoop is a **bridge-only** project: it adds only the governance
capabilities strictly required to bridge a local Agent with ChatGPT Web safely
and reliably. If a target runtime already provides a required capability
natively, GovernLoop uses or adapts it instead of duplicating it (see
`AGENTS.md`).

## Scope of changes

- **In scope:** the Neutral Relay transport, the session manager CLI, skills,
  docs, tests, installer.
- **Out of scope:** governance outside the bridge boundary (session/lineage/
  transcript frameworks, multi-agent coordination, approval mechanisms that
  belong to the runtime, etc.).
- Runtime work should first prove a concrete bridge requirement.

## Workflow

1. Work on a dedicated branch off `origin/main` (e.g. `fix/…`, `docs/…`).
2. Keep the change narrow; no force-push, no history rewrites.
3. Run the test suite before opening a PR:

   ```bash
   python3 -m unittest discover -s tools/neutral-relay/tests
   ```

4. Open a Draft PR; do not mark Ready or merge without explicit authorization.
5. Never merge to `main` directly; never push/rewrite `main`.

## Licensing

By contributing you agree that your contributions are licensed under the
[Apache-2.0](LICENSE) license of this repository.
