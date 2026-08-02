#!/usr/bin/env bash
# Foresight installer — pretty setup for CLI + MCP server
# Usage:  bash install.sh
#   or:   curl -fsSL https://raw.githubusercontent.com/daggerstuff/foresight/master/install.sh | bash

set -euo pipefail

# ── Colours & styles ─────────────────────────────────────────────────────────

if [ -t 1 ] && command -v tput &>/dev/null && tput colors &>/dev/null && [ "$(tput colors)" -ge 8 ]; then
  BOLD="\033[1m"
  DIM="\033[2m"
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
  BOLD="" DIM="" RESET=""
  C_PURPLE="" C_PINK="" C_GREEN="" C_RED="" C_YELLOW="" C_GRAY="" C_WHITE="" C_BLUE=""
fi

# ── Box drawing ───────────────────────────────────────────────────────────────

# Inner width of all boxes (chars between the │ walls).
BOX_W=53

_border_top()    { printf "  ${C_PURPLE}${BOLD}╭%s╮${RESET}\n" "$(printf '─%.0s' $(seq 1 $BOX_W))"; }
_border_bottom() { printf "  ${C_PURPLE}${BOLD}╰%s╯${RESET}\n" "$(printf '─%.0s' $(seq 1 $BOX_W))"; }
_border_empty()  { printf "  ${C_PURPLE}${BOLD}│%${BOX_W}s│${RESET}\n" ""; }

# _bline <display_string>
# Prints one │ … │ row, padding to BOX_W by stripping ANSI escapes to compute
# the visible length.  Works for any mix of colour codes + plain text.
_bline() {
  local display="$1"
  # Strip ANSI escape sequences to get visible (printable) width
  local vis
  vis=$(printf '%s' "$display" | sed 's/\x1b\[[0-9;]*[mK]//g')
  local vlen=${#vis}
  local pad=$(( BOX_W - vlen ))
  [ $pad -lt 0 ] && pad=0
  printf "  ${C_PURPLE}${BOLD}│${RESET}%s%${pad}s${C_PURPLE}${BOLD}│${RESET}\n" "$display" ""
}

# ── Step indicators ───────────────────────────────────────────────────────────

_label() { printf "\n  ${C_PURPLE}${BOLD}$1${RESET}\n\n"; }
_ok()    { printf "  ${C_GREEN}${BOLD}✓${RESET}  $1\n"; }
_warn()  { printf "  ${C_YELLOW}${BOLD}!${RESET}  $1\n"; }
_err()   { printf "\n  ${C_RED}${BOLD}✗${RESET}  $1\n\n" >&2; }
_step()  { printf "  ${C_GRAY}·${RESET}  $1\n"; }

# ── Spinner ───────────────────────────────────────────────────────────────────
# Runs a command in the background and shows an animated spinner.

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

# _prompt <var_name> <label> [<default>]
# Reads a line from /dev/tty (works even inside `curl | bash`).
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

# _menu <prompt> <item1> <item2> …  →  sets $REPLY to 1-based choice
_menu() {
  local prompt="$1"; shift
  local items=("$@")
  local n=${#items[@]}

  printf "  ${C_PINK}${BOLD}?${RESET}  ${BOLD}%s${RESET}\n\n" "$prompt"
  for i in "${!items[@]}"; do
    printf "    ${C_PURPLE}${BOLD}%d${RESET}  %b\n" "$((i+1))" "${items[$i]}"
  done
  printf "\n  ${C_GRAY}›${RESET} "

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

# ── Script directory (works whether run directly or via curl | bash) ──────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"

# ── Banner ────────────────────────────────────────────────────────────────────

_VERSION=""
_VERSION=$(grep '^version' "$SCRIPT_DIR/pyproject.toml" 2>/dev/null | sed 's/.*= *"//;s/".*//') || true

printf "\n"
_border_top
_bline "  ${C_PINK}${BOLD}🧠  foresight${RESET}  ${C_GRAY}${_VERSION}${RESET}"
_bline "  ${C_GRAY}persistent memory for AI agents${RESET}"
_border_bottom

# ── 1. Prerequisites ──────────────────────────────────────────────────────────

_label "Prerequisites"

if command -v uv &>/dev/null; then
  _ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
else
  _step "Installing uv …"
  if curl -LsSf https://astral.sh/uv/install.sh | sh; then
    export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
    _ok "uv installed"
  else
    _err "Could not install uv.  https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
  fi
fi

if command -v python3 &>/dev/null; then
  _ok "python $(python3 --version 2>&1 | awk '{print $2}')"
else
  _err "Python 3.12+ not found.  Install via pyenv, brew, or your system package manager."
  exit 1
fi

# ── 2. Dependencies ───────────────────────────────────────────────────────────

_label "Dependencies"

_spin "Installing packages  (CLI + TUI + MCP + Postgres)" \
  uv sync --extra all

# Make venv scripts available for the rest of this session
VENV_BIN="$SCRIPT_DIR/.venv/bin"
if [ -d "$VENV_BIN" ]; then
  export PATH="$VENV_BIN:$PATH"
fi

# ── 3. Database (Postgres DSN) ────────────────────────────────────────────────

_label "Database"

FORESIGHT_DB_URL="${FORESIGHT_DB_URL:-}"

if [ -n "$FORESIGHT_DB_URL" ]; then
  _ok "FORESIGHT_DB_URL is already set"
elif [ -n "${DATABASE_URL:-}" ]; then
  FORESIGHT_DB_URL="$DATABASE_URL"
  _ok "Using DATABASE_URL  (Replit managed Postgres)"
else
  printf "  ${C_YELLOW}${BOLD}!${RESET}  ${BOLD}Foresight requires PostgreSQL — SQLite is not supported.${RESET}\n"
  printf "  ${C_GRAY}  \$FORESIGHT_DB_URL is not set.${RESET}\n\n"

  _menu "Where is your Postgres database?" \
    "Neon        ${C_GRAY}— free tier, instant serverless Postgres${RESET}" \
    "Supabase    ${C_GRAY}— free tier, built-in dashboard${RESET}" \
    "Railway     ${C_GRAY}— simple hosted Postgres${RESET}" \
    "Replit      ${C_GRAY}— built-in managed Postgres (DATABASE_URL)${RESET}" \
    "Local       ${C_GRAY}— running on localhost${RESET}" \
    "Other       ${C_GRAY}— I already have a connection string${RESET}"

  case "$REPLY" in
    1)
      printf "\n  ${C_GRAY}Create a free database at ${RESET}${C_BLUE}${BOLD}https://neon.tech${RESET}\n"
      printf "  ${C_GRAY}Copy the connection string from Dashboard → Connection Details.${RESET}\n\n"
      _prompt FORESIGHT_DB_URL "Paste your Neon connection string"
      ;;
    2)
      printf "\n  ${C_GRAY}Create a free project at ${RESET}${C_BLUE}${BOLD}https://supabase.com${RESET}\n"
      printf "  ${C_GRAY}Go to Settings → Database → Connection string (URI mode).${RESET}\n\n"
      _prompt FORESIGHT_DB_URL "Paste your Supabase connection string"
      ;;
    3)
      printf "\n  ${C_GRAY}Create a Postgres service at ${RESET}${C_BLUE}${BOLD}https://railway.app${RESET}\n"
      printf "  ${C_GRAY}Copy the connection URL from the service variables tab.${RESET}\n\n"
      _prompt FORESIGHT_DB_URL "Paste your Railway connection string"
      ;;
    4)
      if [ -z "${DATABASE_URL:-}" ]; then
        printf "\n  ${C_GRAY}On Replit, open the Database tab and add the PostgreSQL integration.\n"
        printf "  DATABASE_URL is then injected automatically — re-run this script after.${RESET}\n\n"
        _err "DATABASE_URL is not set.  Add the Replit PostgreSQL integration first."
        exit 1
      else
        FORESIGHT_DB_URL="$DATABASE_URL"
        _ok "Using Replit DATABASE_URL"
      fi
      ;;
    5)
      _prompt FORESIGHT_DB_URL "Connection string" \
        "postgresql://user:pass@localhost:5432/foresight"
      ;;
    6)
      _prompt FORESIGHT_DB_URL "Paste your connection string"
      ;;
  esac

  [ -z "$FORESIGHT_DB_URL" ] && { _err "No connection string provided."; exit 1; }
