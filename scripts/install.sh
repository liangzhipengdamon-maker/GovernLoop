#!/bin/sh
set -eu

# GovernLoop installer (Phase 2A skeleton + Phase 2B minimal installed runtime
# bundle). Phase 2A scope: installation identity, immutable version directories,
# metadata, and atomic current activation. Phase 2B stages an explicit-allowlist
# runtime bundle (runtime/ + skills/governloop/ + bin/governloop) so a successful
# install is checkout-independent. It intentionally installs no Chrome profile,
# shell integration, or agent adapters.

INSTALLER_VERSION="phase2b-runtime-bundle-v1"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
GOVERLOOP_HOME=${GOVERLOOP_HOME:-"$HOME/.governloop"}

fail() {
  printf '%s\n' "ERROR: $*" >&2
  exit 1
}

require_git_source() {
  git -C "$SOURCE_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || fail "installer must run from a GovernLoop Git checkout"
}

source_commit() {
  git -C "$SOURCE_ROOT" rev-parse HEAD
}

source_commit_short() {
  git -C "$SOURCE_ROOT" rev-parse --short=8 HEAD
}

require_clean_tracked_tree() {
  # INSTALL_ID is commit-based. Installing tracked modifications would make the
  # installed provenance differ from the commit encoded in INSTALL_ID.
  if ! git -C "$SOURCE_ROOT" diff --quiet --ignore-submodules -- || \
     ! git -C "$SOURCE_ROOT" diff --cached --quiet --ignore-submodules --; then
    fail "tracked working tree is dirty; commit or discard tracked changes before installation"
  fi
}

sanitize_release_id() {
  # Git ref names are broader than safe path components. Preserve common tag
  # spelling while mapping every other character to '-'.
  printf '%s' "$1" | sed 's/[^A-Za-z0-9._-]/-/g'
}

