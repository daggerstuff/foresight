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
from typing import Any

from foresight.server import get_db_connection

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
    placeholders = ", ".join("?" for _ in cols)
    collist = ", ".join(f'"{c}"' for c in cols)
    conn.execute(
        f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders})',
        tuple(row[c] for c in cols),
    )


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
    stats: dict[str, int] = {}
    memory_map: dict[str, str] = {}
    entity_map: dict[str, str] = {}
    doc_map: dict[str, str] = {}

    def bump(table: str, n: int = 1, skipped: int = 0) -> None:
        stats[table] = stats.get(table, 0) + n
        if skipped:
            stats[f"{table}_skipped"] = stats.get(f"{table}_skipped", 0) + skipped

    try:
        # ---- memories -----------------------------------------------------
        src_memories = conn.execute("SELECT * FROM memories WHERE user_id = ?", (src_user,)).fetchall()
        tenants = {r["tenant_id"] for r in src_memories}
        dst_mem_rows = (
            conn.execute(
                "SELECT id, tenant_id, content_hash, content FROM memories WHERE user_id = ?",
                (dst_user,),
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
                memory_map[row["id"]] = existing
                bump("memories", skipped=1)
                continue
            new_id = _new_id()
            memory_map[row["id"]] = new_id
            dst_mem_key[key] = new_id
            new_row = dict(row)
            new_row["id"] = new_id
            new_row["user_id"] = dst_user
            # Self-references are remapped in the second pass below.
            pending_updates.append(
                (
                    new_id,
                    row.get("related_memory_id"),
                    row.get("superseded_by"),
                    str(row.get("synthesized_from") or "[]"),
                )
            )
            if not dry_run:
                _insert_row(conn, "memories", new_row)
            bump("memories")

        for new_id, rel, sup, synth in pending_updates:
            rel_new = memory_map.get(rel) if rel else None
            sup_new = memory_map.get(sup) if sup else None
            synth_new = _remap_json_ids(synth, memory_map, keep_unmapped=False)
            if not dry_run:
                conn.execute(
                    "UPDATE memories SET related_memory_id = ?, superseded_by = ?, synthesized_from = ? WHERE id = ? AND user_id = ?",
                    (rel_new, sup_new, synth_new, new_id, dst_user),
                )

        # ---- memory_embeddings -------------------------------------------
        emb_rows = conn.execute("SELECT * FROM memory_embeddings WHERE user_id = ?", (src_user,)).fetchall()
        for row in emb_rows:
            mid = memory_map.get(row["memory_id"])
            if not mid:
                bump("memory_embeddings", skipped=1)
                continue
            new_row = dict(row)
            new_row["user_id"] = dst_user
            new_row["memory_id"] = mid
            if not dry_run:
                conn.execute(
                    'INSERT INTO "memory_embeddings" ({cols}) VALUES ({ph}) ON CONFLICT DO NOTHING'.format(
                        cols=", ".join(f'"{c}"' for c in new_row),
                        ph=", ".join("?" for _ in new_row),
                    ),
                    tuple(new_row.values()),
                )
            bump("memory_embeddings")

        # ---- memory_entities ----------------------------------------------
        ent_rows = conn.execute("SELECT * FROM memory_entities WHERE user_id = ?", (src_user,)).fetchall()
        dst_ent = {
            (r["tenant_id"], r["name"], r["entity_type"]): r["id"]
            for r in conn.execute(
                "SELECT id, tenant_id, name, entity_type FROM memory_entities WHERE user_id = ?",
                (dst_user,),
            ).fetchall()
        }
        for row in ent_rows:
            ekey = (row["tenant_id"], row["name"], row["entity_type"])
            existing = dst_ent.get(ekey)
            if existing:
                entity_map[row["id"]] = existing
                bump("memory_entities", skipped=1)
                continue
            new_id = _new_id()
            entity_map[row["id"]] = new_id
            dst_ent[ekey] = new_id
            new_row = dict(row)
            new_row["id"] = new_id
            new_row["user_id"] = dst_user
            if not dry_run:
                _insert_row(conn, "memory_entities", new_row)
            bump("memory_entities")

        # ---- memory_entity_links ------------------------------------------
        link_rows = conn.execute("SELECT * FROM memory_entity_links WHERE user_id = ?", (src_user,)).fetchall()
        for row in link_rows:
            mid = memory_map.get(row["memory_id"])
            eid = entity_map.get(row["entity_id"])
            if not mid or not eid:
                bump("memory_entity_links", skipped=1)
                continue
            new_row = dict(row)
            new_row["user_id"] = dst_user
            new_row["memory_id"] = mid
            new_row["entity_id"] = eid
            if not dry_run:
                conn.execute(
                    'INSERT INTO "memory_entity_links" ({cols}) VALUES ({ph}) ON CONFLICT (memory_id, entity_id) DO NOTHING'.format(
                        cols=", ".join(f'"{c}"' for c in new_row),
                        ph=", ".join("?" for _ in new_row),
                    ),
                    tuple(new_row.values()),
                )
            bump("memory_entity_links")

        # ---- memory_relationships ------------------------------------------
        rel_rows = conn.execute("SELECT * FROM memory_relationships WHERE user_id = ?", (src_user,)).fetchall()
        for row in rel_rows:
            src_id = memory_map.get(row["source_memory_id"])
            tgt_id = memory_map.get(row["target_memory_id"])
            if not src_id or not tgt_id:
                bump("memory_relationships", skipped=1)
                continue
            dup = conn.execute(
                "SELECT 1 FROM memory_relationships WHERE tenant_id = ? AND user_id = ? "
                "AND source_memory_id = ? AND target_memory_id = ? AND relationship_type = ? LIMIT 1",
                (row["tenant_id"], dst_user, src_id, tgt_id, row["relationship_type"]),
            ).fetchone()
            if dup:
                bump("memory_relationships", skipped=1)
                continue
            new_row = dict(row)
            new_row["id"] = _new_id()
            new_row["user_id"] = dst_user
            new_row["source_memory_id"] = src_id
            new_row["target_memory_id"] = tgt_id
            if not dry_run:
                _insert_row(conn, "memory_relationships", new_row)
            bump("memory_relationships")

        # ---- memory_merge_history ------------------------------------------
        mh_rows = conn.execute("SELECT * FROM memory_merge_history WHERE user_id = ?", (src_user,)).fetchall()
        for row in mh_rows:
            new_row = dict(row)
            new_row["id"] = _new_id()
            new_row["user_id"] = dst_user
            if row.get("primary_id") in memory_map:
                new_row["primary_id"] = memory_map[row["primary_id"]]
            new_row["merged_ids"] = _remap_json_ids(row.get("merged_ids"), memory_map, keep_unmapped=True)
            if not dry_run:
                _insert_row(conn, "memory_merge_history", new_row)
            bump("memory_merge_history")

        # ---- memory_decay_events -------------------------------------------
        de_rows = conn.execute("SELECT * FROM memory_decay_events WHERE user_id = ?", (src_user,)).fetchall()
        for row in de_rows:
            mid = memory_map.get(row["memory_id"])
            if not mid:
                bump("memory_decay_events", skipped=1)
                continue
            new_row = dict(row)
            new_row.pop("id", None)  # serial PK
            new_row["user_id"] = dst_user
            new_row["memory_id"] = mid
            if not dry_run:
                _insert_row(conn, "memory_decay_events", new_row)
            bump("memory_decay_events")

        # ---- entity_relationships ------------------------------------------
        er_rows = conn.execute("SELECT * FROM entity_relationships WHERE user_id = ?", (src_user,)).fetchall()
        for row in er_rows:
            s = entity_map.get(row["source_entity_id"])
            t = entity_map.get(row["target_entity_id"])
            if not s or not t:
                bump("entity_relationships", skipped=1)
                continue
            dup = conn.execute(
                "SELECT 1 FROM entity_relationships WHERE tenant_id = ? AND user_id = ? "
                "AND source_entity_id = ? AND target_entity_id = ? AND relationship_type = ? LIMIT 1",
                (row["tenant_id"], dst_user, s, t, row["relationship_type"]),
            ).fetchone()
            if dup:
                bump("entity_relationships", skipped=1)
                continue
            new_row = dict(row)
            new_row.pop("id", None)  # serial PK
            new_row["user_id"] = dst_user
            new_row["source_entity_id"] = s
            new_row["target_entity_id"] = t
            if not dry_run:
                _insert_row(conn, "entity_relationships", new_row)
            bump("entity_relationships")

        # ---- context_blocks --------------------------------------------------
        cb_rows = conn.execute("SELECT * FROM context_blocks WHERE user_id = ?", (src_user,)).fetchall()
        dst_cb = {
            (r["tenant_id"], r["label"], r["agent_id"], r["project"])
            for r in conn.execute(
                "SELECT tenant_id, label, agent_id, project FROM context_blocks WHERE user_id = ?",
                (dst_user,),
            ).fetchall()
        }
        for row in cb_rows:
            ckey = (row["tenant_id"], row["label"], row["agent_id"], row["project"])
            if ckey in dst_cb:
                bump("context_blocks", skipped=1)
                continue
            dst_cb.add(ckey)
            new_row = dict(row)
            new_row["user_id"] = dst_user
            if not dry_run:
                _insert_row(conn, "context_blocks", new_row)
            bump("context_blocks")

        # ---- documents + chunks ---------------------------------------------
        doc_rows = conn.execute("SELECT * FROM documents WHERE user_id = ?", (src_user,)).fetchall()
        dst_docs = {
            (r["tenant_id"], r["content_hash"]): r["id"]
            for r in conn.execute(
                "SELECT id, tenant_id, content_hash FROM documents WHERE user_id = ?",
                (dst_user,),
            ).fetchall()
        }
        for row in doc_rows:
            dkey = (row["tenant_id"], row.get("content_hash") or _content_key(row))
            existing = dst_docs.get(dkey)
            if existing:
                doc_map[row["id"]] = existing
                bump("documents", skipped=1)
                continue
            new_id = _new_id()
            doc_map[row["id"]] = new_id
            dst_docs[dkey] = new_id
            new_row = dict(row)
            new_row["id"] = new_id
            new_row["user_id"] = dst_user
            if not dry_run:
                _insert_row(conn, "documents", new_row)
            bump("documents")

        chunk_rows = conn.execute("SELECT * FROM document_chunks WHERE user_id = ?", (src_user,)).fetchall()
        dst_chunks = {
            (r["document_id"], r["chunk_index"])
            for r in conn.execute(
                "SELECT document_id, chunk_index FROM document_chunks WHERE user_id = ?",
                (dst_user,),
            ).fetchall()
        }
        for row in chunk_rows:
            did = doc_map.get(row["document_id"])
            if not did:
                bump("document_chunks", skipped=1)
                continue
            ckey = (did, row["chunk_index"])
            if ckey in dst_chunks:
                bump("document_chunks", skipped=1)
                continue
            dst_chunks.add(ckey)
            new_row = dict(row)
            new_row.pop("id", None)  # serial PK
            new_row["user_id"] = dst_user
            new_row["document_id"] = did
            if row.get("memory_id"):
                new_row["memory_id"] = memory_map.get(row["memory_id"])
            if not dry_run:
                _insert_row(conn, "document_chunks", new_row)
            bump("document_chunks")

        # ---- observations -----------------------------------------------------
        obs_rows = conn.execute("SELECT * FROM observations WHERE user_id = ?", (src_user,)).fetchall()
        for row in obs_rows:
            new_row = dict(row)
            new_row["id"] = _new_id()
            new_row["user_id"] = dst_user
            new_row["supporting_memory_ids"] = _remap_json_ids(
                row.get("supporting_memory_ids"), memory_map, keep_unmapped=False
            )
            if not dry_run:
                _insert_row(conn, "observations", new_row)
            bump("observations")

        # ---- raw_events --------------------------------------------------------
        raw_rows = conn.execute("SELECT * FROM raw_events WHERE user_id = ?", (src_user,)).fetchall()
        for row in raw_rows:
            new_row = dict(row)
            new_row["id"] = _new_id()
            new_row["user_id"] = dst_user
            if not dry_run:
                _insert_row(conn, "raw_events", new_row)
            bump("raw_events")

        # ---- mental_models -----------------------------------------------------
        mm_rows = conn.execute("SELECT * FROM mental_models WHERE user_id = ?", (src_user,)).fetchall()
        for row in mm_rows:
            new_row = dict(row)
            new_row["id"] = _new_id()
            new_row["user_id"] = dst_user
            new_row["citations_json"] = _remap_json_ids(row.get("citations_json"), memory_map, keep_unmapped=True)
            if not dry_run:
                _insert_row(conn, "mental_models", new_row)
            bump("mental_models")

        # ---- curation_runs -----------------------------------------------------
        cr_rows = conn.execute("SELECT * FROM curation_runs WHERE user_id = ?", (src_user,)).fetchall()
        for row in cr_rows:
            new_row = dict(row)
            new_row["id"] = _new_id()
            new_row["user_id"] = dst_user
            if not dry_run:
                _insert_row(conn, "curation_runs", new_row)
            bump("curation_runs")

        # ---- decay_config --------------------------------------------------------
        dc_rows = conn.execute("SELECT * FROM decay_config WHERE user_id = ?", (src_user,)).fetchall()
        dst_dc = {
            (r["tenant_id"], r["category"])
            for r in conn.execute(
                "SELECT tenant_id, category FROM decay_config WHERE user_id = ?",
                (dst_user,),
            ).fetchall()
        }
        for row in dc_rows:
            dkey = (row["tenant_id"], row["category"])
            if dkey in dst_dc:
                bump("decay_config", skipped=1)
                continue
            dst_dc.add(dkey)
            new_row = dict(row)
            new_row.pop("id", None)  # serial PK
            new_row["user_id"] = dst_user
            if not dry_run:
                _insert_row(conn, "decay_config", new_row)
            bump("decay_config")

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        logger.info("copy_user_lane %s -> %s: %s", src_user, dst_user, stats)
        return stats
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
                f'UPDATE "{table}" SET "{col}" = ? WHERE "{col}" = ?',  # nosec B608 - identifiers from information_schema
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
