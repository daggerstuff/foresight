#!/usr/bin/env bash
# Install the Foresight memory provider for Hermes Agent.
#
# Hermes discovers memory providers in $HERMES_HOME/plugins/<name>/ — a dir
# with __init__.py that calls register_memory_provider(). This script links
# (or copies) this directory there and prints the env vars to configure.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DEST="$HERMES_HOME/plugins/foresight"

usage() {
  cat <<EOF
Usage: bash plugins/hermes/install.sh [--hermes-home DIR] [--copy] [--dry-run]

  --hermes-home DIR   Hermes home (default: \$HERMES_HOME or ~/.hermes)
  --copy              copy files instead of symlinking (default: symlink)
  --dry-run           show what would be done
EOF
}

MODE=link
DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hermes-home) HERMES_HOME="$2"; shift 2 ;;
    --copy) MODE=copy; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done
DEST="$HERMES_HOME/plugins/foresight"

if [[ $DRY -eq 1 ]]; then
  echo "would install $SRC -> $DEST ($MODE)"
  exit 0
fi

mkdir -p "$HERMES_HOME/plugins"

if [[ -L "$DEST" || -e "$DEST" ]]; then
  rm -rf "$DEST"
fi

if [[ "$MODE" == "link" ]]; then
  ln -s "$SRC" "$DEST"
else
  cp -r "$SRC" "$DEST"
fi

echo "Installed Foresight memory provider: $DEST"
cat <<EOF

Next steps:
  1. Select it as the memory provider (hermes memory setup, or set the
     provider in $HERMES_HOME/config.yaml).
  2. If the Foresight server requires auth, export in the agent's env:
       FORESIGHT_API_KEY=<key>   # from 'foresight user create' on the server
       FORESIGHT_USER_ID=chad    # identity lane (optional; server default applies)
       FORESIGHT_URL=http://127.0.0.1:8764   # optional; localhost default
EOF
