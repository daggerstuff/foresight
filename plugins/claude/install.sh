#!/usr/bin/env bash
# Foresight plugin installer — wires Foresight into Claude Code and Claude Desktop.
#
# Usage:
#   bash plugins/claude/install.sh                # install for both clients
#   bash plugins/claude/install.sh --claude-code   # Claude Code only
#   bash plugins/claude/install.sh --desktop       # Claude Desktop only
#   bash plugins/claude/install.sh --stdio         # Desktop: stdio transport instead of HTTP
#   bash plugins/claude/install.sh --url URL       # override MCP endpoint (default http://127.0.0.1:8764)
#   bash plugins/claude/install.sh --api-key KEY   # Bearer token for remote/authenticated servers
#   bash plugins/claude/install.sh --identity ID   # memory lane, e.g. "alice" or "alice@acme" (default: $USER)
#   bash plugins/claude/install.sh --dry-run       # show what would change
#
# Env equivalents: FORESIGHT_MCP_URL, FORESIGHT_API_KEY, FORESIGHT_IDENTITY.
# Credentials are stored in ~/.config/foresight/credentials.env (chmod 600)
# for the hooks, and in ~/.claude/settings.json env for MCP header expansion.
#
# Requirements: python3, curl (optional health check). Foresight server should be
# running (install.sh / foresight.service) unless you use --stdio for Desktop.

set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORESIGHT_URL="${FORESIGHT_MCP_URL:-http://127.0.0.1:8764}"
MCP_URL="${FORESIGHT_URL%/}/mcp"
HEALTH_URL="${FORESIGHT_URL%/}/health"

DO_CLAUDE_CODE=0
DO_DESKTOP=0
DESKTOP_STDIO=0
DRY_RUN=0
API_KEY="${FORESIGHT_API_KEY:-}"
IDENTITY="${FORESIGHT_IDENTITY:-${FORESIGHT_USER_ID:-${USER:-}}}"

while [ $# -gt 0 ]; do
  case "$1" in
    --claude-code) DO_CLAUDE_CODE=1 ;;
    --desktop)     DO_DESKTOP=1 ;;
    --stdio)       DO_DESKTOP=1; DESKTOP_STDIO=1 ;;
    --dry-run)     DRY_RUN=1 ;;
    --api-key)     shift; API_KEY="${1:?--api-key requires a value}" ;;
    --api-key=*)   API_KEY="${1#--api-key=}" ;;
    --identity)    shift; IDENTITY="${1:?--identity requires a value}" ;;
    --identity=*)  IDENTITY="${1#--identity=}" ;;
    --url)
      shift
      [ $# -gt 0 ] || { echo "--url requires a value" >&2; exit 2; }
      MCP_URL="$1"; FORESIGHT_URL="${MCP_URL%/mcp}"; HEALTH_URL="${FORESIGHT_URL%/}/health" ;;
    --url=*)       MCP_URL="${1#--url=}"; FORESIGHT_URL="${MCP_URL%/mcp}"; HEALTH_URL="${FORESIGHT_URL%/}/health" ;;
    -h|--help)
      sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# Default: both clients
if [ "$DO_CLAUDE_CODE" -eq 0 ] && [ "$DO_DESKTOP" -eq 0 ]; then
  DO_CLAUDE_CODE=1
  DO_DESKTOP=1
fi

C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
[ -t 1 ] || { C_GREEN=""; C_YELLOW=""; C_RED=""; C_BOLD=""; C_RESET=""; }

_ok()   { printf "  ${C_GREEN}✓${C_RESET}  %s\n" "$1"; }
_warn() { printf "  ${C_YELLOW}!${C_RESET}  %s\n" "$1"; }
_info() { printf "  ·  %s\n" "$1"; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf "  [dry-run] %s\n" "$*"
  else
    "$@"
  fi
}

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

# ── Health check ─────────────────────────────────────────────────────────────

