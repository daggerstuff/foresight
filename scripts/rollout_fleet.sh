#!/usr/bin/env bash
#
# Rollout PIX-4033 (Foresight MCP → Postgres-only) across the fleet.
#
# Default: DRY-RUN (prints what would happen, executes nothing).
# Pass --apply to actually restart the MCP service on each host.
#
# Hosts (ssh config aliases in ~/.ssh/config):
#   local   -> this machine (localhost)
#   daphne  -> 40.160.4.49  (user vivi)
#   jerry   -> 9.205.154.128 (user vivi)
#   hermes  -> 35.202.60.18  (user chad)
#
# Each host: restart foresight.service (and foresight-oauth.service when present).
# Requires FORESIGHT_DB_URL to be set on every host (Postgres-only storage).
#
set -euo pipefail

APPLY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    -h|--help) echo "Usage: $0 [--apply]   (default: dry-run)"; exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 1 ;;
  esac
done

# name:ssh-alias:user
HOSTS=(
  "local:localhost:$(whoami)"
  "daphne:daphne:vivi"
  "jerry:jerry:vivi"
  "hermes:hermes:chad"
)

for entry in "${HOSTS[@]}"; do
  IFS=':' read -r name host user <<< "$entry"
  echo "==> [$name] ${user}@${host}"

  if [ "$APPLY" -eq 0 ]; then
    echo "    [dry-run] would: 'systemctl --user restart foresight.service (+ foresight-oauth.service if active)'"
    continue
  fi

  RESTART_CMD='systemctl --user restart foresight.service; systemctl --user try-restart foresight-oauth.service 2>/dev/null || true'
  if [ "$host" = "localhost" ]; then
    bash -c "$RESTART_CMD"
  else
    ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "${user}@${host}" "$RESTART_CMD" \
      || echo "    ssh/restart failed for $name"
  fi
  echo "    done"
done
