#!/bin/sh
set -eu

# Expose the installed universal GovernLoop skill through each selected agent's
# native user-level skill directory. This script never copies protocol logic and
# never overwrites an existing user-owned skill.

GOVERLOOP_HOME=${GOVERLOOP_HOME:-"$HOME/.governloop"}
UNIVERSAL_SKILL="$GOVERLOOP_HOME/current/skills/governloop"

fail() {
  printf '%s\n' "ERROR: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: scripts/install-agent-skills.sh [--agents <list>]

Agents: workbuddy, opencode, claude, codex, dsh
List may be comma-separated. Use "all" for every listed integration.

With no --agents argument and an interactive terminal, the script asks which
agents you use. In non-interactive mode, set GOVERLOOP_INSTALL_AGENTS or pass
--agents explicitly.
EOF
}

[ -d "$UNIVERSAL_SKILL" ] || fail "installed universal skill not found: $UNIVERSAL_SKILL"
[ -f "$UNIVERSAL_SKILL/SKILL.md" ] || fail "installed universal skill is incomplete: $UNIVERSAL_SKILL/SKILL.md"

install_skill_link() {
  agent=$1
  destination=$2
  parent=$(dirname "$destination")

  mkdir -p "$parent"

  if [ -L "$destination" ]; then
    current_target=$(readlink "$destination")
    if [ "$current_target" = "$UNIVERSAL_SKILL" ]; then
      printf '%s\n' "[$agent] already linked: $destination"
      return 0
    fi
    fail "$agent skill path already exists as a different symlink: $destination"
  fi

  if [ -e "$destination" ]; then
    fail "$agent skill path already exists; refusing to overwrite user-owned state: $destination"
  fi

  ln -s "$UNIVERSAL_SKILL" "$destination"
  printf '%s\n' "[$agent] installed: $destination -> $UNIVERSAL_SKILL"
}

install_agent() {
  case "$1" in
    workbuddy)
      install_skill_link "WorkBuddy" "${WORKBUDDY_HOME:-$HOME/.workbuddy}/skills/governloop"
      ;;
    opencode)
      install_skill_link "OpenCode" "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills/governloop"
      ;;
    claude)
      install_skill_link "Claude Code" "${CLAUDE_HOME:-$HOME/.claude}/skills/governloop"
      ;;
    codex)
      install_skill_link "Codex" "${CODEX_HOME:-$HOME/.codex}/skills/governloop"
      ;;
    dsh)
      # DSH provides its own native plugin mechanism. Native-first: do not copy a
      # generic skill into DSH or install an external package without a profile.
      printf '%s\n' "[DeepSeek Harness] use the native adapter:"
      printf '%s\n' "  dsh plugin --profile <name> add governloop-dsh@0.1.1"
      ;;
    *)
      fail "unknown agent: $1"
      ;;
  esac
}

normalize_agents() {
  printf '%s' "$1" | tr ',' ' ' | tr '[:upper:]' '[:lower:]'
}

agents_arg=""
case "${1:-}" in
  --agents)
    [ "$#" -eq 2 ] || fail "--agents requires exactly one comma-separated list"
    agents_arg=$2
    ;;
  --help|-h)
    usage
    exit 0
    ;;
  "")
    if [ -n "${GOVERLOOP_INSTALL_AGENTS:-}" ]; then
      agents_arg=$GOVERLOOP_INSTALL_AGENTS
    elif [ -t 0 ]; then
      cat <<'EOF'
Which agents do you use?
  1) WorkBuddy
  2) OpenCode
  3) Claude Code
  4) Codex
  5) DeepSeek Harness

Enter comma-separated names or numbers (for example: workbuddy,codex),
"all", or press Enter to skip agent integration:
EOF
      IFS= read -r agents_arg
    else
      printf '%s\n' "Agent skill integration skipped (non-interactive)."
      printf '%s\n' "Run scripts/install-agent-skills.sh --agents <list> later if needed."
      exit 0
    fi
    ;;
  *)
    fail "unknown argument: $1"
    ;;
esac

[ -n "$agents_arg" ] || {
  printf '%s\n' "Agent skill integration skipped."
  exit 0
}

case "$(printf '%s' "$agents_arg" | tr '[:upper:]' '[:lower:]')" in
  all)
    agents="workbuddy opencode claude codex dsh"
    ;;
  *)
    agents=$(normalize_agents "$agents_arg")
    ;;
esac

for raw in $agents; do
  case "$raw" in
    1) agent=workbuddy ;;
    2) agent=opencode ;;
    3) agent=claude ;;
    4) agent=codex ;;
    5) agent=dsh ;;
    *) agent=$raw ;;
  esac
  install_agent "$agent"
done

printf '%s\n' "Agent integration complete. Open your coding agent and use the GovernLoop skill."
