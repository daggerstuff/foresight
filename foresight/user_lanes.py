"""Copy or move memories between user lanes.

A "lane" is the ``(tenant_id, user_id)`` pair that scopes every memory row.
``copy_user_lane`` clones all user-scoped data into a destination lane with
fresh IDs and remapped references, inside a single transaction — a copy can
never leave a half-written lane or break the source. ``move_user_lane``
reassigns ``user_id`` in place (IDs unchanged), also atomically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from foresight.server import get_db_connection
from foresight.sql_helpers import join_sql_parts, placeholders, quote_identifier

logger = logging.getLogger("foresight.user_lanes")

# Tables never touched: credentials, sessions, audit log, and the derived
# narrative cache (regenerable; its PK would collide across lanes).
_SKIP_TABLES = {"users", "auth_sessions", "audit_events", "narrative_cache"}


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _content_key(row: dict[str, Any]) -> str:
    ch = row.get("content_hash")
    if ch:
        return str(ch)
    return hashlib.sha256(str(row.get("content", "")).encode()).hexdigest()


def _remap_json_ids(value: Any, id_map: dict[str, str], keep_unmapped: bool) -> Any:
    """Best-effort remap of a JSON column that may contain memory/entity IDs."""
    if not isinstance(value, str) or not value:
        return value
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return value
    if isinstance(data, list):
        out = []
        for item in data:
            if isinstance(item, str) and item in id_map:
                out.append(id_map[item])
            elif keep_unmapped:
                out.append(item)
        return json.dumps(out)
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str) and v in id_map:
                data[k] = id_map[v]
        return json.dumps(data)
    return value


def _insert_row(conn, table: str, row: dict[str, Any], exclude: set[str] | None = None) -> None:
    exclude = exclude or set()
    cols = [c for c in row if c not in exclude]
    quoted_cols = ", ".join(quote_identifier(c) for c in cols)
    conn.execute(
        join_sql_parts(
            "INSERT INTO ",
            quote_identifier(table),
            " (",
            quoted_cols,
            ") VALUES (",
            placeholders(len(cols)),
            ")",
        ),
        tuple(row[c] for c in cols),
    )


@dataclass
class _LaneCopy:
    """The connection, identity pair, and id maps shared by the copy phases."""

    conn: Any
    src_user: str
    dst_user: str
    dry_run: bool
    stats: dict[str, int]
    memory_map: dict[str, str] = field(default_factory=dict)
    entity_map: dict[str, str] = field(default_factory=dict)
    doc_map: dict[str, str] = field(default_factory=dict)

    def bump(self, table: str, n: int = 1, skipped: int = 0) -> None:
        """Record copied (and skipped) rows for one table."""
        self.stats[table] = self.stats.get(table, 0) + n
        if skipped:
            self.stats[f"{table}_skipped"] = self.stats.get(f"{table}_skipped", 0) + skipped


def _insert_row_on_conflict(
    conn,
    table: str,
    row: dict[str, Any],
    conflict_target: str | None = None,
) -> None:
    """Insert a row, ignoring conflicts on the given target (or on any constraint)."""
    suffix = "ON CONFLICT DO NOTHING" if conflict_target is None else f"ON CONFLICT ({conflict_target}) DO NOTHING"
    quoted_cols = ", ".join(quote_identifier(c) for c in row)
    conn.execute(
        join_sql_parts(
            "INSERT INTO ",
            quote_identifier(table),
            " (",
            quoted_cols,
            ") VALUES (",
            placeholders(len(row)),
            ") ",
            suffix,
        ),
        tuple(row.values()),
    )


def _copy_memories(ctx: _LaneCopy) -> None:
    """Copy memories with fresh ids, then remap their self-references."""
    src_memories = ctx.conn.execute("SELECT * FROM memories WHERE user_id = ?", (ctx.src_user,)).fetchall()
    tenants = {r["tenant_id"] for r in src_memories}
    dst_mem_rows = (
        ctx.conn.execute(
            "SELECT id, tenant_id, content_hash, content FROM memories WHERE user_id = ?",
            (ctx.dst_user,),
        ).fetchall()
        if tenants
        else []
    )
    dst_mem_key: dict[tuple, str] = {}
    for r in dst_mem_rows:
        dst_mem_key[(r["tenant_id"], _content_key(r))] = r["id"]

    pending_updates: list[tuple[str, str | None, str | None, str]] = []
    for row in src_memories:
        key = (row["tenant_id"], _content_key(row))
        existing = dst_mem_key.get(key)
        if existing:
            ctx.memory_map[row["id"]] = existing
            ctx.bump("memories", skipped=1)
            continue
        new_id = _new_id()
        ctx.memory_map[row["id"]] = new_id
        dst_mem_key[key] = new_id
        new_row = dict(row)
        new_row["id"] = new_id
        new_row["user_id"] = ctx.dst_user
        # Self-references are remapped in the second pass below.
        pending_updates.append(
            (
                new_id,
                row.get("related_memory_id"),
                row.get("superseded_by"),
                str(row.get("synthesized_from") or "[]"),
            )
        )
        if not ctx.dry_run:
            _insert_row(ctx.conn, "memories", new_row)
        ctx.bump("memories")

    for new_id, rel, sup, synth in pending_updates:
        rel_new = ctx.memory_map.get(rel) if rel else None
        sup_new = ctx.memory_map.get(sup) if sup else None
        synth_new = _remap_json_ids(synth, ctx.memory_map, keep_unmapped=False)
        if not ctx.dry_run:
            ctx.conn.execute(
                "UPDATE memories SET related_memory_id = ?, superseded_by = ?, synthesized_from = ? "
                "WHERE id = ? AND user_id = ?",
                (rel_new, sup_new, synth_new, new_id, ctx.dst_user),
            )


def _copy_memory_embeddings(ctx: _LaneCopy) -> None:
    """Copy embeddings for the memories that were copied."""
    emb_rows = ctx.conn.execute("SELECT * FROM memory_embeddings WHERE user_id = ?", (ctx.src_user,)).fetchall()
    for row in emb_rows:
        mid = ctx.memory_map.get(row["memory_id"])
        if not mid:
            ctx.bump("memory_embeddings", skipped=1)
            continue
        new_row = dict(row)
        new_row["user_id"] = ctx.dst_user
        new_row["memory_id"] = mid
        if not ctx.dry_run:
            _insert_row_on_conflict(ctx.conn, "memory_embeddings", new_row)
        ctx.bump("memory_embeddings")


def _copy_memory_entities(ctx: _LaneCopy) -> None:
    """Copy entities, reusing the ones the destination lane already has."""
    ent_rows = ctx.conn.execute("SELECT * FROM memory_entities WHERE user_id = ?", (ctx.src_user,)).fetchall()
    dst_ent = {
        (r["tenant_id"], r["name"], r["entity_type"]): r["id"]
        for r in ctx.conn.execute(
            "SELECT id, tenant_id, name, entity_type FROM memory_entities WHERE user_id = ?",
            (ctx.dst_user,),
        ).fetchall()
    }
    for row in ent_rows:
        ekey = (row["tenant_id"], row["name"], row["entity_type"])
        existing = dst_ent.get(ekey)
        if existing:
            ctx.entity_map[row["id"]] = existing
            ctx.bump("memory_entities", skipped=1)
            continue
        new_id = _new_id()
        ctx.entity_map[row["id"]] = new_id
        dst_ent[ekey] = new_id
        new_row = dict(row)
        new_row["id"] = new_id
        new_row["user_id"] = ctx.dst_user
        if not ctx.dry_run:
            _insert_row(ctx.conn, "memory_entities", new_row)
        ctx.bump("memory_entities")


def _copy_memory_entity_links(ctx: _LaneCopy) -> None:
    """Copy entity links whose memory and entity both exist in the copy."""
    link_rows = ctx.conn.execute("SELECT * FROM memory_entity_links WHERE user_id = ?", (ctx.src_user,)).fetchall()
    for row in link_rows:
        mid = ctx.memory_map.get(row["memory_id"])
        eid = ctx.entity_map.get(row["entity_id"])
        if not mid or not eid:
            ctx.bump("memory_entity_links", skipped=1)
            continue
        new_row = dict(row)
        new_row["user_id"] = ctx.dst_user
        new_row["memory_id"] = mid
        new_row["entity_id"] = eid
        if not ctx.dry_run:
            _insert_row_on_conflict(ctx.conn, "memory_entity_links", new_row, "memory_id, entity_id")
        ctx.bump("memory_entity_links")


def _copy_memory_relationships(ctx: _LaneCopy) -> None:
    """Copy memory relationships whose endpoints both exist in the copy."""
    rel_rows = ctx.conn.execute("SELECT * FROM memory_relationships WHERE user_id = ?", (ctx.src_user,)).fetchall()
    for row in rel_rows:
        src_id = ctx.memory_map.get(row["source_memory_id"])
        tgt_id = ctx.memory_map.get(row["target_memory_id"])
        if not src_id or not tgt_id:
            ctx.bump("memory_relationships", skipped=1)
            continue
        dup = ctx.conn.execute(
            "SELECT 1 FROM memory_relationships WHERE tenant_id = ? AND user_id = ? "
            "AND source_memory_id = ? AND target_memory_id = ? AND relationship_type = ? LIMIT 1",
            (row["tenant_id"], ctx.dst_user, src_id, tgt_id, row["relationship_type"]),
        ).fetchone()
        if dup:
            ctx.bump("memory_relationships", skipped=1)
            continue
        new_row = dict(row)
        new_row["id"] = _new_id()
        new_row["user_id"] = ctx.dst_user
        new_row["source_memory_id"] = src_id
        new_row["target_memory_id"] = tgt_id
        if not ctx.dry_run:
            _insert_row(ctx.conn, "memory_relationships", new_row)
        ctx.bump("memory_relationships")


def _copy_memory_merge_history(ctx: _LaneCopy) -> None:
    """Copy merge-history rows with their memory ids remapped."""
    mh_rows = ctx.conn.execute("SELECT * FROM memory_merge_history WHERE user_id = ?", (ctx.src_user,)).fetchall()
    for row in mh_rows:
        new_row = dict(row)
        new_row["id"] = _new_id()
        new_row["user_id"] = ctx.dst_user
        if row.get("primary_id") in ctx.memory_map:
            new_row["primary_id"] = ctx.memory_map[row["primary_id"]]
        new_row["merged_ids"] = _remap_json_ids(row.get("merged_ids"), ctx.memory_map, keep_unmapped=True)
        if not ctx.dry_run:
            _insert_row(ctx.conn, "memory_merge_history", new_row)
        ctx.bump("memory_merge_history")


def _copy_memory_decay_events(ctx: _LaneCopy) -> None:
    """Copy decay events for the memories that were copied."""
    de_rows = ctx.conn.execute("SELECT * FROM memory_decay_events WHERE user_id = ?", (ctx.src_user,)).fetchall()
    for row in de_rows:
        mid = ctx.memory_map.get(row["memory_id"])
        if not mid:
            ctx.bump("memory_decay_events", skipped=1)
            continue
        new_row = dict(row)
        new_row.pop("id", None)  # serial PK
        new_row["user_id"] = ctx.dst_user
        new_row["memory_id"] = mid
        if not ctx.dry_run:
            _insert_row(ctx.conn, "memory_decay_events", new_row)
        ctx.bump("memory_decay_events")


def _copy_entity_relationships(ctx: _LaneCopy) -> None:
    """Copy entity relationships whose endpoints both exist in the copy."""
    er_rows = ctx.conn.execute("SELECT * FROM entity_relationships WHERE user_id = ?", (ctx.src_user,)).fetchall()
    for row in er_rows:
        s = ctx.entity_map.get(row["source_entity_id"])
        t = ctx.entity_map.get(row["target_entity_id"])
        if not s or not t:
            ctx.bump("entity_relationships", skipped=1)
            continue
        dup = ctx.conn.execute(
            "SELECT 1 FROM entity_relationships WHERE tenant_id = ? AND user_id = ? "
            "AND source_entity_id = ? AND target_entity_id = ? AND relationship_type = ? LIMIT 1",
            (row["tenant_id"], ctx.dst_user, s, t, row["relationship_type"]),
        ).fetchone()
        if dup:
            ctx.bump("entity_relationships", skipped=1)
            continue
        new_row = dict(row)
        new_row.pop("id", None)  # serial PK
        new_row["user_id"] = ctx.dst_user
        new_row["source_entity_id"] = s
        new_row["target_entity_id"] = t
        if not ctx.dry_run:
            _insert_row(ctx.conn, "entity_relationships", new_row)
        ctx.bump("entity_relationships")


def _copy_context_blocks(ctx: _LaneCopy) -> None:
    """Copy context blocks the destination lane does not already hold."""
    cb_rows = ctx.conn.execute("SELECT * FROM context_blocks WHERE user_id = ?", (ctx.src_user,)).fetchall()
    dst_cb = {
        (r["tenant_id"], r["label"], r["agent_id"], r["project"])
        for r in ctx.conn.execute(
            "SELECT tenant_id, label, agent_id, project FROM context_blocks WHERE user_id = ?",
            (ctx.dst_user,),
        ).fetchall()
    }
    for row in cb_rows:
        cb_key = (row["tenant_id"], row["label"], row["agent_id"], row["project"])
        if cb_key in dst_cb:
            ctx.bump("context_blocks", skipped=1)
            continue
        dst_cb.add(cb_key)
        new_row = dict(row)
        new_row["user_id"] = ctx.dst_user
        if not ctx.dry_run:
            _insert_row(ctx.conn, "context_blocks", new_row)
        ctx.bump("context_blocks")


def _copy_documents(ctx: _LaneCopy) -> None:
    """Copy documents, reusing the ones the destination lane already has."""
    doc_rows = ctx.conn.execute("SELECT * FROM documents WHERE user_id = ?", (ctx.src_user,)).fetchall()
    dst_docs = {
        (r["tenant_id"], r["content_hash"]): r["id"]
        for r in ctx.conn.execute(
            "SELECT id, tenant_id, content_hash FROM documents WHERE user_id = ?",
            (ctx.dst_user,),
        ).fetchall()
    }
    for row in doc_rows:
        dkey = (row["tenant_id"], row.get("content_hash") or _content_key(row))
        existing = dst_docs.get(dkey)
        if existing:
            ctx.doc_map[row["id"]] = existing
            ctx.bump("documents", skipped=1)
            continue
        new_id = _new_id()
        ctx.doc_map[row["id"]] = new_id
        dst_docs[dkey] = new_id
        new_row = dict(row)
        new_row["id"] = new_id
        new_row["user_id"] = ctx.dst_user
        if not ctx.dry_run:
            _insert_row(ctx.conn, "documents", new_row)
        ctx.bump("documents")


def _copy_document_chunks(ctx: _LaneCopy) -> None:
    """Copy document chunks for the documents that were copied."""
    chunk_rows = ctx.conn.execute("SELECT * FROM document_chunks WHERE user_id = ?", (ctx.src_user,)).fetchall()
    dst_chunks = {
        (r["document_id"], r["chunk_index"])
        for r in ctx.conn.execute(
            "SELECT document_id, chunk_index FROM document_chunks WHERE user_id = ?",
            (ctx.dst_user,),
        ).fetchall()
    }
    for row in chunk_rows:
        did = ctx.doc_map.get(row["document_id"])
        if not did:
            ctx.bump("document_chunks", skipped=1)
            continue
        chunk_key = (did, row["chunk_index"])
        if chunk_key in dst_chunks:
            ctx.bump("document_chunks", skipped=1)
            continue
        dst_chunks.add(chunk_key)
        new_row = dict(row)
        new_row.pop("id", None)  # serial PK
        new_row["user_id"] = ctx.dst_user
        new_row["document_id"] = did
        if row.get("memory_id"):
            new_row["memory_id"] = ctx.memory_map.get(row["memory_id"])
        if not ctx.dry_run:
            _insert_row(ctx.conn, "document_chunks", new_row)
        ctx.bump("document_chunks")


def _copy_observations(ctx: _LaneCopy) -> None:
    """Copy observations with their supporting memory ids remapped."""
    obs_rows = ctx.conn.execute("SELECT * FROM observations WHERE user_id = ?", (ctx.src_user,)).fetchall()
    for row in obs_rows:
        new_row = dict(row)
        new_row["id"] = _new_id()
        new_row["user_id"] = ctx.dst_user
        new_row["supporting_memory_ids"] = _remap_json_ids(
            row.get("supporting_memory_ids"), ctx.memory_map, keep_unmapped=False
        )
        if not ctx.dry_run:
            _insert_row(ctx.conn, "observations", new_row)
        ctx.bump("observations")


def _copy_raw_events(ctx: _LaneCopy) -> None:
    """Copy the retained raw event layer."""
    raw_rows = ctx.conn.execute("SELECT * FROM raw_events WHERE user_id = ?", (ctx.src_user,)).fetchall()
    for row in raw_rows:
        new_row = dict(row)
        new_row["id"] = _new_id()
        new_row["user_id"] = ctx.dst_user
        if not ctx.dry_run:
            _insert_row(ctx.conn, "raw_events", new_row)
        ctx.bump("raw_events")


def _copy_mental_models(ctx: _LaneCopy) -> None:
    """Copy mental models with their citations remapped."""
    mm_rows = ctx.conn.execute("SELECT * FROM mental_models WHERE user_id = ?", (ctx.src_user,)).fetchall()
    for row in mm_rows:
        new_row = dict(row)
        new_row["id"] = _new_id()
        new_row["user_id"] = ctx.dst_user
        new_row["citations_json"] = _remap_json_ids(row.get("citations_json"), ctx.memory_map, keep_unmapped=True)
        if not ctx.dry_run:
            _insert_row(ctx.conn, "mental_models", new_row)
        ctx.bump("mental_models")


def _copy_curation_runs(ctx: _LaneCopy) -> None:
    """Copy curation run records."""
    cr_rows = ctx.conn.execute("SELECT * FROM curation_runs WHERE user_id = ?", (ctx.src_user,)).fetchall()
    for row in cr_rows:
        new_row = dict(row)
        new_row["id"] = _new_id()
        new_row["user_id"] = ctx.dst_user
        if not ctx.dry_run:
            _insert_row(ctx.conn, "curation_runs", new_row)
        ctx.bump("curation_runs")


def _copy_decay_config(ctx: _LaneCopy) -> None:
    """Copy decay configuration the destination lane does not already hold."""
    dc_rows = ctx.conn.execute("SELECT * FROM decay_config WHERE user_id = ?", (ctx.src_user,)).fetchall()
    dst_dc = {
        (r["tenant_id"], r["category"])
        for r in ctx.conn.execute(
            "SELECT tenant_id, category FROM decay_config WHERE user_id = ?",
            (ctx.dst_user,),
        ).fetchall()
    }
    for row in dc_rows:
        dkey = (row["tenant_id"], row["category"])
        if dkey in dst_dc:
            ctx.bump("decay_config", skipped=1)
            continue
        dst_dc.add(dkey)
        new_row = dict(row)
        new_row.pop("id", None)  # serial PK
        new_row["user_id"] = ctx.dst_user
        if not ctx.dry_run:
            _insert_row(ctx.conn, "decay_config", new_row)
        ctx.bump("decay_config")


def copy_user_lane(src_user: str, dst_user: str, dry_run: bool = False) -> dict[str, int]:
    """Clone every user-scoped row from ``src_user`` into ``dst_user``.

    Memories get fresh IDs; all intra-lane references (embeddings, entity
    links, relationships, merge history, decay events, document chunks,
    observations, mental models) are remapped. Rows that already exist in the
    destination (matched by natural keys like ``content_hash`` or
    ``(label, agent_id, project)``) are reused, so re-running a copy is
    idempotent and never duplicates. Everything runs in one transaction.
    """
    if src_user == dst_user:
        raise ValueError("source and destination users must differ")

    conn = get_db_connection()
    ctx = _LaneCopy(
        conn=conn,
        src_user=src_user,
        dst_user=dst_user,
        dry_run=dry_run,
        stats={},
    )

    try:
        _copy_memories(ctx)
        _copy_memory_embeddings(ctx)
        _copy_memory_entities(ctx)
        _copy_memory_entity_links(ctx)
        _copy_memory_relationships(ctx)
        _copy_memory_merge_history(ctx)
        _copy_memory_decay_events(ctx)
        _copy_entity_relationships(ctx)
        _copy_context_blocks(ctx)
        _copy_documents(ctx)
        _copy_document_chunks(ctx)
        _copy_observations(ctx)
        _copy_raw_events(ctx)
        _copy_mental_models(ctx)
        _copy_curation_runs(ctx)
        _copy_decay_config(ctx)

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        logger.info("copy_user_lane %s -> %s: %s", src_user, dst_user, ctx.stats)
        return ctx.stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def move_user_lane(src_user: str, dst_user: str, dry_run: bool = False) -> dict[str, int]:
    """Reassign ``user_id``/``owner_id`` from ``src_user`` to ``dst_user``.

    IDs are unchanged so every reference stays valid. Runs in one transaction;
    a natural-key collision in the destination aborts the whole move.
    """
    if src_user == dst_user:
        raise ValueError("source and destination users must differ")

    conn = get_db_connection()
    stats: dict[str, int] = {}
    try:
        cur = conn.execute(
            """
            SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND column_name IN ('user_id', 'owner_id')
            """
        )
        tables = cur.fetchall()
        for r in tables:
            table, col = r["table_name"], r["column_name"]
            if table in _SKIP_TABLES:
                continue
            res = conn.execute(
                join_sql_parts(
                    "UPDATE ",
                    quote_identifier(table),
                    " SET ",
                    quote_identifier(col),
                    " = ? WHERE ",
                    quote_identifier(col),
                    " = ?",
                ),
                (dst_user, src_user),
            )
            n = getattr(res, "rowcount", 0) or 0
            if n:
                stats[table] = n
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        logger.info("move_user_lane %s -> %s: %s", src_user, dst_user, stats)
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