if [ "$DESKTOP_STDIO" -eq 0 ] || [ "$DO_CLAUDE_CODE" -eq 1 ]; then
  if command -v curl >/dev/null 2>&1 && curl -sf -m 3 "$HEALTH_URL" >/dev/null 2>&1; then
    _ok "Foresight server reachable at ${MCP_URL}"
  else
    _warn "Foresight server not reachable at ${HEALTH_URL}"
    _info "Start it first:  bash install.sh   (or: foresight-server --port 8764)"
    _info "Config will still be written; tools activate once the server is up."
  fi
fi

# ── Credentials ──────────────────────────────────────────────────────────────

REMOTE_HOST=0
case "$FORESIGHT_URL" in
  http://127.0.0.1*|http://localhost*|https://127.0.0.1*|https://localhost*) ;;
  *) REMOTE_HOST=1 ;;
esac

if [ "$REMOTE_HOST" -eq 1 ] && [ -z "$API_KEY" ]; then
  _warn "Remote endpoint detected but no API key — the server will likely reject requests."
  _info "Re-run with:  --api-key <key>   (provision via: foresight user create)"
fi

# ~/.config/foresight/credentials.env — read by foresight_hook.py (chmod 600).
CREDS_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/foresight"
CREDS_FILE="$CREDS_DIR/credentials.env"
if [ -n "$API_KEY" ] || [ -n "$IDENTITY" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    printf "  [dry-run] write %s (FORESIGHT_API_KEY, FORESIGHT_IDENTITY)\n" "$CREDS_FILE"
  else
    mkdir -p "$CREDS_DIR"
    touch "$CREDS_FILE"
    chmod 600 "$CREDS_FILE"
    CREDS_FILE="$CREDS_FILE" API_KEY="$API_KEY" IDENTITY="$IDENTITY" python3 "$PLUGIN_DIR/install_support.py" credentials
    _ok "Wrote credentials to $CREDS_FILE"
  fi
fi

# ── Claude Code ──────────────────────────────────────────────────────────────

if [ "$DO_CLAUDE_CODE" -eq 1 ]; then
  echo "${C_BOLD}Claude Code${C_RESET}"

  CLAUDE_DIR="$HOME/.claude"
  if [ ! -d "$CLAUDE_DIR" ] && ! command -v claude >/dev/null 2>&1; then
    _info "Claude Code not detected — skipping (install Claude Code or pass --desktop only)"
  else
    HOOK_DEST="$CLAUDE_DIR/hooks/foresight_hook.py"
    HOOK_SRC="$PLUGIN_DIR/hooks/foresight_hook.py"
    LEGACY_HOOK_DEST="$CLAUDE_DIR/hooks/foresight-hook.sh"
    SKILL_SRC="$PLUGIN_DIR/skills/foresight/SKILL.md"

    # Resolve skill destination — users often symlink ~/.claude/skills/<name>
    # into a shared skills dir (e.g. ~/.agents/skills).
    SKILL_DEST_DIR="$CLAUDE_DIR/skills/foresight"
    SKILL_SKIP=""
    if [ -L "$SKILL_DEST_DIR" ]; then
      # Resolve the symlink target (works even when dangling) relative to the link.
      raw="$(readlink "$SKILL_DEST_DIR" 2>/dev/null || true)"
      case "$raw" in
        /*) resolved="$raw" ;;
        *)  resolved="$(cd "$(dirname "$SKILL_DEST_DIR")" 2>/dev/null && cd "$(dirname "$raw")" 2>/dev/null && pwd)/$(basename "$raw")" || resolved="" ;;
      esac
      if [ -n "$resolved" ] && [ -d "$(dirname "$resolved")" ]; then
        SKILL_DEST_DIR="$resolved"
      else
        SKILL_SKIP="cannot resolve symlink target ($SKILL_DEST_DIR -> $raw)"
      fi
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
      printf "  [dry-run] cp %s %s\n" "$HOOK_SRC" "$HOOK_DEST"
      if [ -e "$LEGACY_HOOK_DEST" ]; then
        printf "  [dry-run] rm %s\n" "$LEGACY_HOOK_DEST"
      fi
      if [ -n "$SKILL_SKIP" ]; then
        printf "  [dry-run] skip skill: %s\n" "$SKILL_SKIP"
      else
        printf "  [dry-run] cp %s %s\n" "$SKILL_SRC" "$SKILL_DEST_DIR/SKILL.md"
      fi
    else
      mkdir -p "$CLAUDE_DIR/hooks"
      [ -z "$SKILL_SKIP" ] && mkdir -p "$SKILL_DEST_DIR"
      cp "$HOOK_SRC" "$HOOK_DEST"
      chmod +x "$HOOK_DEST"
      rm -f "$LEGACY_HOOK_DEST"
      if [ -n "$SKILL_SKIP" ]; then
        _warn "Skill install skipped: $SKILL_SKIP"
      else
        cp "$SKILL_SRC" "$SKILL_DEST_DIR/SKILL.md"
      fi
    fi
    _ok "Installed hook + foresight skill"

    # Merge MCP server into ~/.claude.json
    CLAUDE_JSON="$HOME/.claude.json"
    if [ "$DRY_RUN" -eq 1 ]; then
      printf "  [dry-run] merge mcpServers.foresight into %s\n" "$CLAUDE_JSON"
    else
      CLAUDE_JSON="$CLAUDE_JSON" MCP_URL="$MCP_URL" python3 "$PLUGIN_DIR/install_support.py" claude-json
      _ok "Registered foresight MCP server in ~/.claude.json"
    fi

    # Merge hooks into ~/.claude/settings.json (idempotent)
    SETTINGS="$CLAUDE_DIR/settings.json"
    HOOK_CMD="python3 '$CLAUDE_DIR/hooks/foresight_hook.py'"
    if [ "$DRY_RUN" -eq 1 ]; then
      printf "  [dry-run] merge foresight hooks into %s\n" "$SETTINGS"
    else
      SETTINGS="$SETTINGS" HOOK_CMD="$HOOK_CMD" API_KEY="$API_KEY" IDENTITY="$IDENTITY" python3 "$PLUGIN_DIR/install_support.py" settings
      _ok "Registered foresight hooks in ~/.claude/settings.json"
    fi
  fi
fi

# ── Claude Desktop ───────────────────────────────────────────────────────────

if [ "$DO_DESKTOP" -eq 1 ]; then
  echo "${C_BOLD}Claude Desktop${C_RESET}"

  case "$(uname -s)" in
    Darwin)  DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
    Linux)   DESKTOP_CONFIG="$HOME/.config/Claude/claude_desktop_config.json" ;;
    MINGW*|MSYS*|CYGWIN*) DESKTOP_CONFIG="${APPDATA:-$HOME/AppData/Roaming}/Claude/claude_desktop_config.json" ;;
    *)       DESKTOP_CONFIG="$HOME/.config/Claude/claude_desktop_config.json" ;;
  esac

  if [ ! -d "$(dirname "$DESKTOP_CONFIG")" ]; then
    _info "Claude Desktop config dir not found ($(dirname "$DESKTOP_CONFIG")) — skipping"
    _info "If you use Claude Desktop, create the dir and re-run with --desktop"
  else
    if [ "$DRY_RUN" -eq 1 ]; then
      printf "  [dry-run] merge mcpServers.foresight into %s\n" "$DESKTOP_CONFIG"
    else
      DESKTOP_CONFIG="$DESKTOP_CONFIG" MCP_URL="$MCP_URL" DESKTOP_STDIO="$DESKTOP_STDIO" API_KEY="$API_KEY" python3 "$PLUGIN_DIR/install_support.py" desktop
      if [ "$DESKTOP_STDIO" -eq 1 ]; then
        _ok "Registered foresight MCP server (stdio: foresight-server) in Claude Desktop config"
      else
        _ok "Registered foresight MCP server (http) in Claude Desktop config"
      fi
      _info "Restart Claude Desktop for the server to connect."
    fi
  fi
fi

# ── Plugin marketplace option ────────────────────────────────────────────────

echo
_info "This directory is also a valid Claude Code plugin:"
_info "  /plugin marketplace add ${PLUGIN_DIR%/}  then  /plugin install foresight@foresight"
_info "(or point CLAUDE_CODE_PLUGIN_DIRS at ${PLUGIN_DIR%/})"

echo
_ok "Done. Foresight memory is wired into your Claude clients."
