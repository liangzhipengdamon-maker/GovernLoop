# Security Policy

## Reporting a Vulnerability

If you discover a security issue in GovernLoop (the Neutral Relay, the session
manager, or the CDP → ChatGPT Web bridge), **do not open a public issue**.
Report it privately to the maintainers (GitHub private vulnerability reporting,
or direct maintainer contact).

Please include:

- Affected version / commit (`git rev-parse HEAD`).
- A minimal repro (environment, steps, expected vs actual).
- Impact assessment if known.

You will receive an acknowledgement, and we will coordinate a fix before
public disclosure where possible.

## Scope

The transport (Neutral Relay over CDP) and the session manager. Adapter-side
behavior for specific agent runtimes (e.g. DeepSeek Harness) lives in the
matching adapter repository (e.g. GovernLoop-DSH).

## Out of scope / guidance

- ChatGPT conversation URLs are session-level and must never be persisted in
  the canonical config.
- Evidence attachments are scanned (exists → relevance → secret scan → redaction
  → sha256) before upload; secret-bearing evidence is only ever attached as a
  redacted copy.
- Transport success never authorizes repository mutation, merge, deploy, or
  release (see `docs/ops/AGENT_SAFETY_CONTRACT.md`).
