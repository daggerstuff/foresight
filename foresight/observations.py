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
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .enhanced_synthesizer import EnhancedMemorySynthesizer
from .memory_types import MemoryObject
from .reflect import UserScope, _connect, _fetch_all, _fetch_one
from .sql_helpers import join_sql_parts

logger = logging.getLogger("foresight_observations")

MIN_CLUSTER_SIZE = 2
MAX_INJECTION_OBSERVATIONS = 3
_RECENCY_HALF_LIFE_DAYS = 30.0
_MAX_SUMMARY_CHARS = 200
_ARCHIVE_LISTENERS: list[Callable[[str, str, str], Any]] = []


def register_archive_listener(listener: Callable[[str, str, str], Any]) -> None:
    """Register a callback invoked as ``listener(observation_id, user_id, tenant_id)`` on archive."""
    if listener not in _ARCHIVE_LISTENERS:
        _ARCHIVE_LISTENERS.append(listener)


def _execute(sql: str, params: tuple | list = ()) -> None:
    conn = _connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
        "supporting_episode_ids": json.loads(row.get("supporting_episode_ids") or "[]"),
        "memory_type": row.get("memory_type") or "semantic",
        "evidence_count": int(row.get("evidence_count") or 0),
        "status": row.get("status", "active"),
        "first_seen_at": row["first_seen_at"],
        "last_refined_at": row.get("last_refined_at"),
        "version": int(row.get("version") or 1),
    }


def _memory_objects(rows: list[dict]) -> list[Any]:

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

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:length] or "pattern"


_PROCEDURAL_CATEGORIES = frozenset({"procedural", "procedure", "workflow", "recipe"})


def _parse_json_obj(raw: Any, default: Any) -> Any:
    if isinstance(raw, type(default)):
        return raw
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, type(default)) else default
    except (TypeError, ValueError):
        return default


def _procedural_key(row: dict[str, Any]) -> str | None:
    """Derive a stable procedural pattern key for a procedural memory row."""
    if bool(row.get("is_sensitive")):
        return None
    cat = (row.get("category") or "").strip().lower()
    mtype = (row.get("memory_type") or "").strip().lower()
    if mtype != "procedural" and cat not in _PROCEDURAL_CATEGORIES:
        return None

    metrics = _parse_json_obj(row.get("metrics"), {})
    # exp_key is what experience distillation writes; without it every
    # lesson collapses into one shared "procedural:lesson" cluster.
    for key in ("exp_key", "procedure_key", "skill_name", "procedure"):
        val = metrics.get(key)
        if isinstance(val, str) and val.strip():
            return f"procedural:{_slug(val)}"

    tags = _parse_json_obj(row.get("tags"), [])
    for tag in tags:
        if not isinstance(tag, str):
            continue
        low = tag.strip().lower()
        for prefix in ("procedure:", "skill:", "workflow:"):
            if low.startswith(prefix) and len(low) > len(prefix):
                return f"procedural:{_slug(low[len(prefix) :])}"

    if cat and cat not in _PROCEDURAL_CATEGORIES and cat not in ("fact", "general"):
        return f"procedural:{_slug(cat)}"

    content = (row.get("content") or "").strip()
    head = content.splitlines()[0][:64] if content else "procedure"
    return f"procedural:{_slug(head)}"