detect_identity() {
  require_git_source
  require_clean_tracked_tree

  SOURCE_COMMIT=$(source_commit)
  SOURCE_COMMIT_SHORT=$(source_commit_short)

  # If multiple exact tags point at HEAD, choose lexicographically first so the
  # identity is deterministic on every machine with the same refs.
  EXACT_TAG=$(git -C "$SOURCE_ROOT" tag --points-at HEAD 2>/dev/null | LC_ALL=C sort | sed -n '1p')

  if [ -n "$EXACT_TAG" ]; then
    RELEASE_ID=$(sanitize_release_id "$EXACT_TAG")
    [ -n "$RELEASE_ID" ] || fail "exact tag cannot be converted to a safe release id"
    SOURCE_TYPE="tagged_release"
    SOURCE_REF="$EXACT_TAG"
  else
    BRANCH=$(git -C "$SOURCE_ROOT" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    if [ "$BRANCH" = "main" ]; then
      RELEASE_ID="main"
      SOURCE_TYPE="untagged_checkout"
      SOURCE_REF="main"
    else
      RELEASE_ID="local"
      SOURCE_TYPE="local_development"
      if [ -n "$BRANCH" ]; then
        SOURCE_REF="$BRANCH"
      else
        SOURCE_REF="detached"
      fi
    fi
  fi

  INSTALL_ID="${RELEASE_ID}.${SOURCE_COMMIT_SHORT}"
}

write_metadata() {
  destination=$1
  timestamp=$2
  python3 - "$destination" "$INSTALL_ID" "$SOURCE_COMMIT" "$timestamp" \
    "$SOURCE_TYPE" "$SOURCE_REF" "$INSTALLER_VERSION" <<'PY'
import json
import sys

(
    destination,
    install_id,
    source_commit,
    installed_at,
    source_type,
    source_ref,
    installer_version,
) = sys.argv[1:]

payload = {
    "install_id": install_id,
    "source_commit": source_commit,
    "install_timestamp": installed_at,
    "source_type": source_type,
    "source_ref": source_ref,
    "installer_version": installer_version,
}
with open(destination, "x", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
}

# ---------------------------------------------------------------------------
# Phase 2B: minimal installed runtime bundle (explicit allowlist).
# ---------------------------------------------------------------------------
# Canonical runtime sources packaged into every installed version. The bundle is
# generated at install time from the exact committed sources, so provenance
# (INSTALL_ID = <release>.<commit-short>) matches the installed payload.
RUNTIME_BUNDLE_SOURCES="
skills/workbuddy/governloop/scripts/governloop_session.py
tools/neutral-relay/neutral_relay.py
skills/governloop/SKILL.md
docs/architecture/neutral-relay-checkpoint-delivery.md
docs/ops/AGENT_SAFETY_CONTRACT.md
skills/workbuddy/governloop/references/policy.md
"

# Version-scoped entrypoint inside each installed version: resolves THIS
# version's runtime, never cds (the caller's project is the working directory).
version_wrapper_script=$(cat <<'VERSION_WRAPPER_EOF'
#!/bin/sh
# GovernLoop version entrypoint (checkout-independent). Resolves the runtime of
# THIS installed version. The caller's working directory is preserved: repo/task
# discovery belongs to the target project, not the GovernLoop installation.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VERSION_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
runtime_dir="$VERSION_ROOT/runtime"
if [ ! -f "$runtime_dir/governloop_session.py" ]; then
  echo "[FAIL] installed runtime missing: $runtime_dir/governloop_session.py" >&2
  exit 1
fi
if [ ! -f "$runtime_dir/neutral_relay.py" ]; then
  echo "[FAIL] installed relay missing: $runtime_dir/neutral_relay.py" >&2
  exit 1
fi
: "${GOVERLOOP_RELAY_PATH:=$runtime_dir/neutral_relay.py}"
export GOVERLOOP_RELAY_PATH
exec python3 "$runtime_dir/governloop_session.py" "$@"
VERSION_WRAPPER_EOF
)

# Stable user-facing entrypoint at $GOVERLOOP_HOME/bin/governloop: dispatches to
# the ACTIVE installed version (current/bin/governloop). Static across versions.
dispatcher_script=$(cat <<'DISPATCHER_EOF'
#!/bin/sh
# GovernLoop stable entrypoint -> active installed version.
# This shim never changes the working directory: repo/task discovery belongs to
# the caller's project, not the GovernLoop installation.
set -eu
GOVERLOOP_HOME=${GOVERLOOP_HOME:-"$HOME/.governloop"}
current_wrapper="$GOVERLOOP_HOME/current/bin/governloop"
if [ ! -f "$current_wrapper" ]; then
  echo "ERROR: GovernLoop has no active installed version ($current_wrapper)" >&2
  exit 1
fi
exec "$current_wrapper" "$@"
DISPATCHER_EOF
)

require_bundle_sources() {
  for rel in $RUNTIME_BUNDLE_SOURCES; do
    [ -f "$SOURCE_ROOT/$rel" ] \
      || fail "Phase 2B runtime source missing in checkout: $rel"
  done
}

stage_runtime_bundle() {
  require_bundle_sources

  mkdir -p "$stage_dir/runtime" \
           "$stage_dir/skills/governloop/contracts" \
           "$stage_dir/bin"

  cp "$SOURCE_ROOT/skills/workbuddy/governloop/scripts/governloop_session.py" \
     "$stage_dir/runtime/governloop_session.py"
  cp "$SOURCE_ROOT/tools/neutral-relay/neutral_relay.py" \
     "$stage_dir/runtime/neutral_relay.py"

  # Render the installed universal skill + normative contracts from canonical
  # sources with an explicit path-rewrite allowlist. Fail-closed verification:
  # any leftover checkout-relative reference aborts the install.
  python3 - "$stage_dir" "$SOURCE_ROOT" <<'PY'
import os
import re
import sys

stage, source_root = sys.argv[1], sys.argv[2]


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# --- installed universal skill ----------------------------------------------
skill = read(os.path.join(source_root, "skills/governloop/SKILL.md"))
for src, dst in (
    ("docs/architecture/neutral-relay-checkpoint-delivery.md",
     "contracts/neutral-relay-checkpoint-delivery.md"),
    ("skills/workbuddy/governloop/references/policy.md", "contracts/policy.md"),
    ("docs/ops/AGENT_SAFETY_CONTRACT.md", "contracts/AGENT_SAFETY_CONTRACT.md"),
    ("skills/workbuddy/governloop/scripts/governloop_session.py",
     "runtime/governloop_session.py"),
    ("tools/neutral-relay/neutral_relay.py", "runtime/neutral_relay.py"),
):
    skill = skill.replace(src, dst)
# Non-normative / stable installed-form rewrites (after the generic allowlist).
skill = skill.replace(
    "Canonical positioning and design principles: `README.md`.",
    "Canonical positioning and design principles are documented in the source\n"
    "repository README (not packaged in the installed runtime bundle).",
)
skill = skill.replace(
    "lives in the GovernLoop checkout, but",
    "ships in the installed runtime bundle (or the GovernLoop checkout), but",
)
skill = skill.replace(
    "(physical location is transitional; see `docs/AGENT_INTEGRATIONS.md`).",
    "(installed runtime bundles ship the session manager under `runtime/`).",
)
skill = skill.replace(
    "installation-specific absolute path in the current runtime; override when it does not match the local GovernLoop checkout",
    "`~/.governloop/current/runtime/neutral_relay.py` in an installed runtime; "
    "override when the auto-detected default is incorrect",
)
skill = skill.replace(
    "Discover the local GovernLoop checkout and set\n"
    "`GOVERLOOP_RELAY_PATH` explicitly when the default does not match.",
    "Set `GOVERLOOP_RELAY_PATH` explicitly when the auto-detected default does\n"
    "not match.",
)
skill = skill.replace(
    "python3 runtime/neutral_relay.py \\",
    "python3 ~/.governloop/current/runtime/neutral_relay.py \\",
)
skill = skill.replace(
    "Full contract: `contracts/neutral-relay-checkpoint-delivery.md` \u00a76,\n"
    "`tools/neutral-relay/README.md`.",
    "Full contract: `contracts/neutral-relay-checkpoint-delivery.md` \u00a76.",
)
skill = skill.replace(
    "and the repository-level `AGENTS.md`.",
    "and the target project's repository-level `AGENTS.md` when present.",
)
skill += (
    "\n## 9. Installed runtime bundle (Phase 2B)\n\n"
    "When GovernLoop is installed (not run from a checkout):\n\n"
    "- Stable entrypoint: `~/.governloop/bin/governloop` (or `$GOVERLOOP_HOME/bin/governloop`)\n"
    "- Active version pointer: `~/.governloop/current/`\n"
    "- Session manager: `~/.governloop/current/runtime/governloop_session.py`\n"
    "- Neutral Relay: `~/.governloop/current/runtime/neutral_relay.py`\n"
    "- Installed skill bundle: `~/.governloop/current/skills/governloop/` "
    "(this skill + `contracts/`)\n\n"
    "The installed skill's normative references resolve only inside this bundle; "
    "the original Git checkout is not required after installation.\n"
)
leftover = sorted(set(re.findall(r"(?:docs/|tools/|skills/workbuddy|README\.md)", skill)))
if leftover:
    fail("installed universal skill still references checkout paths: " + ", ".join(leftover))
write(os.path.join(stage, "skills/governloop/SKILL.md"), skill)

# --- installed normative contracts ------------------------------------------
relay_contract = read(os.path.join(
    source_root, "docs/architecture/neutral-relay-checkpoint-delivery.md"))
rc = relay_contract.replace("tools/neutral-relay/neutral_relay.py",
                            "runtime/neutral_relay.py")
rc = rc.replace(
    "tools/neutral-relay/tests/",
    "the source repository test suite (not part of the installed runtime bundle)")
rc = rc.replace("python3 runtime/neutral_relay.py",
                "python3 ~/.governloop/current/runtime/neutral_relay.py")
if re.search(r"tools/", rc):
    fail("installed relay contract still references checkout paths")
write(os.path.join(stage, "skills/governloop/contracts/neutral-relay-checkpoint-delivery.md"), rc)

safety = read(os.path.join(source_root, "docs/ops/AGENT_SAFETY_CONTRACT.md"))
write(os.path.join(stage, "skills/governloop/contracts/AGENT_SAFETY_CONTRACT.md"), safety)

policy = read(os.path.join(
    source_root, "skills/workbuddy/governloop/references/policy.md"))
po = policy.replace("scripts/governloop_session.py", "runtime/governloop_session.py")
po = po.replace("neutral_relay.py", "runtime/neutral_relay.py")
po = po.replace("docs/architecture/neutral-relay-checkpoint-delivery.md",
                "contracts/neutral-relay-checkpoint-delivery.md")
leftover = sorted(set(re.findall(r"(?:docs/|scripts/|tools/)", po)))
if leftover:
    fail("installed policy contract still references checkout paths: " + ", ".join(leftover))
write(os.path.join(stage, "skills/governloop/contracts/policy.md"), po)
PY

  printf '%s\n' "$version_wrapper_script" > "$stage_dir/bin/governloop"
  chmod 755 "$stage_dir/bin/governloop"
}

publish_stable_entrypoint() {
  printf '%s\n' "$dispatcher_script" > "$dispatcher_stage"
  chmod 755 "$dispatcher_stage"

  # Injection point (deterministic dispatcher-publication failure test): the
  # stable entrypoint is published BEFORE current is switched, so this failure
  # leaves the previous current + previous dispatcher intact and rolls back
  # this run's published-but-uncommitted artifacts.
  if [ "${GOVERLOOP_FAIL_PUBLISH_DISPATCHER:-0}" = "1" ]; then
    fail "injected interrupt: stable entrypoint publication"
  fi

  if ! python3 - "$dispatcher_stage" "$dispatcher_path" <<'PY'
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
  then
    rm -f "$dispatcher_stage"
    fail "could not publish stable entrypoint $dispatcher_path (install remains committed)"
  fi
  rm -f "$dispatcher_stage"
}

install_skeleton() {
  detect_identity

  versions_dir="$GOVERLOOP_HOME/versions"
  bin_dir="$GOVERLOOP_HOME/bin"
  metadata_dir="$GOVERLOOP_HOME/metadata"
  version_dir="$versions_dir/$INSTALL_ID"
  metadata_index="$metadata_dir/$INSTALL_ID.json"
  current_path="$GOVERLOOP_HOME/current"
  dispatcher_path="$bin_dir/governloop"
  dispatcher_stage="$GOVERLOOP_HOME/.bin.governloop.stage.$$"
  # Whether the stable entrypoint existed before this run. A first install that
  # creates it and then fails BEFORE current activation must remove it during
  # rollback; an upgrade that reuses an installer-managed (byte-identical)
  # dispatcher must leave it intact.
  dispatcher_pre_existed=0

  # --- current pointer preflight (fail-closed) -------------------------------
  # Only two states are safe to replace atomically:
  #   * current is absent, or
  #   * current is an installer-managed symlink with the exact canonical shape
  #     "versions/<INSTALL_ID>" (single component, no traversal) pointing at an
  #     existing installed version.
  # Any other object (regular file, directory, symlink with a malformed or
  # dangling target) is rejected so we never silently overwrite or trust
  # pre-existing state with mv -f.
  if [ -e "$current_path" ] || [ -L "$current_path" ]; then
    if [ ! -L "$current_path" ]; then
      # Regular file or directory: never overwrite with mv -f.
      if [ -d "$current_path" ]; then
        fail "current exists as a directory; refusing non-atomic replacement"
      fi
      fail "current exists as a regular file; refusing non-atomic replacement"
    fi
    # current is a symlink: require the exact installer-managed shape.
    current_target=$(readlink "$current_path")
    case "$current_target" in
      versions/*)
        # Reject traversal, nested paths, or an empty component. The architecture
        # contract is current -> exactly one installed immutable INSTALL_ID.
        rest=${current_target#versions/}
        case "$rest" in
          ""|.|..|*/*)
            fail "current symlink target is malformed or uses traversal: $current_target" ;;
        esac
        ;;
      *)
        fail "current symlink target is not installer-managed: $current_target" ;;
    esac
    # Reject dangling symlinks: the targeted version directory must exist.
    if [ ! -e "$current_path" ]; then
      fail "current symlink points to a missing version directory: $current_target"
    fi
  fi

  # --- stable entrypoint preflight (fail-closed) ----------------------------
  # bin/governloop is an installer-managed static shim (dispatches to the active
  # version's wrapper). Never silently overwrite user-owned state: accept only
  # absence or byte-identical installer content.
  if [ -e "$dispatcher_path" ] || [ -L "$dispatcher_path" ]; then
    dispatcher_pre_existed=1
    if [ -L "$dispatcher_path" ] || [ -d "$dispatcher_path" ]; then
      fail "bin/governloop exists as a non-regular file; refusing to overwrite"
    fi
    if ! printf '%s\n' "$dispatcher_script" | cmp -s - "$dispatcher_path"; then
      fail "bin/governloop exists with unexpected content; refusing to overwrite user-owned state"
    fi
  fi

  mkdir -p "$versions_dir" "$bin_dir" "$metadata_dir"

  [ ! -e "$version_dir" ] && [ ! -L "$version_dir" ] \
    || fail "INSTALL_ID already exists and is immutable: $INSTALL_ID"

  stage_dir="$versions_dir/.${INSTALL_ID}.stage.$$"
  metadata_stage="$GOVERLOOP_HOME/.${INSTALL_ID}.json.stage.$$"
  current_stage="$GOVERLOOP_HOME/.current.stage.$$"

  # Track how far publication progressed so cleanup only rolls back artifacts
  # THIS run published (never a pre-existing, completed install).
  version_published=0
  metadata_published=0
  activated=0
  # Decide committed activation from actual filesystem state. This is robust
  # against a signal/termination arriving between the atomic rename of current
  # and the in-memory activated flag being set: even if activated is still 0, a
  # current that already resolves exactly to this run's version (with that
  # directory present) is committed and must NOT be rolled back.
  activation_committed() {
    [ -L "$current_path" ] || return 1
    cur=$(readlink "$current_path")
    [ "$cur" = "versions/$INSTALL_ID" ] || return 1
    [ -d "$version_dir" ] || return 1
    return 0
  }
  cleanup() {
    rm -rf "$stage_dir"
    rm -f "$metadata_stage" "$current_stage" "$dispatcher_stage"
    # Committed (normal success, or an interrupt after the rename): keep the
    # published artifacts. Only roll back THIS run's published artifacts when
    # activation is NOT committed, so a failed install leaves no partial and a
    # pre-existing completed install is never touched.
    if [ "$activated" -eq 1 ] || activation_committed; then
      return
    fi
    [ "$version_published" -eq 1 ] && rm -rf "$version_dir"
    [ "$metadata_published" -eq 1 ] && rm -f "$metadata_index"
    # A failed pre-activation install must not leave a stable entrypoint that
    # THIS run created (there is no current to resolve through it). A
    # dispatcher that already existed before this run (accepted as
    # installer-managed byte-identical content) is always left intact.
    if [ "$dispatcher_pre_existed" -eq 0 ]; then
      rm -f "$dispatcher_path"
    fi
  }
  trap cleanup EXIT HUP INT TERM

  mkdir "$stage_dir"
  installed_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

  # Keep provenance with the immutable version as well as in the installation
  # metadata index. The runtime bundle is staged below and ships inside the
  # version directory.
  write_metadata "$stage_dir/metadata.json" "$installed_at"
  write_metadata "$metadata_stage" "$installed_at"

  # Stage the Phase 2B runtime bundle inside the immutable version directory so
  # the whole bundle publishes atomically with the single mv below.
  stage_runtime_bundle

  # Publish the immutable version first. It is still recoverable: if any later
  # step fails before activation, cleanup removes it, so a failed install leaves
  # no partial versions/<INSTALL_ID> and the INSTALL_ID stays retryable.
  mv "$stage_dir" "$version_dir"
  version_published=1

  mv "$metadata_stage" "$metadata_index"
  metadata_published=1

  # Publish the stable entrypoint BEFORE switching current: current is switched
  # only after every install-owned artifact (version, metadata, stable CLI) is
  # ready, so a failure before the atomic switch leaves the previous current and
  # the previous stable entrypoint intact and rolls back this run's
  # published-but-uncommitted artifacts (same fail-closed transaction as Phase
  # 2A). The dispatcher content is static, so an already-present dispatcher is
  # either left untouched (os.replace never ran) or replaced byte-identically.
  publish_stable_entrypoint

  # Build the replacement pointer symlink in a temp location first.
  ln -s "versions/$INSTALL_ID" "$current_stage"

  # Injection point (deterministic pre-replace interrupt test): simulate a
  # termination before the atomic replacement is committed. The previously
  # active current must remain intact; this run's published artifacts roll back.
  if [ "${GOVERLOOP_FAIL_BEFORE_ACTIVATE:-0}" = "1" ]; then
    fail "injected interrupt before activation replace"
  fi

  # Atomically replace the current pointer. os.replace() uses rename(2)
  # semantics: it replaces the current symlink entry in place (never follows it
  # into a version directory) and is atomic, so there is no remove-then-rename
  # window where current is momentarily absent.
  python3 - "$current_stage" "$current_path" <<'PY'
import os
import sys

src, dst = sys.argv[1], sys.argv[2]
os.replace(src, dst)
PY

  # Injection point (deterministic post-replace interrupt test): simulate a
  # termination between the atomic replace and the activated flag being set.
  if [ "${GOVERLOOP_FAIL_AFTER_ACTIVATE:-0}" = "1" ]; then
    fail "injected interrupt after activation replace"
  fi

  activated=1

  trap - EXIT HUP INT TERM

  printf '%s\n' "GovernLoop installer activated: $INSTALL_ID"
  printf '%s\n' "Home: $GOVERLOOP_HOME"
  printf '%s\n' "Bundle: runtime/ + skills/governloop/ + bin/governloop (Phase 2B runtime bundle)"
  printf '%s\n' "NOTE: installs no Chrome profile, shell config, or agent adapters."
}

case "${1:-}" in
  --print-install-id)
    [ "$#" -eq 1 ] || fail "--print-install-id accepts no additional arguments"
    detect_identity
    printf '%s\n' "$INSTALL_ID"
    ;;
  "")
    install_skeleton
    ;;
  *)
    fail "unknown argument: $1"
    ;;
esac
