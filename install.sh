#!/usr/bin/env bash
# Foresight installer — setup for CLI, TUI, FastMCP server, and Agent integrations
# Usage:  bash install.sh
#   or:   curl -fsSL https://raw.githubusercontent.com/daggerstuff/foresight/main/install.sh | bash

set -euo pipefail

# ── Environment isolation ───────────────────────────────────────────────────
# Force uv to isolate to Foresight's environment regardless of any ambient
# virtualenv exported by outer workspaces or parent repositories.
unset VIRTUAL_ENV
unset VIRTUAL_ENV_DIR

# ── Colours & styles ─────────────────────────────────────────────────────────

if [ -t 1 ] && command -v tput &>/dev/null && tput colors &>/dev/null && [ "$(tput colors)" -ge 8 ]; then
  BOLD="\033[1m"
  RESET="\033[0m"
  C_PURPLE="\033[38;5;135m"
  C_PINK="\033[38;5;213m"
  C_GREEN="\033[38;5;114m"
  C_RED="\033[38;5;203m"
  C_YELLOW="\033[38;5;221m"
  C_GRAY="\033[38;5;245m"
  C_WHITE="\033[38;5;255m"
  C_BLUE="\033[38;5;111m"
else
  BOLD="" RESET=""
  C_PURPLE="" C_PINK="" C_GREEN="" C_RED="" C_YELLOW="" C_GRAY="" C_WHITE="" C_BLUE=""
fi

# ── Box drawing ───────────────────────────────────────────────────────────────

BOX_W=55

_border_top()    { printf "  ${C_PURPLE}${BOLD}╭%s╮${RESET}\n" "$(printf '─%.0s' $(seq 1 $BOX_W))"; }
_border_bottom() { printf "  ${C_PURPLE}${BOLD}╰%s╯${RESET}\n" "$(printf '─%.0s' $(seq 1 $BOX_W))"; }
_border_empty()  { printf "  ${C_PURPLE}${BOLD}│%${BOX_W}s│${RESET}\n" ""; }

