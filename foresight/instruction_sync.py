"""Opt-in sync of top memories into agent instruction files.

Recalls the highest-importance instruction/preference/goal memories and
writes them as ``- [TYPE] content`` lines inside a sentinel-delimited block
in detected agent instruction files (``CLAUDE.md``, ``AGENTS.md``,
``GEMINI.md``)::

    <!-- FORESIGHT-DYNAMIC-MEMORIES -->
    - [PREFERENCE] prefers concise answers
    <!-- /FORESIGHT-DYNAMIC-MEMORIES -->

Strictly opt-in: this only ever runs via the explicit ``foresight sync``
command, only rewrites the sentinel block, and skips (reporting) any file
that lacks the sentinels — content outside the block is never touched.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .connection_pool import get_db_connection
from .encryption import decrypt_if_encrypted
from .sql_helpers import join_sql_parts, placeholders

logger = logging.getLogger("foresight.instruction_sync")

SENTINEL_OPEN = "<!-- FORESIGHT-DYNAMIC-MEMORIES -->"
SENTINEL_CLOSE = "<!-- /FORESIGHT-DYNAMIC-MEMORIES -->"

# Instruction files probed inside the project directory, in priority order.
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")

# Memory categories eligible for instruction-file sync.
SYNC_CATEGORIES = ("instruction", "instructions", "preference", "preferences", "goal", "goals")

MIN_IMPORTANCE = 0.8
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_LINE_CHARS = 500

_SENTINEL_HINT = (
    f"Add the block manually to opt in:\n"
    f"  {SENTINEL_OPEN}\n"
    f"  {SENTINEL_CLOSE}"
)


def fetch_instruction_memories(
    user_id: str,
    tenant_id: str,
    limit: int = DEFAULT_LIMIT,
    conn: Any | None = None,
) -> list[dict[str, Any]]:
    """Return the top instruction/preference/goal memories for sync.

    Importance >= 0.8, non-ghost, non-expired, ordered by importance then
    recency. ``conn`` may be injected by tests; otherwise the shared
    Postgres connection is used.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    sql = join_sql_parts(
        "SELECT id, content, category, importance, updated_at, created_at ",
        "FROM memories ",
        "WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0 ",
        "AND expired_at IS NULL AND importance >= ? ",
        "AND category IN (", placeholders(len(SYNC_CATEGORIES)), ") ",
        "ORDER BY importance DESC, COALESCE(updated_at, created_at) DESC ",
        "LIMIT ?",
    )
    params = (user_id, tenant_id, MIN_IMPORTANCE, *SYNC_CATEGORIES, limit)

    own_conn = conn is None
    if own_conn:
        conn = get_db_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        if own_conn:
            conn.close()

    memories: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        content = decrypt_if_encrypted(r["content"], tenant_id=tenant_id, user_id=user_id)
        memories.append(
            {
                "id": r["id"],
                "content": content,
                "category": (r.get("category") or "general").upper(),
                "importance": r.get("importance"),
            }
        )
    return memories


def render_block(memories: list[dict[str, Any]]) -> str:
    """Render memories as sentinel-wrapped ``- [TYPE] content`` lines."""
    lines = [SENTINEL_OPEN]
    for mem in memories:
        content = " ".join(str(mem["content"]).split())
        if len(content) > MAX_LINE_CHARS:
            content = content[: MAX_LINE_CHARS - 1].rstrip() + "…"
        lines.append(f"- [{str(mem['category']).upper()}] {content}")
    lines.append(SENTINEL_CLOSE)
    return "\n".join(lines)


def _replace_block(text: str, block: str) -> str | None:
    """Replace the sentinel block in ``text``; None when sentinels are absent."""
    start = text.find(SENTINEL_OPEN)
    end = text.find(SENTINEL_CLOSE)
    if start == -1 or end == -1 or end < start:
        return None
    end += len(SENTINEL_CLOSE)
    return text[:start] + block + text[end:]


def sync_instruction_file(path: Path, block: str) -> str:
    """Atomically replace the sentinel block in one file.

    Returns ``"updated"``. Raises :class:`ValueError` when the file lacks
    sentinels (caller reports + skips).
    """
    text = path.read_text(encoding="utf-8")
    updated = _replace_block(text, block)
    if updated is None:
        raise ValueError("missing FORESIGHT-DYNAMIC-MEMORIES sentinels")
    if updated == text:
        return "unchanged"
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(updated)
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return "updated"


def run_sync(
    project_dir: str | Path,
    user_id: str,
    tenant_id: str,
    limit: int = DEFAULT_LIMIT,
    memories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Sync instruction memories into the project's agent instruction files.

    ``memories`` may be injected by tests; otherwise they are recalled from
    the store. Returns a report: ``{"memories": n, "files": [...], "skipped": [...]}``.
    """
    root = Path(project_dir)
    if memories is None:
        memories = fetch_instruction_memories(user_id, tenant_id, limit)
    block = render_block(memories)

    report: dict[str, Any] = {"memories": len(memories), "files": [], "skipped": []}
    for name in INSTRUCTION_FILES:
        path = root / name
        if not path.is_file():
            continue
        try:
            status = sync_instruction_file(path, block)
        except ValueError:
            report["skipped"].append({"file": name, "reason": "missing-sentinels", "hint": _SENTINEL_HINT})
        except OSError as exc:
            report["skipped"].append({"file": name, "reason": f"io-error: {exc}"})
        else:
            report["files"].append({"file": name, "status": status})
    return report
