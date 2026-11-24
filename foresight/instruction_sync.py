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
from datetime import UTC, datetime
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

_SENTINEL_HINT = f"Add the block manually to opt in:\n  {SENTINEL_OPEN}\n  {SENTINEL_CLOSE}"


def fetch_instruction_memories(
    user_id: str,
    tenant_id: str,
    limit: int = DEFAULT_LIMIT,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Return the top instruction/preference/goal memories for sync.

    Importance >= 0.8, non-ghost, non-expired, non-sensitive, ordered by
    importance then recency. ``project`` / ``agent_id`` restrict rows to that
    lane plus the shared ``'default'`` lane, matching retrieval semantics, so
    another project's memories never land in this project's instruction files.
    ``conn`` may be injected by tests; otherwise the shared Postgres
    connection is used.
    """
    conn: Any | None = kwargs.get("conn")
    project: str | None = kwargs.get("project")
    agent_id: str | None = kwargs.get("agent_id")
    limit = max(1, min(int(limit), MAX_LIMIT))
    now_iso = datetime.now(UTC).isoformat()
    params: list[Any] = [user_id, tenant_id, MIN_IMPORTANCE, now_iso, *SYNC_CATEGORIES]
    lane_sql = ""
    if agent_id:
        lane_sql += "AND (agent_id = ? OR agent_id IS NULL OR agent_id = 'default') "
        params.append(agent_id)
    if project:
        lane_sql += "AND (project = ? OR project IS NULL OR project = 'default') "
        params.append(project)
    sql = join_sql_parts(
        "SELECT id, content, category, importance, updated_at, created_at ",
        "FROM memories ",
        "WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0 ",
        "AND expired_at IS NULL AND importance >= ? ",
        "AND (expires_at IS NULL OR expires_at >= ?) ",
        "AND COALESCE(is_sensitive, 0) = 0 ",
        "AND category IN (",
        placeholders(len(SYNC_CATEGORIES)),
        ") ",
        lane_sql,
        "ORDER BY importance DESC, COALESCE(updated_at, created_at) DESC ",
        "LIMIT ?",
    )
    params.append(limit)

    own_conn = conn is None
    if own_conn:
        conn = get_db_connection()
    if conn is None:
        return []
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        if own_conn and conn is not None:
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
        # A memory must not be able to forge the block delimiters.
        content = content.replace(SENTINEL_OPEN, "[FORESIGHT-OPEN]").replace(SENTINEL_CLOSE, "[FORESIGHT-CLOSE]")
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


class UnsafeInstructionPathError(ValueError):
    """Raised when an instruction file resolves outside the project root."""


def sync_instruction_file(path: Path, block: str, root: Path | None = None) -> str:
    """Atomically replace the sentinel block in one file.

    Returns ``"updated"``. Raises :class:`ValueError` when the file lacks
    sentinels (caller reports + skips) and :class:`UnsafeInstructionPathError` when
    a repository-controlled symlink points outside the project root.
    """
    # Follow a symlinked instruction file to its target: replacing the link
    # itself would break the symlink and leave the real file untouched.
    target = Path(os.path.realpath(path))
    if root is not None and not target.is_relative_to(Path(os.path.realpath(root))):
        raise UnsafeInstructionPathError(f"{path.name} resolves outside the project: {target}")
    text = target.read_text(encoding="utf-8")
    updated = _replace_block(text, block)
    if updated is None:
        raise ValueError("missing FORESIGHT-DYNAMIC-MEMORIES sentinels")
    if updated == text:
        return "unchanged"
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(updated)
        # mkstemp creates 0600; keep the instruction file's own permissions.
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, target.stat().st_mode & 0o7777)
        os.replace(tmp_name, target)
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
    **kwargs: Any,
) -> dict[str, Any]:
    """Sync instruction memories into the project's agent instruction files.

    ``memories`` may be injected by tests; otherwise they are recalled from
    the store. Returns a report: ``{"memories": n, "files": [...], "skipped": [...]}``.
    """
    memories: list[dict[str, Any]] | None = kwargs.get("memories")
    project: str | None = kwargs.get("project")
    agent_id: str | None = kwargs.get("agent_id")
    root = Path(project_dir)
    if memories is None:
        memories = fetch_instruction_memories(
            user_id,
            tenant_id,
            limit,
            project=project,
            agent_id=agent_id,
        )
    block = render_block(memories)

    report: dict[str, Any] = {"memories": len(memories), "files": [], "skipped": []}
    for name in INSTRUCTION_FILES:
        path = root / name
        if not path.is_file():
            continue
        try:
            status = sync_instruction_file(path, block, root)
        except UnicodeDecodeError as exc:
            report["skipped"].append({"file": name, "reason": f"decode-error: {exc}"})
        except UnsafeInstructionPathError as exc:
            report["skipped"].append({"file": name, "reason": f"unsafe-path: {exc}"})
        except ValueError:
            report["skipped"].append({"file": name, "reason": "missing-sentinels", "hint": _SENTINEL_HINT})
        except OSError as exc:
            report["skipped"].append({"file": name, "reason": f"io-error: {exc}"})
        else:
            report["files"].append({"file": name, "status": status})
    return report
