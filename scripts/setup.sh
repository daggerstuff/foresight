#!/usr/bin/env bash
# Development convenience wrapper — delegates to the root install.sh.
# For first-time setup, prefer:  bash install.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/../install.sh" "$@"
