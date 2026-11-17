#!/usr/bin/env bash
#
# Prove that semgrep/sql-injection.yaml still reports what its own comments
# claim it reports, on every run.
#
# ---------------------------------------------------------------------------
# Why this exists
# ---------------------------------------------------------------------------
# The rule is a list of declared coverage: five source classes, nine sink
# methods, sixteen sanitizer patterns. Nothing in the rule enforces that those
# declarations match reality, and one of them already stopped matching: source
# class 5 was written for a Typer app, Typer was removed, and `@$APP.command`
# went on matching nothing while the gate printed "0 findings" and read as full
# coverage. A rule that reports nothing looks exactly like a rule that finds
# nothing.
#
# So every declared shape gets a control here, each with an exact expected
# finding count, and this script fails when any control deviates. Positives are
# asserted at an exact count (not ">= 1") and negatives at exactly zero, so a
# control cannot pass by scanning nothing.
#
# ---------------------------------------------------------------------------
# Why the fixtures are not .py files
# ---------------------------------------------------------------------------
# A committed control is Python living in this repository, so ruff, ruff-format,
# bandit, mypy and pytest would all collect it -- and every one of them would
# have something to say about code whose whole purpose is to be wrong. Silencing
# them would mean per-file-ignores, `# noqa`, `# nosec` or a mypy override, and
# this repository deleted all of those on purpose (see the zero-suppressions
# commits). Instead the fixtures are stored as `<name>.py.txt`: no linter walks
# them, no test collector imports them, no config change is needed. This script
# copies each one into a scratch directory renamed to `.py` and scans there.
#
# The fixtures live under semgrep/controls/, which is not one of the paths the
# main gate scans (foresight foresight_cli scripts plugins), so the target count
# of the main scan is unchanged by their existence.
#
# ---------------------------------------------------------------------------
# Why the fixture list lives in expectations.tsv
# ---------------------------------------------------------------------------
# The expected count has to be declared next to the fixture, not derived from
# it, or the control would assert "whatever semgrep currently does". Both
# directions are checked: a fixture missing from the table fails, and a table
# entry with no fixture fails.
#
# ---------------------------------------------------------------------------
# Why `--error` is absent
# ---------------------------------------------------------------------------
# The main gate runs with `--error` because any finding is a failure there. Here
# the findings are the point, so `--error` would turn a healthy run into exit 1
# and make "found what it should" indistinguishable from "the scan broke".
# `--strict` is kept: it makes a scan error (an unparseable fixture, a malformed
# rule) exit non-zero, which is a real failure. Everything else is the same
# pinned invocation the main gate uses.
#
# ---------------------------------------------------------------------------
# Reading a failure
# ---------------------------------------------------------------------------
# The table prints expected against actual per fixture. A positive that reports
# fewer findings than expected means a declared source class, sink method or
# assembly shape stopped matching. A negative that reports any finding means a
# sanitizer stopped sanitizing, or a bound parameter stopped being bound. The
# fixture docstring says which shape each function covers, and the finding lines
# are printed under the table for every deviating fixture.

set -euo pipefail

SEMGREP_VERSION="1.185.0"

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${REPO_ROOT}/semgrep/sql-injection.yaml"
CONTROLS_DIR="${REPO_ROOT}/semgrep/controls"
EXPECTATIONS="${CONTROLS_DIR}/expectations.tsv"

fail() {
    echo "check_semgrep_controls: $*" >&2
    exit 1
}

[ -f "$CONFIG" ] || fail "rule not found at ${CONFIG}"
[ -d "$CONTROLS_DIR" ] || fail "controls directory not found at ${CONTROLS_DIR}"
[ -f "$EXPECTATIONS" ] || fail "expectations table not found at ${EXPECTATIONS}"

