#!/usr/bin/env bash
#
# Run the vendored Semgrep SQL-injection taint rules over the same paths Bandit
# covers. Exits non-zero on any finding, and on any scan error (--strict), so a
# file Semgrep cannot parse cannot silently drop out of the gate.
#
# Companion to the Bandit run in CI:
#   uv run bandit -c pyproject.toml -r foresight foresight_cli scripts
#
# ---------------------------------------------------------------------------
# Why `uv tool run` and not `uv run`
# ---------------------------------------------------------------------------
# semgrep cannot join this project's dependency graph:
#
#   * semgrep >= 1.137 pins an exact `mcp` (1.12.2 / 1.16.0 / 1.23.3 / 1.29.0),
#     every one of which is < 2.0, while this project requires
#     `mcp>=2.3.0,<3.0.0`. `uv add --group dev semgrep==1.185.0` therefore fails
#     dependency resolution and leaves pyproject.toml/uv.lock untouched.
#   * semgrep <= 1.136 has no `mcp` pin and would resolve, but it fails to import
#     on Python 3.14 (opentelemetry/protobuf: "Metaclasses with custom tp_new
#     are not supported"), which is the interpreter this project runs on.
#
# So the version is pinned here, at the invocation, and `uv tool run` installs
# it into an isolated environment. uv.lock stays untouched and the version is
# still exact and reproducible. Bump SEMGREP_VERSION deliberately, and re-run
# both controls in the rule header afterwards.
#
# ---------------------------------------------------------------------------
# Why the scan is offline with respect to rules
# ---------------------------------------------------------------------------
# The config is vendored at semgrep/sql-injection.yaml, so there is no `p/...`
# remote ruleset, no Semgrep account, no login and no registry fetch. Only
# `semgrep scan` is used -- never `semgrep ci`, which resolves a remote ruleset
# from the Semgrep backend.

set -euo pipefail

SEMGREP_VERSION="1.185.0"

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${REPO_ROOT}/semgrep/sql-injection.yaml"

# Default to exactly the paths the Bandit step in CI covers. Optional arguments
# override the set, which is what the positive-control check uses:
#   scripts/run_semgrep_sql.sh /tmp/fs-semgrep-controls/positive_control.py
TARGETS=(foresight foresight_cli scripts)
if [[ $# -gt 0 ]]; then
    TARGETS=("$@")
fi

if [[ ! -f "$CONFIG" ]]; then
    echo "run_semgrep_sql: vendored config not found at ${CONFIG}" >&2
    exit 2
fi

cd "$REPO_ROOT"

# --error          exit non-zero when any finding is reported
# --strict         exit non-zero when the scan itself reports errors
# --metrics=off    no telemetry
# --disable-version-check  no update ping (keeps the run offline)
# --no-git-ignore  walk the tree like Bandit does, so the two gates cover the
#                  same files instead of semgrep honouring .gitignore
exec uv tool run --from "semgrep==${SEMGREP_VERSION}" semgrep scan \
    --config "$CONFIG" \
    --error \
    --strict \
    --metrics=off \
    --disable-version-check \
    --no-git-ignore \
    "${TARGETS[@]}"