fi

export FORESIGHT_DB_URL

# ── 4. Persist to .env ────────────────────────────────────────────────────────

ENV_FILE="$SCRIPT_DIR/.env"

if [ ! -f "$ENV_FILE" ] || ! grep -q "FORESIGHT_DB_URL" "$ENV_FILE" 2>/dev/null; then
  if [ "${FORESIGHT_DB_URL}" = "${DATABASE_URL:-}" ] && [ -n "${DATABASE_URL:-}" ]; then
    # Keep portable — expand at runtime, not at install time
    printf 'FORESIGHT_DB_URL=${DATABASE_URL}\n' >> "$ENV_FILE"
  else
    printf 'FORESIGHT_DB_URL=%s\n' "$FORESIGHT_DB_URL" >> "$ENV_FILE"
  fi
  _ok "Saved connection string to .env"
fi

# ── 5. Config & schema init ───────────────────────────────────────────────────

_label "Initialization"

MEMORY_DIR="$HOME/.foresight"
mkdir -p "$MEMORY_DIR"
chmod 700 "$MEMORY_DIR"
_ok "Memory directory  $MEMORY_DIR"

_spin "Initializing config and database schema" \
  uv run foresight system init --force

# ── 6. Health check ───────────────────────────────────────────────────────────

_label "Health check"

_spin "Running diagnostics  (7 checks)" \
  uv run foresight system doctor

# ── 7. PATH hint ──────────────────────────────────────────────────────────────

if ! command -v foresight &>/dev/null; then
  BIN_DIR="$VENV_BIN"
  _warn "foresight not on your PATH — add this to ~/.zshrc or ~/.bashrc:"
  printf "\n    ${C_GREEN}export PATH=\"\$PATH:%s\"${RESET}\n\n" "$BIN_DIR"
fi

# ── 8. Success ────────────────────────────────────────────────────────────────

printf "\n"
_border_top
_bline "  ${C_GREEN}${BOLD}✓  All done — Foresight is ready.${RESET}"
_border_empty
_bline "  ${BOLD}Try it:${RESET}"
_border_empty
_bline "  ${C_GRAY}store a memory    ${RESET}${C_PINK}foresight store \"hello\"${RESET}"
_bline "  ${C_GRAY}browse the TUI    ${RESET}${C_PINK}foresight tui${RESET}"
_bline "  ${C_GRAY}start MCP server  ${RESET}${C_PINK}uv run foresight-server${RESET}"
_bline "  ${C_GRAY}full command list  ${RESET}${C_PINK}foresight --help${RESET}"
_border_empty
_bline "  ${C_GRAY}docs  ${RESET}${C_BLUE}https://foresight.vectorize.io${RESET}"
_border_bottom
printf "\n"