shopt -s nullglob
FIXTURES=("${CONTROLS_DIR}"/*.py.txt)
shopt -u nullglob

if [ "${#FIXTURES[@]}" -eq 0 ]; then
    fail "no *.py.txt fixtures under ${CONTROLS_DIR}"
fi

declare -A EXPECTED=()
declare -A COVERAGE=()

while IFS=$'\t' read -r name count description; do
    [ -n "$name" ] || continue
    case "$name" in
        \#*) continue ;;
    esac
    if [[ ! "$count" =~ ^[0-9]+$ ]]; then
        fail "${EXPECTATIONS}: '${name}' has a non-numeric expected count '${count}'"
    fi
    if [ -n "${EXPECTED[$name]:-}" ]; then
        fail "${EXPECTATIONS}: '${name}' is listed more than once"
    fi
    EXPECTED[$name]="$count"
    COVERAGE[$name]="${description:-}"
done <"$EXPECTATIONS"

if [ "${#EXPECTED[@]}" -eq 0 ]; then
    fail "${EXPECTATIONS} declares no fixtures"
fi

for src in "${FIXTURES[@]}"; do
    file="$(basename -- "$src")"
    if [ -z "${EXPECTED[$file]:-}" ]; then
        fail "${file} exists but is not declared in ${EXPECTATIONS}"
    fi
done

for name in "${!EXPECTED[@]}"; do
    if [ ! -f "${CONTROLS_DIR}/${name}" ]; then
        fail "${EXPECTATIONS} declares ${name} but that file does not exist"
    fi
done

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/foresight-semgrep-controls.XXXXXX")"
cleanup() {
    rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT

for src in "${FIXTURES[@]}"; do
    file="$(basename -- "$src")"
    cp -- "$src" "${WORK_DIR}/${file%.py.txt}.py"
done

cd "$REPO_ROOT"

status=0
checked=0
deviations=()
declare -A ACTUAL=()

printf '%-42s %8s %8s  %s\n' "fixture" "expected" "actual" "result"
printf '%-42s %8s %8s  %s\n' "------------------------------------------" "--------" "--------" "------"

for src in "${FIXTURES[@]}"; do
    file="$(basename -- "$src")"
    stem="${file%.py.txt}"
    target="${WORK_DIR}/${stem}.py"
    want="${EXPECTED[$file]}"

    report="$(uv tool run --from "semgrep==${SEMGREP_VERSION}" semgrep scan \
        --config "$CONFIG" \
        --json \
        --quiet \
        --strict \
        --metrics=off \
        --disable-version-check \
        --no-git-ignore \
        "$target")" || fail "semgrep scan failed on ${file} (see the report above)"

    got="$(printf '%s' "$report" | { grep -o '"check_id"' || true; } | wc -l)"
    got="${got//[[:space:]]/}"
    ACTUAL[$file]="$got"

    checked=$((checked + 1))

    if [ "$got" = "$want" ]; then
        printf '%-42s %8s %8s  %s\n' "$stem" "$want" "$got" "ok"
    else
        printf '%-42s %8s %8s  %s\n' "$stem" "$want" "$got" "FAIL"
        deviations+=("$file")
        status=1
    fi
done

if [ "$status" -ne 0 ]; then
    echo
    echo "Deviating fixtures, with the reported finding lines:" >&2
    for file in "${deviations[@]}"; do
        stem="${file%.py.txt}"
        echo "--- ${file}" >&2
        echo "    covers: ${COVERAGE[$file]:-}" >&2
        echo "    expected ${EXPECTED[$file]} finding(s), got ${ACTUAL[$file]:-?}" >&2
        uv tool run --from "semgrep==${SEMGREP_VERSION}" semgrep scan \
            --config "$CONFIG" \
            --quiet \
            --metrics=off \
            --disable-version-check \
            --no-git-ignore \
            "${WORK_DIR}/${stem}.py" >&2 || true
    done
    fail "${#deviations[@]} of ${checked} control(s) deviated from ${EXPECTATIONS}"
fi

echo
echo "check_semgrep_controls: ${checked} control(s) matched their declared finding counts."
