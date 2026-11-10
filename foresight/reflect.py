"""Reflect — memory-backed reasoning with citations.

``reflect`` answers a question by running the hybrid retriever and then
synthesizing a concise answer over the retrieved memories. Unlike
``search_memories`` (which returns a raw slice) or ``inject_context``
(which returns context to inject), reflect returns a *conclusion* with
receipts: every claim is anchored to the memory IDs it was drawn from.

Synthesis prefers the configured LLM provider (``FORESIGHT_LLM_*`` env)
and degrades to a deterministic extractive summary when no provider is
configured or the call fails — reflect never hard-fails on LLM outage.

This module also owns the derived-layer stores introduced by later
phases: mental models (cached reflections, ``mental_models`` table) and
evidence-linked observations (``observations`` table). All SQL uses the
portable ``?`` placeholder style translated by the Postgres backend.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("foresight_reflect")

DEFAULT_MENTAL_MODEL_TTL_DAYS = 30
MENTAL_MODEL_TTL_ENV = "FORESIGHT_MENTAL_MODEL_TTL"

MAX_CITATION_EXCERPT_CHARS = 280
MAX_ANSWER_PROMPT_MEMORIES = 8


# ---------------------------------------------------------------------------
# Connection helper (same fallback shape as ContextBlockAgent._connect)
# ---------------------------------------------------------------------------


def _connect():
    try:
        from .server import get_db_connection

        return get_db_connection()
    except Exception:
        from .config import DB_PATH
        from .connection_pool import get_pool

        return get_pool(DB_PATH).acquire()


def _fetch_all(sql: str, params: tuple | list = ()) -> list[dict]:
    conn = _connect()
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _fetch_one(sql: str, params: tuple | list = ()) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _execute(sql: str, params: tuple | list = ()) -> None:
    conn = _connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Question normalization (shared by reflect + mental models)
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def question_key(question: str) -> str:
    """Normalize a question into a stable cache key.

    Lowercase, strip punctuation, collapse whitespace — the same
    normalization family ``query_rewrite`` uses for retrieval variants,
    kept dependency-free so it works identically on every backend.
    """
    text = _PUNCT_RE.sub(" ", question.strip().lower())
    return _WS_RE.sub(" ", text).strip()


def mental_model_ttl_days() -> float:
    """``FORESIGHT_MENTAL_MODEL_TTL`` in days (default 30)."""
    raw = os.environ.get(MENTAL_MODEL_TTL_ENV, "").strip()
    if not raw:
        return float(DEFAULT_MENTAL_MODEL_TTL_DAYS)
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %sd", MENTAL_MODEL_TTL_ENV, raw, DEFAULT_MENTAL_MODEL_TTL_DAYS)
        return float(DEFAULT_MENTAL_MODEL_TTL_DAYS)
    return value if value > 0 else float(DEFAULT_MENTAL_MODEL_TTL_DAYS)


# ---------------------------------------------------------------------------
# Reflect core
# ---------------------------------------------------------------------------


def _memory_rows_for_ids(memory_ids: list[str], uid: str, tenant_id: str) -> dict[str, dict]:
    """Fetch liveness metadata for citation validation."""
    if not memory_ids:
        return {}
    placeholders = ",".join("?" * len(memory_ids))
    rows = _fetch_all(
        f"SELECT id, is_ghost, expires_at, is_latest FROM memories "  # nosec B608 - values parameterized; IN placeholders generated count-only
        f"WHERE id IN ({placeholders}) AND user_id = ? AND tenant_id = ?",
        (*memory_ids, uid, tenant_id),
    )
    return {row["id"]: row for row in rows}


def _is_live(row: dict | None, now_iso: str, include_archived: bool) -> bool:
    if row is None:
        return False
    if not include_archived and row.get("is_ghost"):
        return False
    expires_at = row.get("expires_at")
    return not (expires_at and str(expires_at) < now_iso)


def _build_citations(results: list[Any], live_ids: set[str]) -> list[dict]:
    citations: list[dict] = []
    for r in results:
        if r.memory_id not in live_ids:
            continue
        excerpt = (r.content or "").strip()
        if len(excerpt) > MAX_CITATION_EXCERPT_CHARS:
            excerpt = excerpt[:MAX_CITATION_EXCERPT_CHARS].rsplit(" ", 1)[0] + "…"
        why = ", ".join(r.source_signals) if r.source_signals else (r.category or "memory")
        citations.append(
            {
                "memory_id": r.memory_id,
                "content_excerpt": excerpt,
                "score": round(float(r.combined_score), 4),
                "why_relevant": why,
            }
        )
    return citations


def _extractive_answer(citations: list[dict]) -> str:
    """Deterministic fallback synthesis — never fails, no LLM required."""
    if not citations:
        return "No relevant memories found to answer this question."
    lines = [f"Based on {len(citations)} memor{'y' if len(citations) == 1 else 'ies'}:"]
    for c in citations[:5]:
        lines.append(f"- [{c['memory_id']}] {c['content_excerpt']}")
    return "\n".join(lines)


def _llm_answer(question: str, citations: list[dict], uid: str, tenant_id: str) -> str | None:
    """Best-effort LLM synthesis. Returns None when unavailable/failing."""
    from . import llm_client

    excerpts = "\n".join(f"[{c['memory_id']}] {c['content_excerpt']}" for c in citations[:MAX_ANSWER_PROMPT_MEMORIES])
    prompt = (
        "Answer the question using ONLY the memories below. Cite each fact "
        "with its [memory_id]. If the memories do not answer the question, "
        "say so plainly. Be concise.\n\n"
        f"Question: {question}\n\nMemories:\n{excerpts}"
    )
    try:
        answer = llm_client.default_llm_call(prompt, tenant_id, uid)
    except Exception as exc:
        logger.info("reflect LLM synthesis unavailable, using extractive fallback: %s", exc)
        return None
    cleaned = str(answer).strip()
    return cleaned or None


def reflect(
    question: str,
    *,
    user_id: str,
    tenant_id: str,
    max_memories: int = 8,
    include_archived: bool = False,
    agent_id: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Answer ``question`` from the user's memories with citations.

    Returns a dict with ``answer``, ``citations``, ``memories_considered``,
    ``tokens_used``, ``cached`` and ``synthesized_by`` keys. Never raises
    for retrieval or synthesis failures — degradation is reported in-band.
    """
    from .hybrid_retriever import HybridSearchOptions, get_hybrid_retriever

    t0 = time.perf_counter()
    retriever = get_hybrid_retriever()
    search_options: dict[str, Any] = {}
    if agent_id is not None or project is not None:
        search_options["agent_id"] = agent_id
        search_options["project"] = project
    result = retriever.search(
        question,
        user_id,
        options=HybridSearchOptions(
            tenant_id=tenant_id,
            limit=max(1, int(max_memories)),
            min_importance=0.0,
            use_graph=True,
            use_temporal=True,
            **search_options,
        ),
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    candidates = result.results[: max(1, int(max_memories))]
    rows = _memory_rows_for_ids([r.memory_id for r in candidates], user_id, tenant_id)
    live_ids = {mid for mid, row in rows.items() if _is_live(row, now_iso, include_archived)}
    citations = _build_citations(candidates, live_ids)

    answer: str | None = None
    synthesized_by = "extractive"
    if citations:
        answer = _llm_answer(question, citations, user_id, tenant_id)
        if answer is not None:
            synthesized_by = "llm"
    if answer is None:
        answer = _extractive_answer(citations)

    tokens_used = max(1, (len(question) + len(answer) + sum(len(c["content_excerpt"]) for c in citations)) // 4)
    latency_ms = (time.perf_counter() - t0) * 1000

    return {
        "answer": answer,
        "citations": citations,
        "memories_considered": len(candidates),
        "memories_used": len(citations),
        "tokens_used": tokens_used,
        "cached": False,
        "synthesized_by": synthesized_by,
        "latency_ms": round(latency_ms, 2),
    }


# ---------------------------------------------------------------------------
# Mental models (cached reflections)
# ---------------------------------------------------------------------------


def _mental_model_row(row: dict) -> dict[str, Any]:
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "user_id": row["user_id"],
        "namespace_agent_id": row.get("namespace_agent_id"),
        "question_key": row["question_key"],
        "question_text": row["question_text"],
        "answer": row["answer"],
        "citations": json.loads(row["citations_json"] or "[]"),
        "hit_count": row["hit_count"],
        "last_hit_at": row["last_hit_at"],
        "created_at": row["created_at"],
        "expires_at": row.get("expires_at"),
        "stale": bool(row.get("stale", 0)),
    }


def lookup_mental_model(
    qkey: str,
    *,
    user_id: str,
    tenant_id: str,
    agent_id: str | None = None,
) -> dict | None:
    """Fetch a non-expired mental model for (tenant, user, question_key, agent lane)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    if agent_id is None:
        row = _fetch_one(
            "SELECT * FROM mental_models WHERE tenant_id = ? AND user_id = ? AND question_key = ? "
            "AND namespace_agent_id IS NULL AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY last_hit_at DESC LIMIT 1",
            (tenant_id, user_id, qkey, now_iso),
        )
    else:
        row = _fetch_one(
            "SELECT * FROM mental_models WHERE tenant_id = ? AND user_id = ? AND question_key = ? "
            "AND namespace_agent_id = ? AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY last_hit_at DESC LIMIT 1",
            (tenant_id, user_id, qkey, agent_id, now_iso),
        )
    return _mental_model_row(row) if row else None


def touch_mental_model(model_id: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    _execute(
        "UPDATE mental_models SET hit_count = hit_count + 1, last_hit_at = ? WHERE id = ?",
        (now_iso, model_id),
    )


def store_mental_model(
    question: str,
    result: dict[str, Any],
    *,
    user_id: str,
    tenant_id: str,
    agent_id: str | None = None,
) -> str:
    """Upsert a mental model for a reflect result. Returns the model id."""
    qkey = question_key(question)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    expires_at = (now + timedelta(days=mental_model_ttl_days())).isoformat()
    citations_json = json.dumps(result.get("citations", []))
    # namespace_agent_id is nullable and NULLs never satisfy a UNIQUE/ON
    # CONFLICT match, so upsert is done as lookup-then-update-or-insert.
    if agent_id is None:
        existing = _fetch_one(
            "SELECT id FROM mental_models WHERE tenant_id = ? AND user_id = ? AND question_key = ? "
            "AND namespace_agent_id IS NULL",
            (tenant_id, user_id, qkey),
        )
    else:
        existing = _fetch_one(
            "SELECT id FROM mental_models WHERE tenant_id = ? AND user_id = ? AND question_key = ? "
            "AND namespace_agent_id = ?",
            (tenant_id, user_id, qkey, agent_id),
        )
    if existing:
        _execute(
            "UPDATE mental_models SET question_text = ?, answer = ?, citations_json = ?, "
            "expires_at = ?, stale = 0, last_hit_at = ? WHERE id = ?",
            (question, result.get("answer", ""), citations_json, expires_at, now_iso, existing["id"]),
        )
        return existing["id"]
    model_id = hashlib.sha256(f"{tenant_id}:{user_id}:{agent_id or ''}:{qkey}".encode()).hexdigest()[:16]
    _execute(
        """INSERT INTO mental_models
        (id, tenant_id, user_id, namespace_agent_id, question_key, question_text,
         answer, citations_json, hit_count, last_hit_at, created_at, expires_at, stale)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0)""",
        (
            model_id,
            tenant_id,
            user_id,
            agent_id,
            qkey,
            question,
            result.get("answer", ""),
            citations_json,
            now_iso,
            now_iso,
            expires_at,
        ),
    )
    return model_id


def mental_model_citations_valid(model: dict, *, user_id: str, tenant_id: str, include_archived: bool = False) -> bool:
    """True when every cited memory still exists, is in-scope, and is live."""
    citations = model.get("citations") or []
    if not citations:
        return True
    ids = [c.get("memory_id") for c in citations if isinstance(c, dict) and c.get("memory_id")]
    if not ids:
        return True
    rows = _memory_rows_for_ids(ids, user_id, tenant_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    for mid in ids:
        if not _is_live(rows.get(mid), now_iso, include_archived):
            return False
        if rows[mid].get("is_latest") == 0:
            return False
    return True


def mark_mental_models_stale(memory_ids: list[str], *, user_id: str, tenant_id: str) -> int:
    """Flag mental models citing any of ``memory_ids`` as stale.

    Called when a memory is superseded or archived so the next reflect hit
    recomputes lazily instead of serving receipts that no longer resolve.
    """
    if not memory_ids:
        return 0
    # citations_json is a JSON array of objects; match on the memory_id text.
    like_clauses = " OR ".join(["citations_json LIKE ?" for _ in memory_ids])
    params: list[Any] = [f'%"memory_id": "{mid}"%' for mid in memory_ids]
    # Some rows serialize without the space after the colon — cover both.
    like_clauses2 = " OR ".join(["citations_json LIKE ?" for _ in memory_ids])
    params2: list[Any] = [f'%"memory_id":"{mid}"%' for mid in memory_ids]
    sql = (
        f"UPDATE mental_models SET stale = 1 WHERE tenant_id = ? AND user_id = ? "  # nosec B608 - values parameterized; LIKE fragments are bound params
        f"AND stale = 0 AND (({like_clauses}) OR ({like_clauses2}))"
    )
    conn = _connect()
    try:
        cur = conn.execute(sql, (tenant_id, user_id, *params, *params2))
        conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    finally:
        conn.close()


def list_mental_models(
    *,
    user_id: str,
    tenant_id: str,
    agent_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    if agent_id is None:
        rows = _fetch_all(
            "SELECT * FROM mental_models WHERE tenant_id = ? AND user_id = ? AND namespace_agent_id IS NULL "
            "ORDER BY last_hit_at DESC LIMIT ?",
            (tenant_id, user_id, limit),
        )
    else:
        rows = _fetch_all(
            "SELECT * FROM mental_models WHERE tenant_id = ? AND user_id = ? AND namespace_agent_id = ? "
            "ORDER BY last_hit_at DESC LIMIT ?",
            (tenant_id, user_id, agent_id, limit),
        )
    return [_mental_model_row(r) for r in rows]


def refresh_mental_model(model_id: str, *, user_id: str, tenant_id: str) -> dict | None:
    """Recompute a mental model from its stored question. Returns updated row."""
    row = _fetch_one(
        "SELECT * FROM mental_models WHERE id = ? AND tenant_id = ? AND user_id = ?",
        (model_id, tenant_id, user_id),
    )
    if not row:
        return None
    model = _mental_model_row(row)
    result = reflect(
        model["question_text"],
        user_id=user_id,
        tenant_id=tenant_id,
        agent_id=model.get("namespace_agent_id"),
    )
    store_mental_model(
        model["question_text"],
        result,
        user_id=user_id,
        tenant_id=tenant_id,
        agent_id=model.get("namespace_agent_id"),
    )
    return lookup_mental_model(
        model["question_key"],
        user_id=user_id,
        tenant_id=tenant_id,
        agent_id=model.get("namespace_agent_id"),
    )


def delete_mental_model(model_id: str, *, user_id: str, tenant_id: str) -> bool:
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM mental_models WHERE id = ? AND tenant_id = ? AND user_id = ?",
            (model_id, tenant_id, user_id),
        )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()