def _detect_procedural_candidates(rows: list[dict]) -> list[dict[str, Any]]:
    """Cluster procedural memories/episodes into procedural observation candidates."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        pkey = _procedural_key(row)
        if pkey is None:
            continue
        groups.setdefault(pkey, []).append(row)

    candidates: list[dict[str, Any]] = []
    for pkey, members in groups.items():
        ids = [m["id"] for m in members]
        if len(ids) < MIN_CLUSTER_SIZE:
            continue
        first_text = str(members[0].get("content") or "").strip()
        summary = first_text[:_MAX_SUMMARY_CHARS] + ("..." if len(first_text) > _MAX_SUMMARY_CHARS else "")
        candidates.append(
            {
                "pattern_key": pkey,
                "observation_text": f"Procedural pattern ({pkey.split(':', 1)[-1]}): {summary}",
                "category": "procedural",
                "confidence": min(0.95, 0.5 + 0.08 * len(ids)),
                "supporting_memory_ids": ids,
            }
        )
    return candidates


def _detect_candidates(rows: list[dict]) -> list[dict[str, Any]]:
    """Detect recurring patterns via the enhanced synthesizer.

    Returns candidate dicts: pattern_key, observation_text, category,
    confidence, supporting_memory_ids.
    """

    # The synthesizer embeds memory content in its insights, so sensitive rows
    # must never reach it. Category clusters only carry a count and a category
    # name, and procedural clusters already skip sensitive rows.
    objects = _memory_objects([row for row in rows if not bool(row.get("is_sensitive"))])

    synth = EnhancedMemorySynthesizer()
    candidates: list[dict[str, Any]] = []

    # Procedural pattern clusters (never distill is_sensitive rows).
    procedural_candidates = _detect_procedural_candidates(rows)
    candidates.extend(procedural_candidates)
    procedural_ids = {mid for c in procedural_candidates for mid in c["supporting_memory_ids"]}

    # Recurring category clusters — the base "keeps happening" signal.
    by_category: dict[str, list[dict]] = {}
    for row in rows:
        # Already carried by a procedural candidate (memory_type='procedural'
        # rows are often category='lesson'), so a second cluster would duplicate.
        if row["id"] in procedural_ids:
            continue
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
    if not objects:
        return candidates
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
        join_sql_parts(
            "SELECT * FROM observations WHERE tenant_id = ? AND user_id = ? AND pattern_key = ? ",
            "AND status != 'archived'",
            frag,
            " ORDER BY version DESC LIMIT 1",
        ),
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

    supporting_episode_ids = list(dict.fromkeys(candidate.get("supporting_episode_ids") or []))
    memory_type = candidate.get("memory_type") or "semantic"

    if existing is None:
        obs_id = uuid.uuid4().hex[:16]
        supporting = list(dict.fromkeys(candidate["supporting_memory_ids"]))
        _execute(
            """INSERT INTO observations
            (id, tenant_id, user_id, agent_id, project, pattern_key, observation_text, category,
             confidence, supporting_memory_ids, supporting_episode_ids, memory_type, evidence_count, status,
             first_seen_at, last_refined_at, version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 1)""",
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
                json.dumps(supporting_episode_ids),
                memory_type,
                len(supporting) + len(supporting_episode_ids),
                now,
                now,
            ),
        )
        row = _fetch_one("SELECT * FROM observations WHERE id = ?", (obs_id,))
        return _observation_row(row) if row else {"id": obs_id, **candidate}

    # Refine: union evidence, bump version, advance last_refined_at.
    merged_ids = list(dict.fromkeys(existing["supporting_memory_ids"] + candidate["supporting_memory_ids"]))
    merged_episodes = list(dict.fromkeys(existing.get("supporting_episode_ids", []) + supporting_episode_ids))
    new_version = existing["version"] + 1
    new_confidence = max(existing["confidence"], float(candidate["confidence"]))
    memory_type = candidate.get("memory_type") or "semantic"
    prior_memory_type = existing.get("memory_type") or "semantic"
    new_memory_type = memory_type if memory_type != "semantic" or prior_memory_type == "semantic" else prior_memory_type
    _execute(
        """UPDATE observations SET observation_text = ?, category = ?, confidence = ?,
           supporting_memory_ids = ?, supporting_episode_ids = ?, memory_type = ?,
           evidence_count = ?, status = 'refined',
           last_refined_at = ?, version = ?
           WHERE id = ? AND tenant_id = ? AND user_id = ?""",
        (
            candidate["observation_text"],
            candidate.get("category") or existing.get("category"),
            new_confidence,
            json.dumps(merged_ids),
            json.dumps(merged_episodes),
            new_memory_type,
            len(merged_ids) + len(merged_episodes),
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
    scope: UserScope,
    *,
    category: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Scan recent memories and upsert evidence-linked observations.

    This is the observation curation path: it reads the tenant/user's
    recent latest-truth memories (optionally narrowed to one category),
    detects recurring patterns, and refines existing observations rather
    than overwriting them.
    """
    user_id = scope.user_id
    tenant_id = scope.tenant_id
    t0 = time.perf_counter()
    sql = (
        "SELECT id, content, category, scope, retention, created_at, tags, "
        "memory_type, metrics, is_sensitive, is_latest "
        "FROM memories WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0 AND is_latest = 1"
    )
    params: list[Any] = [user_id, tenant_id]
    ns_frag, ns_params = _ns_clause(scope.agent_id, scope.project)
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
            candidate["pattern_key"],
            user_id=user_id,
            tenant_id=tenant_id,
            agent_id=scope.agent_id,
            project=scope.project,
        )
        obs = _upsert_observation(
            candidate, user_id=user_id, tenant_id=tenant_id, agent_id=scope.agent_id, project=scope.project
        )
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
            refined = refined.replace(tzinfo=UTC)
        age_days = max((now - refined).total_seconds() / 86400.0, 0.0)
    except (ValueError, TypeError):
        return 0.5
    return 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)


