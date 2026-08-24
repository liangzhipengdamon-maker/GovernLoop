# GovernLoop Agent Instructions

All local agents working in this repository must follow the shared authorization boundary in:

`docs/ops/AGENT_SAFETY_CONTRACT.md`

Key rules:

- GovernLoop transport success does not authorize repository mutation.
- Scoped authorization: for one clearly scoped task, once the user explicitly
  authorizes execution, the agent may continue within that same scope through
  implementation → commit/push → PR → Ready → merge without repeatedly stopping
  for authorization at each individual stage.
- Still STOP (and require fresh explicit authorization) when any of these occur:
  material scope change, unexpected HEAD/main drift, merge conflict, P0/P1
  blocker, destructive/high-risk action (e.g. force push, direct `main` rewrite),
  or deploy/release/tag.
- Transport success, GPT/REVIEW PASS, test PASS, PR mergeability, or Ready state
  alone does NOT create authorization. Authorization originates only from an
  explicit user grant for the task in scope.
- Before executing Ready/merge/deploy/release within an authorized flow, verify
  current remote state and the exact target/HEAD (PR HEAD) where applicable; if
  state drifted, STOP.
- Do not broaden scope or start follow-up work without explicit authorization.
- Do not directly push/rewrite/force-push `main` without explicit authorization
  for that exact action.
- Diagnostic CDP read-back is not canonical relay success; canonical success
  requires relay exit success plus canonical output written.
- Do not reintroduce the historical AgentOps lifecycle/authority/runtime stack
  just to enforce these rules.
- GovernLoop only adds the governance capabilities strictly required to bridge a
  local Agent with ChatGPT Web safely and reliably. Governance outside this
  bridge boundary is out of scope.
- If the target Agent/runtime already provides a required bridge capability
  natively, GovernLoop must use or adapt that native capability instead of
  duplicating it.
