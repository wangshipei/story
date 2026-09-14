#!/bin/bash
# Mac daily runner; launchd calls --scheduled every five minutes.
# STORY_REPO picks the checkout the task works in. Left unset, daily.py defaults it to the
# private worktree, so a manual run lands in the same repo as the scheduled one instead of
# writing a job into the shared state from the user's own checkout.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export STORY_REPO="${STORY_REPO:-$(/usr/bin/python3 "$SCRIPT_DIR/daily.py" --repo)}"
exec /usr/bin/python3 "$SCRIPT_DIR/daily.py" "$@"
