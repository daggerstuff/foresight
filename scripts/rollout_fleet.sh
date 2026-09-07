#!/usr/bin/env bash
#
# Rollout PIX-4033 (Foresight MCP → Postgres-only) across the fleet.
#
# Default: DRY-RUN (prints what would happen, executes nothing).
# Pass --apply to actually restart the MCP service on each host.
#
# Hosts:
#   local  -> this machine (localhost)
#   billy  -> 40.160.6.46
#   gnasty -> 167.233.25.111
#
# Each host: restart foresight-mcp service.
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

# name:host:user
HOSTS=(
  "local:localhost:$(whoami)"
  "billy:40.160.6.46:billy"
  "gnasty:167.233.25.111:gnasty"
)

for entry in "${HOSTS[@]}"; do
  IFS=':' read -r name host user <<< "$entry"
  echo "==> [$name] ${user}@${host}"

  if [ "$APPLY" -eq 0 ]; then
    echo "    [dry-run] would: 'systemctl --user restart foresight-mcp'"
    continue
  fi

  if [ "$host" = "localhost" ]; then
    systemctl --user restart foresight-mcp
  else
    ssh -o StrictHostKeyChecking=accept-new "${user}@${host}" \
      "systemctl --user restart foresight-mcp"
  fi
  echo "    done"
done
