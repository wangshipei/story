#!/bin/bash
# Mac daily runner; launchd calls --scheduled every five minutes.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "$SCRIPT_DIR/daily.py" "$@"
