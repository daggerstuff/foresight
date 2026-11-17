#!/usr/bin/env bash
#
# Fail when a git-tracked shell script carries Python embedded in a here-document.
#
# Path-based scanners (Bandit, Semgrep) only read real .py files, so a Python
# block spliced into a shell script is invisible to both gates. This check makes
# that class of file impossible to add without the security gates going red.
#
# Only tracked files are considered, via `git ls-files`, so untracked
# working-tree files cannot trigger a failure. Every offending file and line is
# printed before the non-zero exit.
#
# Wired into scripts/run_semgrep_sql.sh, which runs it before the scan so the
# security step can never claim coverage it does not have.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# A python interpreter invocation -- `python` or `python3`, optionally with a
# `-` argument -- immediately followed by the here-document operator.
HEREDOC_RE='python3?[[:space:]]+(-[[:space:]]*)?<<'
SHEBANG_RE='^#!.*[[:space:]/](ba|da|k|z)?sh([[:space:]]|$)'

status=0

while IFS= read -r -d '' file; do
    [ -f "$file" ] || continue
    case "$file" in
        *.sh | *.bash) ;;
        *)
            head -n 1 "$file" 2>/dev/null | grep -Eq "$SHEBANG_RE" || continue
            ;;
    esac
    matches="$(grep -nHE -- "$HEREDOC_RE" "$file" || true)"
    if [ -n "$matches" ]; then
        status=1
        printf '%s\n' "$matches"
    fi
done < <(git ls-files -z)

if [ "$status" -ne 0 ]; then
    echo "check_no_embedded_python: embedded Python here-documents found (listed above)." >&2
    echo "Move the Python into a real .py file so the security gates can scan it." >&2
    exit 1
fi

echo "check_no_embedded_python: no embedded Python here-documents in tracked shell files."
