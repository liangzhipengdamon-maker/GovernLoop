#!/bin/sh
set -eu

# GovernLoop Phase 2A installer skeleton.
# Scope: installation identity, immutable version directories, metadata, and
# atomic current activation only. It intentionally installs no runtime, skills,
# Chrome profile, shell integration, or agent adapters.

INSTALLER_VERSION="phase2a-skeleton-v1"
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

install_skeleton() {
  detect_identity

  versions_dir="$GOVERLOOP_HOME/versions"
  bin_dir="$GOVERLOOP_HOME/bin"
  metadata_dir="$GOVERLOOP_HOME/metadata"
  version_dir="$versions_dir/$INSTALL_ID"
  metadata_index="$metadata_dir/$INSTALL_ID.json"
  current_path="$GOVERLOOP_HOME/current"

  # --- current pointer preflight (fail-closed) -------------------------------
  # Only two states are safe to replace atomically:
  #   * current is absent, or
  #   * current is an installer-managed symlink (target under versions/).
  # Any other object (regular file, directory, or an unexpected symlink) is
  # rejected so we never silently overwrite pre-existing state with mv -f.
  if [ -e "$current_path" ] || [ -L "$current_path" ]; then
    if [ -L "$current_path" ]; then
      current_target=$(readlink "$current_path")
      case "$current_target" in
        versions/*) : ;;  # installer-managed symlink: safe to replace atomically
        *) fail "current is a symlink to an unexpected target; refusing: $current_target" ;;
      esac
    elif [ -d "$current_path" ]; then
      fail "current exists as a directory; refusing non-atomic replacement"
    else
      fail "current exists as a regular file; refusing non-atomic replacement"
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
  cleanup() {
    rm -rf "$stage_dir"
    rm -f "$metadata_stage" "$current_stage"
    if [ "$activated" -ne 1 ]; then
      [ "$version_published" -eq 1 ] && rm -rf "$version_dir"
      [ "$metadata_published" -eq 1 ] && rm -f "$metadata_index"
    fi
  }
  trap cleanup EXIT HUP INT TERM

  mkdir "$stage_dir"
  installed_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

  # Keep provenance with the immutable version as well as in the installation
  # metadata index. No runtime payload is copied in Phase 2A.
  write_metadata "$stage_dir/metadata.json" "$installed_at"
  write_metadata "$metadata_stage" "$installed_at"

  # Publish the immutable version first. It is still recoverable: if any later
  # step fails, cleanup removes it, so a failed install leaves no partial
  # versions/<INSTALL_ID> and the INSTALL_ID stays retryable.
  mv "$stage_dir" "$version_dir"
  version_published=1

  mv "$metadata_stage" "$metadata_index"
  metadata_published=1

  # Build the replacement symlink separately, then rename it into place. Rename
  # within one filesystem is atomic; current therefore never points at a partial
  # version directory.
  ln -s "versions/$INSTALL_ID" "$current_stage"
  mv -f "$current_stage" "$current_path"
  activated=1

  trap - EXIT HUP INT TERM

  printf '%s\n' "GovernLoop installer skeleton activated: $INSTALL_ID"
  printf '%s\n' "Home: $GOVERLOOP_HOME"
  printf '%s\n' "NOTE: Phase 2A installs no runtime, skills, Chrome, shell config, or agent adapters."
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
