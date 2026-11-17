#!/usr/bin/env bash
#
# Run the vendored Semgrep SQL-injection taint rules over the same paths Bandit
# covers. Exits non-zero on any finding, and on any scan error (--strict), so a
# file Semgrep cannot parse cannot silently drop out of the gate.
#
# Companion to the Bandit run in CI:
#   uv run bandit -c pyproject.toml -r foresight foresight_cli scripts plugins
#
# ---------------------------------------------------------------------------
# The gate proves itself before it scans
# ---------------------------------------------------------------------------
# A taint rule that reports nothing looks exactly like a rule that finds
# nothing, and this one has already had a declared source class stop matching
# while the gate printed "0 findings" and read as coverage. So the committed
# positive and negative controls in semgrep/controls/ are asserted first, by
# scripts/check_semgrep_controls.sh, with an exact expected finding count per
# fixture. If any declared source class, sink method, assembly shape or
# sanitizer stops behaving as the rule's comments claim, that check fails and
# this script never reaches the scan.
#
# The check is skipped when target paths are passed on the command line, since
# that form is for looking at a single file and the controls do not depend on
# the target set. CI invokes this script with no arguments, so CI always runs
# it.
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
# still exact and reproducible. Bump SEMGREP_VERSION deliberately: the controls
# are re-asserted on the next run, so a bump that changes what the rule reports
# fails there instead of reaching the scan.
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
# override the set, which is how a single file is inspected while chasing a
# finding:
#   scripts/run_semgrep_sql.sh /tmp/candidate.py
# Passing targets also skips the control self-test below, because that check
# asserts the rule against its own fixtures and does not depend on the target
# set. CI passes no targets, so CI always runs the self-test.
TARGETS=(foresight foresight_cli scripts plugins)
TARGETS_OVERRIDDEN=0
if [[ $# -gt 0 ]]; then
    TARGETS=("$@")
    TARGETS_OVERRIDDEN=1
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

# Embedded Python inside shell scripts is invisible to a path-based scan, so the
# guard runs first and fails fast: the scan can never report coverage it does
# not have.
"${REPO_ROOT}/scripts/check_no_embedded_python.sh"

# Then prove the rule still reports what it claims, before scanning anything
# that matters. `exec` below replaces this process, so the self-test has to run
# as a separate step ahead of it.
if [[ "$TARGETS_OVERRIDDEN" -eq 0 ]]; then
    "${REPO_ROOT}/scripts/check_semgrep_controls.sh"
fi

exec uv tool run --from "semgrep==${SEMGREP_VERSION}" semgrep scan \
    --config "$CONFIG" \
    --error \
    --strict \
    --metrics=off \
    --disable-version-check \
    --no-git-ignore \
    "${TARGETS[@]}"