def list_observations(
    scope: UserScope,
    *,
    status: str = "active",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List observations for a scope, ranked by confidence * recency."""
    user_id = scope.user_id
    tenant_id = scope.tenant_id
    frag, ns_params = _ns_clause(scope.agent_id, scope.project)
    if status == "active":
        rows = _fetch_all(
            join_sql_parts(
                "SELECT * FROM observations WHERE tenant_id = ? AND user_id = ? AND status IN ('active','refined') ",
                frag,
                " ORDER BY confidence DESC, last_refined_at DESC LIMIT ?",
            ),
            (tenant_id, user_id, *ns_params, limit),
        )
    else:
        rows = _fetch_all(
            join_sql_parts(
                "SELECT * FROM observations WHERE tenant_id = ? AND user_id = ? AND status = ? ",
                frag,
                " ORDER BY confidence DESC, last_refined_at DESC LIMIT ?",
            ),
            (tenant_id, user_id, status, *ns_params, limit),
        )
    observations = [_observation_row(r) for r in rows]
    now = datetime.now(UTC)
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
        archived = bool(cur.rowcount)
    finally:
        conn.close()
    if archived:
        for listener in _ARCHIVE_LISTENERS:
            try:
                listener(observation_id, user_id, tenant_id)
            except Exception:
                logger.debug("archive listener failed (non-fatal)", exc_info=True)
    return archived


@dataclass(frozen=True)
class EpisodeObservationSpec:
    pattern_key: str
    observation_text: str
    category: str
    confidence: float
    episode_ids: list[str]
    memory_type: str = "procedural"


def upsert_episode_observation(
    spec: EpisodeObservationSpec,
    *,
    user_id: str,
    tenant_id: str,
) -> dict[str, Any]:
    """Upsert a procedural observation whose evidence is episode IDs.

    Used by the experience-memory distiller: success recipes and failure
    signatures become observations whose ``supporting_episode_ids`` point at
    the episodes that produced them. Evidence is unioned on refine.
    """
    candidate = {
        "pattern_key": spec.pattern_key,
        "observation_text": spec.observation_text,
        "category": spec.category,
        "confidence": spec.confidence,
        "supporting_memory_ids": [],
        "supporting_episode_ids": list(dict.fromkeys(spec.episode_ids)),
        "memory_type": spec.memory_type,
    }
    return _upsert_observation(candidate, user_id=user_id, tenant_id=tenant_id)