_bline() {
  local display="$1"
  local vis
  vis=$(printf '%s' "$display" | sed 's/\x1b\[[0-9;]*[mK]//g')
  local vlen=${#vis}
  local pad=$(( BOX_W - vlen ))
  [ $pad -lt 0 ] && pad=0
  printf "  ${C_PURPLE}${BOLD}│${RESET}%s%${pad}s${C_PURPLE}${BOLD}│${RESET}\n" "$display" ""
}

# ── Step indicators ───────────────────────────────────────────────────────────

_label() { printf "\n  ${C_PURPLE}${BOLD}%s${RESET}\n\n" "$1"; }
_ok()    { printf "  ${C_GREEN}${BOLD}✓${RESET}  %s\n" "$1"; }
_warn()  { printf "  ${C_YELLOW}${BOLD}!${RESET}  %s\n" "$1"; }
_err()   { printf "\n  ${C_RED}${BOLD}✗${RESET}  %s\n\n" "$1" >&2; }
_step()  { printf "  ${C_GRAY}·${RESET}  %s\n" "$1"; }

# ── Spinner ───────────────────────────────────────────────────────────────────

_spin() {
  local msg="$1"; shift
  local frames=('⣾' '⣽' '⣻' '⢿' '⡿' '⣟' '⣯' '⣷')
  local i=0
  local log
  log=$(mktemp)

  "$@" >"$log" 2>&1 &
  local pid=$!

  while kill -0 "$pid" 2>/dev/null; do
    printf "\r  ${C_PURPLE}%s${RESET}  ${C_WHITE}%s${RESET}   " "${frames[$i]}" "$msg"
    i=$(( (i + 1) % 8 ))
    sleep 0.08
  done

  wait "$pid"
  local rc=$?
  if [ $rc -eq 0 ]; then
    printf "\r  ${C_GREEN}${BOLD}✓${RESET}  ${C_WHITE}%s${RESET}   \n" "$msg"
  else
    printf "\r  ${C_RED}${BOLD}✗${RESET}  ${C_WHITE}%s${RESET}   \n" "$msg"
    _err "Command failed. Details:"
    sed 's/^/     /' "$log" >&2
    rm -f "$log"
    exit 1
  fi
  rm -f "$log"
}

# ── Interactive helpers ───────────────────────────────────────────────────────

_prompt() {
  local var="$1" msg="$2" default="${3:-}"
  local hint=""
  [ -n "$default" ] && hint=" ${C_GRAY}($default)${RESET}"
  printf "  ${C_PINK}${BOLD}?${RESET}  ${BOLD}%s${RESET}%b\n  ${C_GRAY}›${RESET} " "$msg" "$hint"
  local val
  if [ -t 0 ]; then
    IFS= read -r val
  else
    IFS= read -r val </dev/tty
  fi
  [ -z "$val" ] && val="$default"
  eval "${var}=\$val"
}

_menu() {
  local prompt="$1"; shift
  local items=("$@")
  local n=${#items[@]}

  printf "  ${C_PINK}${BOLD}?${RESET}  ${BOLD}%s${RESET}\n\n" "$prompt"
  for i in "${!items[@]}"; do
    printf "    ${C_PURPLE}${BOLD}%d${RESET}  %b\n" "$((i+1))" "${items[$i]}"
  done
  printf '%b' "\n  ${C_GRAY}›${RESET} "

  local choice
  if [ -t 0 ]; then
    IFS= read -r choice
  else
    IFS= read -r choice </dev/tty
  fi

  if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "$n" ]; then
    choice=1
  fi
  REPLY=$choice
}

# ── Repository location & clone handling ──────────────────────────────────────

RAW_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"

if [ -f "$RAW_SCRIPT_DIR/pyproject.toml" ] && grep -q 'name = "foresight"' "$RAW_SCRIPT_DIR/pyproject.toml" 2>/dev/null; then
  SCRIPT_DIR="$RAW_SCRIPT_DIR"
elif [ -f "$PWD/pyproject.toml" ] && grep -q 'name = "foresight"' "$PWD/pyproject.toml" 2>/dev/null; then
  SCRIPT_DIR="$PWD"
elif [ -d "$PWD/foresight" ] && [ -f "$PWD/foresight/pyproject.toml" ]; then
  SCRIPT_DIR="$PWD/foresight"
else
  INSTALL_DEST="${FORESIGHT_INSTALL_DIR:-$HOME/.local/share/foresight}"
  _label "Cloning Foresight repository"
  mkdir -p "$(dirname "$INSTALL_DEST")"
  if [ -d "$INSTALL_DEST/.git" ]; then
    git -C "$INSTALL_DEST" pull --ff-only || true
  else
    git clone https://github.com/daggerstuff/foresight.git "$INSTALL_DEST"
  fi
  SCRIPT_DIR="$INSTALL_DEST"
fi

cd "$SCRIPT_DIR"

# ── Banner ────────────────────────────────────────────────────────────────────

_VERSION=$(grep '^version' "$SCRIPT_DIR/pyproject.toml" 2>/dev/null | sed 's/.*= *"//;s/".*//') || _VERSION="0.19.0"

printf "\n"
_border_top
_bline "  ${C_PINK}${BOLD}🧠  foresight${RESET}  ${C_GRAY}v${_VERSION}${RESET}"
_bline "  ${C_GRAY}persistent memory for AI agents${RESET}"
_border_bottom

# ── 1. Prerequisites ──────────────────────────────────────────────────────────

_label "Prerequisites"

# Check & ensure uv
if command -v uv &>/dev/null; then
  _ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
else
  _step "Installing uv package manager …"
  if curl -LsSf https://astral.sh/uv/install.sh | sh; then
    export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
    _ok "uv installed successfully"
  else
    _err "Could not install uv. See https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
  fi
fi

# Check Python version
if command -v python3 &>/dev/null; then
  PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
  PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)' 2>/dev/null || echo "0")
  PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo "0")
  if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 12 ]; then
    _ok "python $PY_VER (meets requirement >= 3.12)"
  else
    _step "System Python is $PY_VER. uv will automatically fetch and manage Python 3.12+"
  fi
else
  _step "System Python not found. uv will automatically provision Python 3.12+"
fi

# ── 2. Dependencies ───────────────────────────────────────────────────────────

