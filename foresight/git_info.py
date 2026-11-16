"""Read-only git metadata without spawning a process.

Both helpers answer questions about the *checked-out working tree* by reading
``.git`` directly, so callers never need ``subprocess``. Only the layouts git
itself writes are supported: a ``.git`` directory, a ``.git`` file pointing at
a worktree gitdir, loose refs, and ``packed-refs``. Environment overrides
(``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_CEILING_DIRECTORIES``) are ignored.
"""

from __future__ import annotations

import os
from pathlib import Path

_SHA_LENGTH = 40
_SHORT_SHA_LENGTH = 7


def find_git_root(start: str | Path | None = None) -> Path | None:
    """Return the nearest ancestor of *start* that contains a ``.git`` entry.

    ``.git`` may be a directory (ordinary checkout) or a file (worktree or
    submodule); either way the directory holding it is the working-tree root.
    """
    try:
        current = Path(start if start is not None else os.getcwd()).resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _git_dir(root: Path) -> Path | None:
    """Resolve the directory that actually holds ``HEAD`` for *root*."""
    entry = root / ".git"
    if entry.is_dir():
        return entry
    try:
        text = entry.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not text.startswith(prefix):
        return None
    target = Path(text[len(prefix) :].strip())
    if not target.is_absolute():
        target = (root / target).resolve()
    return target if target.is_dir() else None


def _resolve_ref(git_dir: Path, ref: str) -> str | None:
    """Resolve *ref* (e.g. ``refs/heads/main``) to a full SHA, or None."""
    try:
        loose = (git_dir / ref).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        loose = ""
    if len(loose) >= _SHA_LENGTH:
        return loose[:_SHA_LENGTH]

    try:
        packed = (git_dir / "packed-refs").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw_line in packed.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "^")):
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref:
            return sha[:_SHA_LENGTH]
    return None


def head_sha(start: str | Path | None = None, *, short: bool = True) -> str:
    """Return the commit SHA ``HEAD`` points at, or ``"unknown"``.

    Mirrors ``git rev-parse [--short] HEAD`` for the supported layouts. A
    detached ``HEAD`` holds the SHA itself; an attached one names a ref that is
    resolved from the loose ref file or ``packed-refs``.
    """
    root = find_git_root(start)
    if root is None:
        return "unknown"
    git_dir = _git_dir(root)
    if git_dir is None:
        return "unknown"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "unknown"

    sha: str | None
    if head.startswith("ref:"):
        sha = _resolve_ref(git_dir, head[len("ref:") :].strip())
    elif len(head) >= _SHA_LENGTH:
        sha = head[:_SHA_LENGTH]
    else:
        sha = None

    if sha is None:
        return "unknown"
    return sha[:_SHORT_SHA_LENGTH] if short else sha
