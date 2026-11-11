"""Shared cleanup for rows that reference memories.

The schema has no FK cascades, so dependents must be swept explicitly.
``purge_dead_synthesized_from_ids`` handles the one reference type that
cannot be expressed in SQL: the ``synthesized_from`` JSON array on
``memories`` itself.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("foresight.dependent_cleanup")


def _row_get(row: Any, key: str, idx: int) -> Any:
    """Read a column from dict-like (server pool) or tuple (raw psycopg) rows."""
    if isinstance(row, dict):
        return row.get(key)
    return row[idx]


def purge_dead_synthesized_from_ids(conn, user_id: str | None = None, tenant_id: str | None = None) -> int:
    """Remove dead memory IDs from ``memories.synthesized_from`` arrays.

    Returns the number of memory rows updated. Idempotent — safe to run after
    any bulk or single memory delete.
    """
    scope_parts: list[str] = []
    scope_params: list = []
    if user_id is not None:
        scope_parts.append("user_id = ?")
        scope_params.append(user_id)
    if tenant_id is not None:
        scope_parts.append("tenant_id = ?")
        scope_params.append(tenant_id)
    scope = (" WHERE " + " AND ".join(scope_parts)) if scope_parts else ""

    candidates = conn.execute(
        "SELECT id, synthesized_from FROM memories "
        "WHERE synthesized_from IS NOT NULL AND synthesized_from != '' AND synthesized_from != '[]'" + scope,
        tuple(scope_params),
    ).fetchall()
    if not candidates:
        return 0

    valid_ids = {_row_get(r, "id", 0) for r in conn.execute("SELECT id FROM memories").fetchall()}

    updated = 0
    for row in candidates:
        mid = _row_get(row, "id", 0)
        raw = _row_get(row, "synthesized_from", 1)
        if not isinstance(raw, str):
            continue
        try:
            ids = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(ids, list):
            continue
        kept = [i for i in ids if not isinstance(i, str) or i in valid_ids]
        if len(kept) == len(ids):
            continue
        conn.execute(
            "UPDATE memories SET synthesized_from = ? WHERE id = ?",
            (json.dumps(kept), mid),
        )
        updated += 1
    if updated:
        logger.info("purged dead synthesized_from IDs from %d memories", updated)
    return updated