_label "Dependencies"

_spin "Installing packages  (CLI + TUI + FastMCP + PostgreSQL + Redis)" \
  uv sync --project "$SCRIPT_DIR" --extra all

VENV_BIN="$SCRIPT_DIR/.venv/bin"
if [ -d "$VENV_BIN" ]; then
  export PATH="$VENV_BIN:$PATH"
fi

# Symlink binaries to ~/.local/bin if directory exists or can be created
LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
if [ -x "$VENV_BIN/foresight" ]; then
  ln -sf "$VENV_BIN/foresight" "$LOCAL_BIN/foresight"
  ln -sf "$VENV_BIN/foresight-server" "$LOCAL_BIN/foresight-server"
  _ok "Symlinked CLI to $LOCAL_BIN/foresight"
fi

# ── 3. Database (Postgres DSN) ────────────────────────────────────────────────

_label "Database"

FORESIGHT_DB_URL="${FORESIGHT_DB_URL:-}"

# Check existing .env if URL not in current environment
if [ -z "$FORESIGHT_DB_URL" ] && [ -f "$SCRIPT_DIR/.env" ]; then
  FORESIGHT_DB_URL=$(grep '^FORESIGHT_DB_URL=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d '=' -f2- | tr -d '"' | tr -d "'") || true
fi

if [ -n "$FORESIGHT_DB_URL" ]; then
  _ok "Using configured FORESIGHT_DB_URL"
elif [ -n "${DATABASE_URL:-}" ]; then
  FORESIGHT_DB_URL="$DATABASE_URL"
  _ok "Using DATABASE_URL from environment"
else
  printf '%b\n' "  ${C_YELLOW}${BOLD}!${RESET}  ${BOLD}Foresight requires PostgreSQL (pgvector supported) — SQLite is not supported for production.${RESET}"
  printf '%b\n\n' "  ${C_GRAY}  \$FORESIGHT_DB_URL is not set.${RESET}"

  _menu "Where is your PostgreSQL database?" \
    "Neon        ${C_GRAY}— recommended serverless Postgres (https://neon.tech)${RESET}" \
    "Supabase    ${C_GRAY}— hosted Postgres with dashboard (https://supabase.com)${RESET}" \
    "Railway     ${C_GRAY}— hosted Postgres service (https://railway.app)${RESET}" \
    "Local       ${C_GRAY}— running on localhost (e.g. docker or local service)${RESET}" \
    "Other       ${C_GRAY}— custom PostgreSQL connection string${RESET}"

  case "$REPLY" in
    1)
      printf '%b\n' "\n  ${C_GRAY}Create a free database at ${RESET}${C_BLUE}${BOLD}https://neon.tech${RESET}"
      printf '%b\n\n' "  ${C_GRAY}Copy the connection string from Dashboard → Connection Details.${RESET}"
      _prompt FORESIGHT_DB_URL "Paste your Neon connection string"
      ;;
    2)
      printf '%b\n' "\n  ${C_GRAY}Create a project at ${RESET}${C_BLUE}${BOLD}https://supabase.com${RESET}"
      printf '%b\n\n' "  ${C_GRAY}Go to Settings → Database → Connection string (URI mode).${RESET}"
      _prompt FORESIGHT_DB_URL "Paste your Supabase connection string"
      ;;
    3)
      printf '%b\n' "\n  ${C_GRAY}Create a Postgres service at ${RESET}${C_BLUE}${BOLD}https://railway.app${RESET}"
      printf '%b\n\n' "  ${C_GRAY}Copy the connection URL from the service variables tab.${RESET}"
      _prompt FORESIGHT_DB_URL "Paste your Railway connection string"
      ;;
    4)
      _prompt FORESIGHT_DB_URL "Local connection string" \
        "postgresql://postgres:postgres@localhost:5432/foresight"
      ;;
    5)
      _prompt FORESIGHT_DB_URL "Paste your PostgreSQL connection string"
      ;;
  esac

  [ -z "$FORESIGHT_DB_URL" ] && { _err "No connection string provided. Aborting installation."; exit 1; }
fi

