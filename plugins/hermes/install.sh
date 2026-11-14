#!/usr/bin/env bash
# Install / deploy the Foresight memory provider for Hermes Agent.
#
# Hermes discovers memory providers in $HERMES_HOME/plugins/<name>/ — a dir
# with __init__.py that calls register_memory_provider(). This script links
# (or copies) this directory there, and can also deploy directly to a remote host.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DEST="$HERMES_HOME/plugins/foresight"
REMOTE=""
MODE="link"
DRY=0

usage() {
  cat <<EOF
Usage: bash plugins/hermes/install.sh [options]

Options:
  --hermes-home DIR   Hermes home (default: \$HERMES_HOME or ~/.hermes)
  --copy              Copy files instead of symlinking (default: symlink)
  --remote HOST       Deploy via SSH to remote host (e.g. hermes, daphne)
  --dry-run           Show what would be done without modifying anything
  -h, --help          Show this help message

Examples:
  bash plugins/hermes/install.sh                     # Local symlink install
  bash plugins/hermes/install.sh --copy              # Local copy install
  bash plugins/hermes/install.sh --remote hermes     # Deploy to fleet host 'hermes'
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hermes-home) HERMES_HOME="$2"; shift 2 ;;
    --copy) MODE="copy"; shift ;;
    --remote) REMOTE="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

DEST="$HERMES_HOME/plugins/foresight"

# Remote deployment via SSH
if [[ -n "$REMOTE" ]]; then
  echo "==> Deploying Foresight memory provider to remote host: $REMOTE"
  if [[ $DRY -eq 1 ]]; then
    echo "  [dry-run] Would sync $SRC -> $REMOTE:~/.hermes/plugins/foresight/"
    exit 0
  fi

  ssh "$REMOTE" "mkdir -p ~/.hermes/plugins/foresight"
  rsync -avz --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    "$SRC/" "$REMOTE:~/.hermes/plugins/foresight/"

  echo "  ✓ Files synced to $REMOTE:~/.hermes/plugins/foresight/"
  echo "  Verifying on $REMOTE..."
  ssh "$REMOTE" "if command -v hermes &>/dev/null; then hermes memory status; elif [ -x ~/.hermes/hermes-agent/.hermes/bin/hermes ]; then ~/.hermes/hermes-agent/.hermes/bin/hermes memory status; fi" || true
  echo "  ✓ Deployment to $REMOTE complete!"
  exit 0
fi

# Local installation
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
  1. Select it as the memory provider:
       hermes memory setup foresight
     or verify with:
       hermes memory status
  2. Test CLI commands:
       hermes foresight status
       hermes foresight test
  3. Ensure credentials in ~/.hermes/.env or environment:
       FORESIGHT_URL=https://foresight.pixelated.love
       FORESIGHT_API_KEY=<your-key>
       FORESIGHT_USER_ID=<identity-lane>
EOF
