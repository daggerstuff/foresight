"""Evidence-linked observations — recurring patterns distilled from memories.

Observations are the derived layer between raw memories and context blocks:
where a memory records *what happened*, an observation records *what keeps
happening* — a recurring pattern, trend, or contradiction — always anchored
to the memory IDs that support it.

``curate_observations`` scans recent memories for a (tenant, user, category)
scope, runs the enhanced synthesizer's contradiction/trend detection, and
upserts observations. Refinement never overwrites evidence: supporting
memory IDs are unioned, ``version`` increments, and ``last_refined_at``
advances, so the observation's provenance grows monotonically.

All SQL uses the portable ``?`` placeholder style translated by the
Postgres backend, and every read/write filters on ``tenant_id`` +
``user_id``.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .reflect import _connect, _fetch_all, _fetch_one

logger = logging.getLogger("foresight_observations")

MIN_CLUSTER_SIZE = 2
MAX_INJECTION_OBSERVATIONS = 3
_RECENCY_HALF_LIFE_DAYS = 30.0


def _execute(sql: str, params: tuple | list = ()) -> None:
    conn = _connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _observation_row(row: dict) -> dict[str, Any]:
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "user_id": row["user_id"],
        "pattern_key": row.get("pattern_key", ""),
        "observation_text": row["observation_text"],
        "category": row.get("category"),
        "confidence": float(row.get("confidence") or 0.5),
        "supporting_memory_ids": json.loads(row.get("supporting_memory_ids") or "[]"),
        "evidence_count": int(row.get("evidence_count") or 0),
        "status": row.get("status", "active"),
        "first_seen_at": row["first_seen_at"],
        "last_refined_at": row.get("last_refined_at"),
        "version": int(row.get("version") or 1),
    }


def _memory_objects(rows: list[dict]) -> list[Any]:
    from .memory_types import MemoryObject

    objects = []
    for row in rows:
        try:
            tags = json.loads(row.get("tags") or "[]")
        except (TypeError, ValueError):
            tags = []
        objects.append(
            MemoryObject(
                id=row["id"],
                timestamp=row.get("created_at") or _now_iso(),
                scope=row.get("scope") or "arc",
                retention=row.get("retention") or "long_term",
                content=row.get("content") or "",
                tags=tags if isinstance(tags, list) else [],
            )
        )
    return objects


def _slug(text: str, length: int = 48) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:length] or "pattern"


def _detect_candidates(rows: list[dict]) -> list[dict[str, Any]]:
    """Detect recurring patterns via the enhanced synthesizer.

    Returns candidate dicts: pattern_key, observation_text, category,
    confidence, supporting_memory_ids.
    """
    from .enhanced_synthesizer import EnhancedMemorySynthesizer

    objects = _memory_objects(rows)
    if not objects:
        return []

    synth = EnhancedMemorySynthesizer()
    candidates: list[dict[str, Any]] = []

    # Recurring category clusters — the base "keeps happening" signal.
    by_category: dict[str, list[dict]] = {}
    for row in rows:
        cat = row.get("category") or "general"
        by_category.setdefault(cat, []).append(row)
    for cat, members in by_category.items():
        ids = [m["id"] for m in members]
        if len(ids) < MIN_CLUSTER_SIZE:
            continue
        candidates.append(
            {
                "pattern_key": f"recurring:{cat}",
                "observation_text": (
                    f"Recurring pattern: {len(ids)} memories share category '{cat}'. "
                    "This theme keeps recurring across sessions."
                ),
                "category": cat,
                "confidence": min(0.9, 0.45 + 0.05 * len(ids)),
                "supporting_memory_ids": ids,
            }
        )

    # Synthesizer-driven detection: contradictions and temporal trends,
    # promoted to evidence-linked observations via generated insights.
    try:
        contradictions = synth._detect_contradictions(objects)
        trends = synth._analyze_temporal_trends(objects)
        insights = synth._generate_insights(objects, contradictions, trends)
    except Exception as exc:
        logger.debug("enhanced pattern detection unavailable: %s", exc)
        return candidates

    for insight in insights:
        if len(insight.evidence_ids) < MIN_CLUSTER_SIZE:
            continue
        candidates.append(
            {
                "pattern_key": f"{insight.insight_type}:{_slug(insight.statement)}",
                "observation_text": insight.statement,
                "category": insight.insight_type,
                "confidence": float(insight.confidence),
                "supporting_memory_ids": list(insight.evidence_ids),
            }
        )
    return candidates


def _ns_clause(agent_id: str | None, project: str | None, prefix: str = "") -> tuple[str, list]:
    """Namespace filter: caller's lane plus the shared 'default' lane."""
    frag, params = "", []
    if agent_id:
        frag += f" AND ({prefix}agent_id = ? OR {prefix}agent_id IS NULL OR {prefix}agent_id = 'default')"
        params.append(agent_id)
    if project:
        frag += f" AND ({prefix}project = ? OR {prefix}project IS NULL OR {prefix}project = 'default')"
        params.append(project)
    return frag, params


def _find_observation(
    pattern_key: str,
    *,
    user_id: str,
    tenant_id: str,
    agent_id: str | None = None,
    project: str | None = None,
) -> dict | None:
    frag, params = _ns_clause(agent_id, project)
    row = _fetch_one(
        "SELECT * FROM observations WHERE tenant_id = ? AND user_id = ? AND pattern_key = ? "
        f"AND status != 'archived'{frag} ORDER BY version DESC LIMIT 1",
        (tenant_id, user_id, pattern_key, *params),
    )
    return _observation_row(row) if row else None


