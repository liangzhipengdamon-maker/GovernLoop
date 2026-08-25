#!/bin/sh
set -eu

# User-facing GovernLoop installer.
# 1) install the checkout-independent Core runtime transactionally
# 2) expose the same universal installed skill to selected agent runtimes

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

"$SCRIPT_DIR/scripts/install.sh"
sh "$SCRIPT_DIR/scripts/install-agent-skills.sh"