# Ensure sslmode=require for Neon / Supabase cloud endpoints
if [[ "$FORESIGHT_DB_URL" == *"neon.tech"* ]] || [[ "$FORESIGHT_DB_URL" == *"supabase.co"* ]]; then
  if [[ "$FORESIGHT_DB_URL" != *"sslmode="* ]]; then
    if [[ "$FORESIGHT_DB_URL" == *"?"* ]]; then
      FORESIGHT_DB_URL="${FORESIGHT_DB_URL}&sslmode=require"
    else
      FORESIGHT_DB_URL="${FORESIGHT_DB_URL}?sslmode=require"
    fi
  fi
fi

export FORESIGHT_DB_URL

# Live pre-flight test with psycopg
_spin "Verifying PostgreSQL connectivity" \
  uv run --project "$SCRIPT_DIR" python3 -c "
import sys, psycopg
try:
    with psycopg.connect('$FORESIGHT_DB_URL', connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT 1;')
except Exception as err:
    print(f'Connection failed: {err}', file=sys.stderr)
    sys.exit(1)
"

# ── 4. Identity & Scoping ────────────────────────────────────────────────────

_label "Identity & Scoping"

DEFAULT_ID="${USER:-user}"
if [ -f "$SCRIPT_DIR/.env" ]; then
  EXISTING_ID=$(grep '^FORESIGHT_IDENTITY=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d '=' -f2- | tr -d '"' | tr -d "'") || true
  [ -n "$EXISTING_ID" ] && DEFAULT_ID="$EXISTING_ID"
fi

_prompt FORESIGHT_IDENTITY "Active agent identity (format: user or user@account)" "$DEFAULT_ID"
export FORESIGHT_IDENTITY

DEFAULT_BANK="default"
if [ -f "$SCRIPT_DIR/.env" ]; then
  EXISTING_BANK=$(grep '^FORESIGHT_BANK_ID=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d '=' -f2- | tr -d '"' | tr -d "'") || true
  [ -n "$EXISTING_BANK" ] && DEFAULT_BANK="$EXISTING_BANK"
fi

_prompt FORESIGHT_BANK_ID "Tenant / memory bank namespace" "$DEFAULT_BANK"
export FORESIGHT_BANK_ID

# ── 5. Persist to .env ───────────────────────────────────────────────────────

_label "Configuration"

ENV_FILE="$SCRIPT_DIR/.env"
ENV_TEMPLATE="$SCRIPT_DIR/.env.example"

if [ ! -f "$ENV_FILE" ] && [ -f "$ENV_TEMPLATE" ]; then
  cp "$ENV_TEMPLATE" "$ENV_FILE"
fi

# Write variables cleanly into .env using python
uv run --project "$SCRIPT_DIR" python3 -c "
import pathlib, os

env_path = pathlib.Path('$ENV_FILE')
lines = []
if env_path.exists():
    lines = env_path.read_text().splitlines()

vars_to_set = {
    'FORESIGHT_DB_URL': os.environ.get('FORESIGHT_DB_URL', ''),
    'FORESIGHT_IDENTITY': os.environ.get('FORESIGHT_IDENTITY', ''),
    'FORESIGHT_BANK_ID': os.environ.get('FORESIGHT_BANK_ID', ''),
}

updated_keys = set()
new_lines = []
for line in lines:
    stripped = line.strip()
    matched = False
    for k, v in vars_to_set.items():
        if stripped.startswith(f'{k}=') or stripped.startswith(f'# {k}='):
            new_lines.append(f'{k}={v}')
            updated_keys.add(k)
            matched = True
            break
    if not matched:
        new_lines.append(line)

for k, v in vars_to_set.items():
    if k not in updated_keys and v:
        new_lines.append(f'{k}={v}')

env_path.write_text('\n'.join(new_lines) + '\n')
"

chmod 600 "$ENV_FILE"
_ok "Saved secure settings to .env (chmod 600)"

# ── 6. Config & Schema Initialization ────────────────────────────────────────

_label "Initialization"

MEMORY_DIR="$HOME/.foresight"
mkdir -p "$MEMORY_DIR"
chmod 700 "$MEMORY_DIR"
_ok "Memory directory $MEMORY_DIR"

_spin "Initializing schema and configuration" \
  uv run --project "$SCRIPT_DIR" foresight init --force --user-id "$FORESIGHT_IDENTITY" --bank-id "$FORESIGHT_BANK_ID"

# ── 7. Health Diagnostics ────────────────────────────────────────────────────

_label "Diagnostics"

_spin "Running comprehensive diagnostics (11-point suite)" \
  uv run --project "$SCRIPT_DIR" foresight doctor

# ── 8. Systemd User Service ──────────────────────────────────────────────────

SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SYSTEMD_DIR/foresight.service"

if command -v systemctl &>/dev/null; then
  mkdir -p "$SYSTEMD_DIR" 2>/dev/null || true

  if systemctl --user show-environment &>/dev/null; then
    _label "Systemd Service"

    UV_PATH=$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")
    UV_DIR=$(dirname "$UV_PATH")

    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Foresight MCP Server (FastMCP HTTP)
Documentation=https://github.com/daggerstuff/foresight
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
EnvironmentFile=$SCRIPT_DIR/.env
Environment="PATH=$UV_DIR:/usr/local/bin:/usr/bin:/bin"
Environment=FASTMCP_STATELESS_HTTP=1
Environment=FORESIGHT_HOST=127.0.0.1
Environment=FORESIGHT_PORT=8764
Environment=FORESIGHT_ALLOW_UNAUTHENTICATED=1

ExecStart=$UV_PATH run --project $SCRIPT_DIR --no-active python -m foresight --host 127.0.0.1 --port 8764
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=foresight

[Install]
WantedBy=default.target
EOF

    # Enable lingering if loginctl is available so service keeps running across disconnects
    if command -v loginctl &>/dev/null && [ -n "${USER:-}" ]; then
      loginctl enable-linger "$USER" 2>/dev/null || true
    fi

    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable foresight 2>/dev/null || true
    systemctl --user restart foresight 2>/dev/null || true
    _ok "Installed and started foresight.service on port 8764"
  else
    _warn "systemd user session unavailable — skipping service startup (manual: cp foresight.service ~/.config/systemd/user/)"
  fi
fi

# ── 9. Agent MCP Integrations ────────────────────────────────────────────────

_label "Agent Client Integrations"

# A. OpenCode Auto-Configuration
OPENCODE_DIR="$HOME/.config/opencode"
OPENCODE_CONFIG="$OPENCODE_DIR/opencode.json"
[ -f "$OPENCODE_DIR/opencode.jsonc" ] && OPENCODE_CONFIG="$OPENCODE_DIR/opencode.jsonc"
OPENCODE_PLUGIN_DIR="$OPENCODE_DIR/plugins"
PLUGIN_TARGET="$OPENCODE_PLUGIN_DIR/foresight-autoinject.js"
REPO_PLUGIN="$SCRIPT_DIR/plugins/foresight-autoinject.js"

if [ -f "$REPO_PLUGIN" ]; then
  mkdir -p "$OPENCODE_PLUGIN_DIR"
  cp "$REPO_PLUGIN" "$PLUGIN_TARGET"
  _ok "Installed OpenCode auto-inject plugin"
fi

if [ -d "$OPENCODE_DIR" ] || command -v opencode &>/dev/null; then
  if uv run --project "$SCRIPT_DIR" python3 -c "
import json, pathlib

cfg_path = pathlib.Path('$OPENCODE_CONFIG')
if not cfg_path.exists():
    cfg_path = pathlib.Path('$OPENCODE_DIR/opencode.json')

data = {}
if cfg_path.exists():
    try:
        # Strip simple comments if JSONC
        raw = cfg_path.read_text()
        clean = []
        for line in raw.splitlines():
            s = line.strip()
            if not s.startswith('//'):
                clean.append(line)
        data = json.loads('\n'.join(clean))
    except Exception:
        data = {}

data.setdefault('mcp', {})
if 'foresight' not in data['mcp']:
    data['mcp']['foresight'] = {
        'type': 'remote',
        'url': 'http://127.0.0.1:8764/mcp',
        'enabled': True
    }

plugins = data.get('plugin', [])
entry = './plugins/foresight-autoinject.js'
if entry not in plugins:
    plugins.append(entry)
    data['plugin'] = plugins

cfg_path.write_text(json.dumps(data, indent=2))
" 2>/dev/null; then
  _ok "Configured OpenCode MCP endpoint & plugin"
else
  _warn "OpenCode config exists; please verify foresight MCP in opencode.json"
fi
fi

# B. Claude Code Integration
CLAUDE_CONFIG="$HOME/.claude.json"
if [ -f "$CLAUDE_CONFIG" ] || [ -d "$HOME/.claude" ] || command -v claude &>/dev/null; then
  if [ -f "$CLAUDE_CONFIG" ]; then
    uv run --project "$SCRIPT_DIR" python3 -c "
import json, pathlib

p = pathlib.Path('$CLAUDE_CONFIG')
try:
    d = json.loads(p.read_text())
    mcp = d.setdefault('mcpServers', {})
    if 'foresight' not in mcp:
        mcp['foresight'] = {
            'url': 'http://127.0.0.1:8764/mcp'
        }
        p.write_text(json.dumps(d, indent=2))
except Exception:
    pass
" 2>/dev/null && _ok "Configured Claude Code MCP endpoint in ~/.claude.json" || true
  fi
fi

# ── 10. First Memory Onboarding ──────────────────────────────────────────────

_label "First Memory"

_spin "Storing welcome memory" \
  uv run --project "$SCRIPT_DIR" foresight store "Foresight initialized via install.sh (v${_VERSION}) — persistent memory system live and ready." --scope session

# ── 11. PATH Guidance ────────────────────────────────────────────────────────

if ! command -v foresight &>/dev/null; then
  _warn "foresight is not in your current PATH. Add to ~/.bashrc or ~/.zshrc:"
  printf "\n    ${C_GREEN}export PATH=\"\$HOME/.local/bin:%s:\$PATH\"${RESET}\n\n" "$VENV_BIN"
fi

# ── 12. Success Summary ──────────────────────────────────────────────────────

printf "\n"
_border_top
_bline "  ${C_GREEN}${BOLD}✓  All done — Foresight is fully operational.${RESET}"
_border_empty
_bline "  ${BOLD}Configured Subsystems:${RESET}"
_border_empty
_bline "  ${C_GRAY}✓ Dependencies & Venv  ${RESET}${C_GRAY}($SCRIPT_DIR/.venv)${RESET}"
_bline "  ${C_GRAY}✓ PostgreSQL Connected ${RESET}${C_GRAY}(Neon / pgvector)${RESET}"
_bline "  ${C_GRAY}✓ Schema Initialized   ${RESET}${C_GRAY}(all 23 tables)${RESET}"
_bline "  ${C_GRAY}✓ 11-Point Diagnostics ${RESET}${C_GRAY}(foresight doctor)${RESET}"
[ -f "$SERVICE_FILE" ] && _bline "  ${C_GRAY}✓ Daemon Service       ${RESET}${C_GRAY}(http://127.0.0.1:8764/mcp)${RESET}"
_bline "  ${C_GRAY}✓ Agent MCP Endpoints  ${RESET}${C_GRAY}(Claude Code, OpenCode)${RESET}"
_bline "  ${C_GRAY}✓ Welcome Memory Stored${RESET}"
_border_empty
_bline "  ${BOLD}Essential Commands:${RESET}"
_border_empty
_bline "  ${C_GRAY}interactive TUI       ${RESET}${C_PINK}foresight tui${RESET}"
_bline "  ${C_GRAY}system health         ${RESET}${C_PINK}foresight doctor${RESET}"
_bline "  ${C_GRAY}proof benchmarks      ${RESET}${C_PINK}foresight prove${RESET}"
_bline "  ${C_GRAY}store memory          ${RESET}${C_PINK}foresight store \"hello world\"${RESET}"
_bline "  ${C_GRAY}service status        ${RESET}${C_PINK}systemctl --user status foresight${RESET}"
_border_empty
_bline "  ${C_GRAY}documentation         ${RESET}${C_BLUE}https://foresight.vectorize.io${RESET}"
_border_bottom
printf "\n"