def _upsert_observation(
    candidate: dict[str, Any],
    *,
    user_id: str,
    tenant_id: str,
    agent_id: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Insert or refine an observation. Evidence is unioned, never dropped."""
    now = _now_iso()
    existing = _find_observation(
        candidate["pattern_key"], user_id=user_id, tenant_id=tenant_id, agent_id=agent_id, project=project
    )

    if existing is None:
        obs_id = uuid.uuid4().hex[:16]
        supporting = list(dict.fromkeys(candidate["supporting_memory_ids"]))
        _execute(
            """INSERT INTO observations
            (id, tenant_id, user_id, agent_id, project, pattern_key, observation_text, category,
             confidence, supporting_memory_ids, evidence_count, status,
             first_seen_at, last_refined_at, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 1)""",
            (
                obs_id,
                tenant_id,
                user_id,
                agent_id or "default",
                project or "default",
                candidate["pattern_key"],
                candidate["observation_text"],
                candidate.get("category"),
                candidate["confidence"],
                json.dumps(supporting),
                len(supporting),
                now,
                now,
            ),
        )
        row = _fetch_one("SELECT * FROM observations WHERE id = ?", (obs_id,))
        return _observation_row(row) if row else {"id": obs_id, **candidate}

    # Refine: union evidence, bump version, advance last_refined_at.
    merged_ids = list(dict.fromkeys(existing["supporting_memory_ids"] + candidate["supporting_memory_ids"]))
    new_version = existing["version"] + 1
    new_confidence = max(existing["confidence"], float(candidate["confidence"]))
    _execute(
        """UPDATE observations SET observation_text = ?, category = ?, confidence = ?,
           supporting_memory_ids = ?, evidence_count = ?, status = 'refined',
           last_refined_at = ?, version = ?
           WHERE id = ? AND tenant_id = ? AND user_id = ?""",
        (
            candidate["observation_text"],
            candidate.get("category") or existing.get("category"),
            new_confidence,
            json.dumps(merged_ids),
            len(merged_ids),
            now,
            new_version,
            existing["id"],
            tenant_id,
            user_id,
        ),
    )
    row = _fetch_one(
        "SELECT * FROM observations WHERE id = ? AND tenant_id = ? AND user_id = ?",
        (existing["id"], tenant_id, user_id),
    )
    return _observation_row(row) if row else existing


def curate_observations(
    *,
    user_id: str,
    tenant_id: str,
    category: str | None = None,
    limit: int = 200,
    agent_id: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Scan recent memories and upsert evidence-linked observations.

    This is the observation curation path: it reads the tenant/user's
    recent latest-truth memories (optionally narrowed to one category),
    detects recurring patterns, and refines existing observations rather
    than overwriting them.
    """
    t0 = time.perf_counter()
    sql = (
        "SELECT id, content, category, scope, retention, created_at, tags "
        "FROM memories WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0 AND is_latest = 1"
    )
    params: list[Any] = [user_id, tenant_id]
    ns_frag, ns_params = _ns_clause(agent_id, project)
    sql += ns_frag
    params.extend(ns_params)
    if category:
        sql += " AND category = ?"
        params.append(category)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, int(limit)))

    rows = _fetch_all(sql, params)
    candidates = _detect_candidates(rows)

    created = refined = 0
    observations: list[dict[str, Any]] = []
    for candidate in candidates:
        before = _find_observation(
            candidate["pattern_key"], user_id=user_id, tenant_id=tenant_id, agent_id=agent_id, project=project
        )
        obs = _upsert_observation(candidate, user_id=user_id, tenant_id=tenant_id, agent_id=agent_id, project=project)
        observations.append(obs)
        if before is None:
            created += 1
        else:
            refined += 1

    return {
        "memories_scanned": len(rows),
        "candidates": len(candidates),
        "observations_created": created,
        "observations_refined": refined,
        "observations": observations,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
    }


def _recency_factor(last_refined_at: str | None, now: datetime) -> float:
    if not last_refined_at:
        return 0.5
    try:
        refined = datetime.fromisoformat(str(last_refined_at).replace("Z", "+00:00"))
        if refined.tzinfo is None:
            refined = refined.replace(tzinfo=timezone.utc)
        age_days = max((now - refined).total_seconds() / 86400.0, 0.0)
    except (ValueError, TypeError):
        return 0.5
    return 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)


def list_observations(
    *,
    user_id: str,
    tenant_id: str,
    status: str = "active",
    limit: int = 50,
    agent_id: str | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    """List observations for a scope, ranked by confidence * recency."""
    frag, ns_params = _ns_clause(agent_id, project)
    if status == "active":
        rows = _fetch_all(
            "SELECT * FROM observations WHERE tenant_id = ? AND user_id = ? AND status IN ('active','refined') "
            f"{frag} ORDER BY confidence DESC, last_refined_at DESC LIMIT ?",
            (tenant_id, user_id, *ns_params, limit),
        )
    else:
        rows = _fetch_all(
            "SELECT * FROM observations WHERE tenant_id = ? AND user_id = ? AND status = ? "
            f"{frag} ORDER BY confidence DESC, last_refined_at DESC LIMIT ?",
            (tenant_id, user_id, status, *ns_params, limit),
        )
    observations = [_observation_row(r) for r in rows]
    now = datetime.now(timezone.utc)
    observations.sort(
        key=lambda o: o["confidence"] * _recency_factor(o.get("last_refined_at"), now),
        reverse=True,
    )
    return observations


def archive_observation(observation_id: str, *, user_id: str, tenant_id: str) -> bool:
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE observations SET status = 'archived' WHERE id = ? AND tenant_id = ? AND user_id = ?",
            (observation_id, tenant_id, user_id),
        )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()
