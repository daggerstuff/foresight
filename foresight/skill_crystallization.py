"""Skill crystallization — render proven procedural observations into signed SKILL.md artifacts.

A crystallized skill is a rendered artifact stored as a ``memories`` row
(``category = 'skill'``, ``memory_type = 'procedural'``), versioned in
``memory_versions``, HMAC-signed with the ``profile_export`` key, and staged in
the ``manage_inferred`` review queue (``inferred = 1``) until a human approves
it. Only approved, non-retired, signature-verified skills are loadable by agents
or exported to a ``skills/``-compatible directory layout.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .context_cache import get_context_cache
from .encryption import decrypt_if_encrypted, encrypt_if_enabled
from .hybrid_retriever import get_hybrid_retriever
from .observations import _ns_clause, list_observations, register_archive_listener
from .profile_export import SKILL_SCHEMA_VERSION, sign_skill_document, verify_skill_document
from .reflect import UserScope, _connect
from .sql_helpers import join_sql_parts, placeholders

logger = logging.getLogger("foresight_skill_crystallization")

DEFAULT_MIN_SUCCESS_EPISODES = 3
MIN_SUCCESS_ENV_VAR = "FORESIGHT_SKILL_MIN_SUCCESSES"
_OUTCOME_SUCCESS_SCORE_THRESHOLD = 0.5

SKILL_CATEGORY = "skill"
SKILL_MEMORY_TYPE = "procedural"
SKILL_REVIEW_RETIRED = "retired"

_SUCCESS_OUTCOMES = frozenset({"success", "succeeded", "pass", "passed", "ok"})
_FAILURE_OUTCOMES = frozenset({"failure", "failed", "fail", "error", "contradicted"})
_SUCCESS_TAGS = frozenset({"outcome:success", "success", "passed", "episode:success"})
_FAILURE_TAGS = frozenset({"outcome:failure", "failure", "failed", "error", "episode:failure"})
_PROCEDURAL_CATEGORIES = frozenset({"procedural", "procedure", "workflow", "recipe"})


@dataclass(frozen=True)
class SkillProofEvaluation:
    """Result of evaluating whether an observation crosses the skill proof threshold."""

    eligible: bool
    is_procedural: bool
    success_count: int
    failure_count: int
    unresolved_failure_count: int
    superseded_ids: list[str]
    has_sensitive: bool
    evidence_ids: list[str]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "is_procedural": self.is_procedural,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "unresolved_failure_count": self.unresolved_failure_count,
            "superseded_ids": list(self.superseded_ids),
            "has_sensitive": self.has_sensitive,
            "evidence_ids": list(self.evidence_ids),
            "reason": self.reason,
        }


def min_success_threshold(override: int | None = None) -> int:
    """Return the minimum successful episodes required to crystallize a skill."""
    if override is not None:
        return max(1, int(override))
    raw = os.environ.get(MIN_SUCCESS_ENV_VAR, "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed >= 1:
                return parsed
        except ValueError:
            logger.warning("Invalid %s=%r; using default %d", MIN_SUCCESS_ENV_VAR, raw, DEFAULT_MIN_SUCCESS_EPISODES)
    return DEFAULT_MIN_SUCCESS_EPISODES


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _skill_slug(text: str, length: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:length] or "procedural-skill"


def _parse_json_value(raw: Any, default: Any) -> Any:
    if isinstance(raw, type(default)):
        return raw
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, type(default)) else default
    except (TypeError, ValueError):
        return default


def _episodes_columns(conn: Any) -> set[str]:
    """Portable column lookup for the optional ``episodes`` table (FOR-60)."""
    try:
        rows = conn.execute("PRAGMA table_info(episodes)").fetchall()
        return {str(dict(r)["name"]) for r in rows if dict(r).get("name")}
    except Exception:
        return set()


def _is_expired(expires_at: Any, now_iso: str) -> bool:
    if not expires_at:
        return False
    return str(expires_at) < now_iso


def _episode_outcome_kind(row: dict[str, Any]) -> tuple[str | None, bool]:
    """Classify an ``episodes`` row as ('success' | 'failure' | None, resolved)."""
    resolved = bool(row.get("resolved"))
    outcome = str(row.get("outcome") or "").strip().lower()
    if outcome in _SUCCESS_OUTCOMES:
        return "success", True
    if outcome in _FAILURE_OUTCOMES:
        return "failure", resolved

    score = row.get("outcome_score")
    if score is not None:
        try:
            numeric = float(score)
        except (TypeError, ValueError):
            numeric = None
        if numeric is not None:
            return ("success", True) if numeric >= _OUTCOME_SUCCESS_SCORE_THRESHOLD else ("failure", resolved)
    return None, resolved


def _memory_outcome_kind(row: dict[str, Any]) -> tuple[str | None, bool]:
    """Classify a ``memories`` row's outcome from metrics/tags."""
    metrics = _parse_json_value(row.get("metrics"), {})
    tags = {str(t).strip().lower() for t in _parse_json_value(row.get("tags"), []) if isinstance(t, str)}
    resolved = bool(metrics.get("resolved")) or "resolved" in tags

    outcome = str(metrics.get("outcome") or "").strip().lower()
    if outcome in _SUCCESS_OUTCOMES or tags & _SUCCESS_TAGS:
        return "success", True
    if outcome in _FAILURE_OUTCOMES or tags & _FAILURE_TAGS:
        return "failure", resolved

    for score_key in ("outcome_score", "score"):
        raw_score = metrics.get(score_key)
        if raw_score is not None:
            try:
                numeric = float(raw_score)
            except (TypeError, ValueError):
                numeric = None
            if numeric is not None:
                return ("success", True) if numeric >= _OUTCOME_SUCCESS_SCORE_THRESHOLD else ("failure", resolved)

    return None, resolved


def _fetch_episodes_by_ids(conn: Any, ids: list[str], *, user_id: str, tenant_id: str) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    cols = _episodes_columns(conn)
    if not cols or "id" not in cols:
        return {}
    slots = placeholders(len(ids))
    clauses = [f"id IN ({slots})"]
    params: list[Any] = list(ids)
    if "tenant_id" in cols:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if "user_id" in cols:
        clauses.append("user_id = ?")
        params.append(user_id)
    rows = conn.execute(
        join_sql_parts("SELECT * FROM episodes WHERE ", " AND ".join(clauses)),
        tuple(params),
    ).fetchall()
    return {dict(r)["id"]: dict(r) for r in rows}


def _fetch_memories_by_ids(conn: Any, ids: list[str], *, user_id: str, tenant_id: str) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    slots = placeholders(len(ids))
    rows = conn.execute(
        join_sql_parts(
            "SELECT id, content, category, memory_type, tags, metrics, is_ghost, is_latest, "
            "superseded_by, is_sensitive, expired_at, expires_at, created_at "
            "FROM memories WHERE tenant_id = ? AND user_id = ? AND id IN (",
            slots,
            ")",
        ),
        (tenant_id, user_id, *ids),
    ).fetchall()
    return {dict(r)["id"]: dict(r) for r in rows}


def _active_contradictions(conn: Any, ids: list[str], *, user_id: str, tenant_id: str) -> tuple[set[str], set[str]]:
    """Return (contradicted_target_ids, updated_target_ids) from live relationship sources."""
    if not ids:
        return set(), set()
    slots = placeholders(len(ids))
    rows = conn.execute(
        join_sql_parts(
            "SELECT r.relationship_type, r.target_memory_id "
            "FROM memory_relationships r "
            "JOIN memories src ON src.id = r.source_memory_id "
            "WHERE r.tenant_id = ? AND r.user_id = ? "
            "AND r.relationship_type IN ('contradicts', 'updates') "
            "AND r.target_memory_id IN (",
            slots,
            ") AND src.is_ghost = 0 AND src.is_latest = 1 AND src.expired_at IS NULL",
        ),
        (tenant_id, user_id, *ids),
    ).fetchall()
    contradicted: set[str] = set()
    updated: set[str] = set()
    for r in rows:
        rd = dict(r)
        if rd["relationship_type"] == "contradicts":
            contradicted.add(rd["target_memory_id"])
        elif rd["relationship_type"] == "updates":
            updated.add(rd["target_memory_id"])
    return contradicted, updated


@dataclass
class _EvidenceTally:
    success_count: int = 0
    failure_count: int = 0
    unresolved_failure_count: int = 0
    superseded_ids: list[str] | None = None
    has_sensitive: bool = False

    def __post_init__(self) -> None:
        if self.superseded_ids is None:
            self.superseded_ids = []


def _record_outcome_tally(tally: _EvidenceTally, kind: str | None, resolved: bool) -> None:
    if kind == "success":
        tally.success_count += 1
    elif kind == "failure":
        tally.failure_count += 1
        if not resolved:
            tally.unresolved_failure_count += 1


def _is_inactive_memory(mem: dict[str, Any], eid: str, updated_ids: set[str], now_iso: str) -> bool:
    is_latest = int(mem.get("is_latest") if mem.get("is_latest") is not None else 1)
    return (
        bool(mem.get("is_ghost"))
        or mem.get("expired_at") is not None
        or _is_expired(mem.get("expires_at"), now_iso)
        or is_latest == 0
        or mem.get("superseded_by") is not None
        or eid in updated_ids
    )


def _tally_evidence(
    evidence_ids: list[str],
    episodes: dict[str, dict[str, Any]],
    memories: dict[str, dict[str, Any]],
    contradicted_ids: set[str],
    updated_ids: set[str],
) -> _EvidenceTally:
    tally = _EvidenceTally()
    now_iso = _now_iso()
    for eid in evidence_ids:
        ep = episodes.get(eid)
        if ep is not None:
            if bool(ep.get("is_sensitive")):
                tally.has_sensitive = True
            else:
                _record_outcome_tally(tally, *_episode_outcome_kind(ep))
            continue

        mem = memories.get(eid)
        if mem is None or _is_inactive_memory(mem, eid, updated_ids, now_iso):
            if mem is not None and bool(mem.get("is_sensitive")):
                tally.has_sensitive = True
            elif tally.superseded_ids is not None:
                tally.superseded_ids.append(eid)
            continue

        if bool(mem.get("is_sensitive")):
            tally.has_sensitive = True
        elif eid in contradicted_ids:
            _record_outcome_tally(tally, "failure", False)
        else:
            _record_outcome_tally(tally, *_memory_outcome_kind(mem))
    return tally


def _proof_decision(
    tally: _EvidenceTally,
    *,
    is_procedural: bool,
    threshold: int,
) -> tuple[bool, str]:
    superseded = tally.superseded_ids or []
    if not is_procedural:
        return False, "observation is not procedural"
    if tally.has_sensitive:
        return False, "contains sensitive evidence"
    if superseded:
        return False, f"evidence superseded/unavailable ({len(superseded)})"
    if tally.unresolved_failure_count > 0:
        return False, f"has {tally.unresolved_failure_count} unresolved contradicting failure(s)"
    if tally.success_count < threshold:
        return False, f"insufficient proof ({tally.success_count}/{threshold} successful episodes)"
    return True, f"eligible ({tally.success_count} successful episodes, 0 unresolved failures)"


def evaluate_observation_proof(
    conn: Any,
    obs: dict[str, Any],
    *,
    user_id: str,
    tenant_id: str,
    min_successes: int | None = None,
) -> SkillProofEvaluation:
    """Evaluate whether a procedural observation meets the crystallization threshold."""
    threshold = min_success_threshold(min_successes)
    raw_ids = [
        str(eid)
        for eid in ([*(obs.get("supporting_memory_ids") or []), *(obs.get("supporting_episode_ids") or [])])
        if eid
    ]
    evidence_ids = list(dict.fromkeys(raw_ids))

    category = str(obs.get("category") or "").strip().lower()
    memory_type = str(obs.get("memory_type") or "").strip().lower()
    pattern_key = str(obs.get("pattern_key") or "").strip().lower()
    is_procedural = (
        category in _PROCEDURAL_CATEGORIES
        or memory_type == "procedural"
        or pattern_key.startswith("procedural:")
    )

    if obs.get("status") not in ("active", "refined"):
        return SkillProofEvaluation(
            eligible=False,
            is_procedural=is_procedural,
            success_count=0,
            failure_count=0,
            unresolved_failure_count=0,
            superseded_ids=[],
            has_sensitive=False,
            evidence_ids=evidence_ids,
            reason=f"observation status is {obs.get('status')!r}",
        )

    episodes = _fetch_episodes_by_ids(conn, evidence_ids, user_id=user_id, tenant_id=tenant_id)
    memories = _fetch_memories_by_ids(conn, evidence_ids, user_id=user_id, tenant_id=tenant_id)
    contradicted_ids, updated_ids = _active_contradictions(conn, evidence_ids, user_id=user_id, tenant_id=tenant_id)

    if not is_procedural:
        is_procedural = any(
            str(m.get("memory_type") or "").strip().lower() == "procedural"
            or str(m.get("category") or "").strip().lower() in _PROCEDURAL_CATEGORIES
            for m in memories.values()
        )

    tally = _tally_evidence(evidence_ids, episodes, memories, contradicted_ids, updated_ids)
    eligible, reason = _proof_decision(tally, is_procedural=is_procedural, threshold=threshold)

    return SkillProofEvaluation(
        eligible=eligible,
        is_procedural=is_procedural,
        success_count=tally.success_count,
        failure_count=tally.failure_count,
        unresolved_failure_count=tally.unresolved_failure_count,
        superseded_ids=tally.superseded_ids or [],
        has_sensitive=tally.has_sensitive,
        evidence_ids=evidence_ids,
        reason=reason,
    )


def _derive_skill_name(obs: dict[str, Any]) -> str:
    pkey = str(obs.get("pattern_key") or "").strip()
    if ":" in pkey:
        _, raw_name = pkey.split(":", 1)
    else:
        raw_name = pkey or str(obs.get("observation_text") or "")
    return _skill_slug(raw_name)


def _existing_skill_by_observation(
    conn: Any,
    observation_id: str,
    pattern_key: str,
    *,
    user_id: str,
    tenant_id: str,
) -> dict[str, Any] | None:
    rows = conn.execute(
        "SELECT * FROM memories WHERE tenant_id = ? AND user_id = ? AND category = ? "
        "AND is_ghost = 0 ORDER BY updated_at DESC, created_at DESC LIMIT 100",
        (tenant_id, user_id, SKILL_CATEGORY),
    ).fetchall()
    for row in rows:
        rd = dict(row)
        metrics = _parse_json_value(rd.get("metrics"), {})
        if metrics.get("observation_id") == observation_id or (
            pattern_key and metrics.get("pattern_key") == pattern_key
        ):
            return rd
    return None


def select_skill_candidates(
    scope: UserScope,
    *,
    min_successes: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return procedural observations that cross the proof threshold for skill crystallization."""
    observations = list_observations(scope, status="active", limit=max(1, int(limit)))
    if not observations:
        return []

    conn = _connect()
    try:
        candidates: list[dict[str, Any]] = []
        for obs in observations:
            proof = evaluate_observation_proof(
                conn,
                obs,
                user_id=scope.user_id,
                tenant_id=scope.tenant_id,
                min_successes=min_successes,
            )
            if not proof.eligible:
                continue
            skill_name = _derive_skill_name(obs)
            existing = _existing_skill_by_observation(
                conn,
                obs["id"],
                obs.get("pattern_key", ""),
                user_id=scope.user_id,
                tenant_id=scope.tenant_id,
            )
            existing_meta = _parse_json_value(existing.get("metrics"), {}) if existing else {}
            existing_obs_ver = int(existing_meta.get("observation_version") or 0) if existing else 0
            existing_evidence = sorted(str(x) for x in (existing_meta.get("evidence_ids") or [])) if existing else []
            needs_update = existing is None or (
                existing.get("review_status") == SKILL_REVIEW_RETIRED
                or existing_obs_ver != int(obs.get("version") or 1)
                or existing_evidence != sorted(proof.evidence_ids)
            )
            candidates.append(
                {
                    "observation_id": obs["id"],
                    "pattern_key": obs.get("pattern_key", ""),
                    "observation_text": obs.get("observation_text", ""),
                    "observation_version": int(obs.get("version") or 1),
                    "confidence": float(obs.get("confidence") or 0.5),
                    "skill_name": skill_name,
                    "success_count": proof.success_count,
                    "failure_count": proof.failure_count,
                    "evidence_ids": proof.evidence_ids,
                    "already_staged": existing is not None,
                    "existing_skill_id": existing["id"] if existing else None,
                    "existing_review_status": existing.get("review_status") if existing else None,
                    "needs_update": needs_update,
                    "reason": proof.reason,
                }
            )
        candidates.sort(key=lambda c: (c["success_count"], c["confidence"]), reverse=True)
        return candidates
    finally:
        conn.close()


def _collect_procedure_steps(
    conn: Any,
    evidence_ids: list[str],
    *,
    user_id: str,
    tenant_id: str,
) -> list[str]:
    memories = _fetch_memories_by_ids(conn, evidence_ids, user_id=user_id, tenant_id=tenant_id)
    episodes = _fetch_episodes_by_ids(conn, evidence_ids, user_id=user_id, tenant_id=tenant_id)
    steps: list[str] = []
    seen: set[str] = set()
    for eid in evidence_ids:
        text = ""
        if eid in memories:
            text = decrypt_if_encrypted(
                str(memories[eid].get("content") or ""),
                tenant_id=tenant_id,
                user_id=user_id,
            ).strip()
        elif eid in episodes:
            text = str(episodes[eid].get("summary") or episodes[eid].get("procedure") or "").strip()
        if not text:
            continue
        snippet = text[:280]
        if snippet not in seen:
            seen.add(snippet)
            steps.append(snippet)
    return steps


@dataclass(frozen=True)
class SkillRenderSpec:
    """Bundled parameters for rendering and signing a SKILL.md document."""

    scope: UserScope
    memory_id: str
    skill_version: int = 1
    steps: list[str] | None = None
    retired: bool = False
    retired_reason: str | None = None


def _canonical_skill_payload(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SKILL_SCHEMA_VERSION,
        "skill_name": str(fields["skill_name"]),
        "description": str(fields["description"]),
        "skill_version": int(fields["skill_version"]),
        "tenant_id": str(fields["tenant_id"]),
        "user_id": str(fields["user_id"]),
        "observation_id": str(fields["observation_id"]),
        "pattern_key": str(fields["pattern_key"]),
        "evidence_ids": sorted(str(eid) for eid in fields["evidence_ids"]),
        "success_count": int(fields["success_count"]),
        "failure_count": int(fields["failure_count"]),
        "confidence": round(float(fields["confidence"]), 4),
        "retired": bool(fields["retired"]),
        "instructions": str(fields["instructions"]).strip(),
    }


def render_skill_markdown(
    candidate: dict[str, Any],
    spec: SkillRenderSpec | None = None,
    **legacy_kwargs: Any,
) -> tuple[str, str, dict[str, Any]]:
    """Render a skill candidate into a signed SKILL.md document and metadata dict."""
    if spec is None:
        spec = SkillRenderSpec(
            scope=legacy_kwargs["scope"],
            memory_id=legacy_kwargs["memory_id"],
            skill_version=int(legacy_kwargs.get("skill_version", 1)),
            steps=legacy_kwargs.get("steps"),
            retired=bool(legacy_kwargs.get("retired", False)),
            retired_reason=legacy_kwargs.get("retired_reason"),
        )
    scope = spec.scope
    memory_id = spec.memory_id
    skill_version = spec.skill_version
    retired = spec.retired
    retired_reason = spec.retired_reason

    skill_name = _skill_slug(str(candidate.get("skill_name") or _derive_skill_name(candidate)))
    obs_text = str(candidate.get("observation_text") or skill_name).strip()
    success_count = int(candidate.get("success_count") or 0)
    description = (
        f"Crystallized procedural skill from {success_count} verified successes: {obs_text[:140]}"
    )
    evidence_ids = sorted(str(eid) for eid in (candidate.get("evidence_ids") or []))
    confidence = round(float(candidate.get("confidence") or 0.5), 4)
    failure_count = int(candidate.get("failure_count") or 0)
    observation_id = str(candidate.get("observation_id") or "")
    pattern_key = str(candidate.get("pattern_key") or "")

    title = skill_name.replace("-", " ").title()
    body_lines = [
        f"# {title}",
        "",
    ]
    if retired:
        body_lines.extend(
            [
                f"> **RETIRED:** {retired_reason or 'Underlying evidence superseded.'}",
                "",
            ]
        )
    body_lines.extend(
        [
            "## When to apply",
            "",
            obs_text,
            "",
            "## Procedure",
            "",
        ]
    )
    procedure_steps = [s for s in (spec.steps or []) if s]
    if procedure_steps:
        for idx, step in enumerate(procedure_steps, start=1):
            body_lines.append(f"{idx}. {step}")
    else:
        body_lines.append(f"1. Follow verified pattern `{pattern_key}` ({obs_text}).")
    body_lines.extend(
        [
            "",
            "## Provenance & Evidence",
            "",
            f"- Observation: `{observation_id}` (pattern `{pattern_key}`)",
            f"- Verified successes: {success_count} (unresolved failures: {failure_count})",
            f"- Evidence IDs: {', '.join(evidence_ids) if evidence_ids else 'none'}",
        ]
    )
    instructions = "\n".join(body_lines).strip()

    signing_doc = _canonical_skill_payload(
        {
            "skill_name": skill_name,
            "description": description,
            "skill_version": skill_version,
            "tenant_id": scope.tenant_id,
            "user_id": scope.user_id,
            "observation_id": observation_id,
            "pattern_key": pattern_key,
            "evidence_ids": evidence_ids,
            "success_count": success_count,
            "failure_count": failure_count,
            "confidence": confidence,
            "retired": retired,
            "instructions": instructions,
        }
    )
    signature = sign_skill_document(signing_doc)

    frontmatter_lines = [
        "---",
        f"name: {json.dumps(skill_name)}",
        f"description: {json.dumps(description)}",
        "license: MIT",
        "metadata:",
        "  foresight_skill: true",
        f"  schema_version: {SKILL_SCHEMA_VERSION}",
        f"  skill_version: {int(skill_version)}",
        f"  memory_id: {json.dumps(memory_id)}",
        f"  observation_id: {json.dumps(observation_id)}",
        f"  pattern_key: {json.dumps(pattern_key)}",
        f"  tenant_id: {json.dumps(scope.tenant_id)}",
        f"  user_id: {json.dumps(scope.user_id)}",
        "  evidence_ids:",
    ]
    for eid in evidence_ids:
        frontmatter_lines.append(f"    - {json.dumps(eid)}")
    frontmatter_lines.extend(
        [
            f"  success_count: {success_count}",
            f"  failure_count: {failure_count}",
            f"  confidence: {confidence}",
            f"  retired: {'true' if retired else 'false'}",
            '  signature_alg: "HMAC-SHA256"',
            f"  signature: {json.dumps(signature)}",
            "---",
            "",
            instructions,
            "",
        ]
    )
    skill_md = "\n".join(frontmatter_lines)

    metadata: dict[str, Any] = {
        "foresight_skill": True,
        "schema_version": SKILL_SCHEMA_VERSION,
        "skill_name": skill_name,
        "description": description,
        "skill_version": int(skill_version),
        "memory_id": memory_id,
        "observation_id": observation_id,
        "observation_version": int(candidate.get("observation_version") or 1),
        "pattern_key": pattern_key,
        "evidence_ids": evidence_ids,
        "success_count": success_count,
        "failure_count": failure_count,
        "confidence": confidence,
        "retired": bool(retired),
        "retired_reason": retired_reason,
        "signature_alg": "HMAC-SHA256",
        "signature": signature,
    }
    return skill_md, signature, metadata


def _parse_scalar(raw: str) -> Any:
    val = raw.strip()
    result: Any = val
    if val in ("true", "True"):
        result = True
    elif val in ("false", "False"):
        result = False
    elif (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        if val.startswith('"'):
            try:
                result = json.loads(val)
            except ValueError:
                result = val[1:-1]
        else:
            result = val[1:-1]
    else:
        try:
            result = float(val) if "." in val else int(val)
        except ValueError:
            result = val
    return result


def parse_skill_markdown(skill_md: str) -> dict[str, Any]:
    """Parse a rendered SKILL.md string into frontmatter, metadata, and instructions."""
    text = (skill_md or "").lstrip("\ufeff")
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md missing YAML frontmatter start")
    end_idx = text.find("\n---\n", 4)
    if end_idx < 0:
        raise ValueError("SKILL.md missing YAML frontmatter end")

    header = text[4:end_idx]
    body = text[end_idx + 5 :].strip()

    top: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    in_metadata = False
    in_evidence = False
    evidence_ids: list[str] = []

    for line in header.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if in_evidence and line.startswith("    - "):
            evidence_ids.append(str(_parse_scalar(line[6:])))
            continue
        in_evidence = False
        if line.startswith("  ") and in_metadata:
            key, _, val = line.strip().partition(":")
            key = key.strip()
            if key == "evidence_ids" and not val.strip():
                in_evidence = True
                continue
            meta[key] = _parse_scalar(val)
        else:
            in_metadata = False
            key, _, val = line.partition(":")
            key = key.strip()
            if key == "metadata" and not val.strip():
                in_metadata = True
                continue
            top[key] = _parse_scalar(val)

    meta["evidence_ids"] = evidence_ids
    return {
        "name": str(top.get("name") or ""),
        "description": str(top.get("description") or ""),
        "license": str(top.get("license") or "MIT"),
        "metadata": meta,
        "instructions": body,
    }


def verify_skill_markdown(skill_md: str) -> tuple[bool, dict[str, Any]]:
    """Verify the HMAC-SHA256 signature embedded in a rendered SKILL.md document."""
    try:
        parsed = parse_skill_markdown(skill_md)
    except ValueError as exc:
        return False, {"error": str(exc)}

    meta = parsed["metadata"]
    signature = str(meta.get("signature") or "")
    if not signature:
        return False, parsed

    try:
        signing_doc = _canonical_skill_payload(
            {
                "skill_name": parsed["name"],
                "description": parsed["description"],
                "skill_version": int(meta.get("skill_version") or 1),
                "tenant_id": str(meta.get("tenant_id") or ""),
                "user_id": str(meta.get("user_id") or ""),
                "observation_id": str(meta.get("observation_id") or ""),
                "pattern_key": str(meta.get("pattern_key") or ""),
                "evidence_ids": list(meta.get("evidence_ids") or []),
                "success_count": int(meta.get("success_count") or 0),
                "failure_count": int(meta.get("failure_count") or 0),
                "confidence": float(meta.get("confidence") or 0.0),
                "retired": bool(meta.get("retired", False)),
                "instructions": parsed["instructions"],
            }
        )
    except (TypeError, ValueError):
        return False, parsed

    return verify_skill_document(signing_doc, signature), parsed


def _record_skill_version(conn: Any, snapshot: dict[str, Any]) -> str:
    memory_id = str(snapshot["memory_id"])
    version = int(snapshot["version"])
    created_at = str(snapshot["created_at"])
    version_id = hashlib.sha256(f"{memory_id}:{version}:{created_at}".encode()).hexdigest()[:16]
    conn.execute(
        """INSERT INTO memory_versions
        (id, memory_id, tenant_id, content, version, created_at, tags, emotional_context, metrics, rollback_of)
        VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, NULL)""",
        (
            version_id,
            memory_id,
            str(snapshot["tenant_id"]),
            str(snapshot["content"]),
            version,
            created_at,
            json.dumps(snapshot["tags"]),
            json.dumps(snapshot["metadata"]),
        ),
    )
    return version_id


def _invalidate_caches(user_id: str, tenant_id: str) -> None:
    try:
        get_hybrid_retriever().invalidate_caches(user_id, tenant_id)
        get_context_cache().invalidate_user(user_id)
    except Exception:
        logger.debug("skill cache invalidation failed (non-fatal)", exc_info=True)


def crystallize_skills(
    scope: UserScope,
    *,
    min_successes: int | None = None,
    bank_id: str = "default",
    limit: int = 50,
) -> dict[str, Any]:
    """Evaluate procedural observations and stage qualifying SKILL.md artifacts for review."""
    retired = retire_stale_skills(scope)
    candidates = select_skill_candidates(scope, min_successes=min_successes, limit=limit)
    staged: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    unchanged: list[str] = []

    if not candidates:
        return {
            "candidates": [],
            "staged_count": 0,
            "updated_count": 0,
            "unchanged_count": 0,
            "retired_count": len(retired),
            "skills": [],
            "retired_skills": retired,
        }

    conn = _connect()
    try:
        now = _now_iso()
        for cand in candidates:
            existing_id = cand.get("existing_skill_id")
            if existing_id and not cand.get("needs_update"):
                unchanged.append(existing_id)
                continue

            steps = _collect_procedure_steps(
                conn,
                cand["evidence_ids"],
                user_id=scope.user_id,
                tenant_id=scope.tenant_id,
            )

            if existing_id is None:
                memory_id = f"skill_{uuid.uuid4().hex[:12]}"
                skill_version = 1
            else:
                row = conn.execute(
                    "SELECT version FROM memories WHERE id = ? AND tenant_id = ? AND user_id = ?",
                    (existing_id, scope.tenant_id, scope.user_id),
                ).fetchone()
                old_ver = int(dict(row)["version"] or 1) if row else 1
                memory_id = existing_id
                skill_version = old_ver + 1

            skill_md, signature, metadata = render_skill_markdown(
                cand,
                scope=scope,
                memory_id=memory_id,
                skill_version=skill_version,
                steps=steps,
            )
            tags = [
                "skill",
                "procedural",
                f"skill:{metadata['skill_name']}",
                f"obs:{cand['observation_id']}",
            ]
            stored_content = encrypt_if_enabled(
                skill_md,
                is_sensitive=False,
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
            )
            content_hash = hashlib.sha256(skill_md.encode("utf-8")).hexdigest()

            if existing_id is None:
                conn.execute(
                    """INSERT INTO memories
                    (id, content, tenant_id, scope, retention, category, user_id, bank_id,
                     created_at, updated_at, tags, emotional_context, metrics, is_ghost,
                     synthesized_from, version, importance, is_latest, inferred, review_status,
                     review_reason, memory_type, agent_id, project, content_hash, valid_from)
                    VALUES (?, ?, ?, 'arc', 'long_term', ?, ?, ?, ?, ?, ?, '{}', ?, 0,
                            ?, ?, 0.85, 1, 1, NULL, NULL, ?, ?, ?, ?, ?)""",
                    (
                        memory_id,
                        stored_content,
                        scope.tenant_id,
                        SKILL_CATEGORY,
                        scope.user_id,
                        bank_id,
                        now,
                        now,
                        json.dumps(tags),
                        json.dumps(metadata),
                        json.dumps(cand["evidence_ids"]),
                        skill_version,
                        SKILL_MEMORY_TYPE,
                        scope.agent_id or "default",
                        scope.project or "default",
                        content_hash,
                        now,
                    ),
                )
                version_id = _record_skill_version(
                    conn,
                    {
                        "memory_id": memory_id,
                        "tenant_id": scope.tenant_id,
                        "content": stored_content,
                        "version": skill_version,
                        "tags": tags,
                        "metadata": metadata,
                        "created_at": now,
                    },
                )
                staged.append(
                    {
                        "memory_id": memory_id,
                        "skill_name": metadata["skill_name"],
                        "version": skill_version,
                        "version_id": version_id,
                        "signature": signature,
                        "status": "pending",
                    }
                )
            else:
                conn.execute(
                    """UPDATE memories
                    SET content = ?, updated_at = ?, tags = ?, metrics = ?, synthesized_from = ?,
                        version = ?, is_latest = 1, is_ghost = 0, inferred = 1,
                        review_status = NULL, review_reason = NULL, content_hash = ?,
                        valid_until = NULL
                    WHERE id = ? AND tenant_id = ? AND user_id = ?""",
                    (
                        stored_content,
                        now,
                        json.dumps(tags),
                        json.dumps(metadata),
                        json.dumps(cand["evidence_ids"]),
                        skill_version,
                        content_hash,
                        memory_id,
                        scope.tenant_id,
                        scope.user_id,
                    ),
                )
                version_id = _record_skill_version(
                    conn,
                    {
                        "memory_id": memory_id,
                        "tenant_id": scope.tenant_id,
                        "content": stored_content,
                        "version": skill_version,
                        "tags": tags,
                        "metadata": metadata,
                        "created_at": now,
                    },
                )
                updated.append(
                    {
                        "memory_id": memory_id,
                        "skill_name": metadata["skill_name"],
                        "version": skill_version,
                        "version_id": version_id,
                        "signature": signature,
                        "status": "pending",
                    }
                )

        conn.commit()
    finally:
        conn.close()

    if staged or updated:
        _invalidate_caches(scope.user_id, scope.tenant_id)

    return {
        "candidates": candidates,
        "staged_count": len(staged),
        "updated_count": len(updated),
        "unchanged_count": len(unchanged),
        "retired_count": len(retired),
        "skills": [*staged, *updated],
        "retired_skills": retired,
    }


def _skill_status(row: dict[str, Any]) -> str:
    if row.get("review_status") == SKILL_REVIEW_RETIRED:
        return "retired"
    if row.get("review_status") == "declined" or bool(row.get("is_ghost")):
        return "declined"
    is_approved = (
        int(row.get("inferred") or 0) == 0
        and row.get("review_status") == "approved"
        and int(row.get("is_latest") or 0) == 1
    )
    return "approved" if is_approved else "pending"


def _skill_record(row: dict[str, Any], *, verify_signatures: bool = True) -> dict[str, Any]:
    content = decrypt_if_encrypted(
        str(row.get("content") or ""),
        tenant_id=str(row.get("tenant_id") or "default"),
        user_id=str(row.get("user_id") or "default"),
    )
    meta = _parse_json_value(row.get("metrics"), {})
    sig_valid = False
    parsed: dict[str, Any] = {}
    if verify_signatures:
        sig_valid, parsed = verify_skill_markdown(content)
    else:
        try:
            parsed = parse_skill_markdown(content)
            sig_valid = bool(meta.get("signature"))
        except ValueError:
            parsed = {}
    skill_name = str(meta.get("skill_name") or parsed.get("name") or row["id"])
    return {
        "memory_id": row["id"],
        "skill_name": skill_name,
        "description": str(meta.get("description") or parsed.get("description") or ""),
        "status": _skill_status(row),
        "review_status": row.get("review_status"),
        "review_reason": row.get("review_reason"),
        "version": int(row.get("version") or meta.get("skill_version") or 1),
        "observation_id": meta.get("observation_id"),
        "pattern_key": meta.get("pattern_key"),
        "evidence_ids": meta.get("evidence_ids") or _parse_json_value(row.get("synthesized_from"), []),
        "success_count": int(meta.get("success_count") or 0),
        "failure_count": int(meta.get("failure_count") or 0),
        "confidence": float(meta.get("confidence") or 0.0),
        "signature": meta.get("signature"),
        "signature_alg": meta.get("signature_alg") or "HMAC-SHA256",
        "signature_valid": sig_valid,
        "retired": _skill_status(row) == "retired",
        "retired_reason": meta.get("retired_reason") or row.get("review_reason"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "content": content,
    }


def list_skills(
    scope: UserScope,
    *,
    status: str = "approved",
    limit: int = 50,
    verify_signatures: bool = True,
    reconcile: bool = True,
) -> list[dict[str, Any]]:
    """List crystallized skills for a scope.

    When ``status == 'approved'`` (the agent/plugin loading path), only
    approved, non-retired, non-ghost, latest, non-expired skills whose HMAC
    signature verifies cleanly are returned.
    """
    if reconcile:
        try:
            retire_stale_skills(scope)
        except Exception:
            logger.debug("skill reconcile skipped (non-fatal)", exc_info=True)

    frag, ns_params = _ns_clause(scope.agent_id, scope.project)
    conn = _connect()
    try:
        if status == "approved":
            sql = join_sql_parts(
                "SELECT * FROM memories WHERE tenant_id = ? AND user_id = ? AND category = ? ",
                "AND inferred = 0 AND review_status = 'approved' AND is_ghost = 0 AND is_latest = 1 ",
                "AND expired_at IS NULL ",
                frag,
                " ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            )
            params: list[Any] = [scope.tenant_id, scope.user_id, SKILL_CATEGORY, *ns_params, max(1, int(limit))]
        else:
            sql = join_sql_parts(
                "SELECT * FROM memories WHERE tenant_id = ? AND user_id = ? AND category = ? ",
                frag,
                " ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            )
            params = [scope.tenant_id, scope.user_id, SKILL_CATEGORY, *ns_params, max(1, int(limit)) * 4]
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()

    now_iso = _now_iso()
    records: list[dict[str, Any]] = []
    for row in rows:
        rd = dict(row)
        if status == "approved" and _is_expired(rd.get("expires_at"), now_iso):
            continue
        rec = _skill_record(rd, verify_signatures=verify_signatures)
        if status == "approved":
            if rec["status"] != "approved" or (verify_signatures and not rec["signature_valid"]):
                continue
        elif status != "all" and rec["status"] != status:
            continue
        records.append(rec)
        if len(records) >= max(1, int(limit)):
            break
    return records


def load_skill(skill_name_or_id: str, scope: UserScope) -> dict[str, Any] | None:
    """Load a single approved, signature-verified skill by ID or slug name."""
    target = skill_name_or_id.strip()
    if not target:
        return None
    for skill in list_skills(scope, status="approved", limit=100, verify_signatures=True):
        if skill["memory_id"] == target or skill["skill_name"] == target:
            return skill
    return None


def is_skill_memory_loadable(row: dict[str, Any]) -> bool:
    """Return True if a memories row is either not a skill or is an approved, valid skill."""
    if str(row.get("category") or "").strip().lower() != SKILL_CATEGORY:
        return True
    if _skill_status(row) != "approved":
        return False
    content = decrypt_if_encrypted(
        str(row.get("content") or ""),
        tenant_id=str(row.get("tenant_id") or "default"),
        user_id=str(row.get("user_id") or "default"),
    )
    ok, _ = verify_skill_markdown(content)
    return ok


def export_skills_to_dir(
    scope: UserScope,
    output_dir: str | Path,
    *,
    skill_name: str | None = None,
    include_unapproved: bool = False,
) -> dict[str, Any]:
    """Export rendered SKILL.md packages into a ``skills/``-compatible directory layout."""
    base_dir = Path(output_dir)
    status = "all" if include_unapproved else "approved"
    skills = list_skills(scope, status=status, limit=200, verify_signatures=True)
    if skill_name:
        target = skill_name.strip()
        skills = [s for s in skills if s["skill_name"] == target or s["memory_id"] == target]

    exported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for skill in skills:
        if not include_unapproved and skill["status"] != "approved":
            skipped.append({"memory_id": skill["memory_id"], "reason": f"status={skill['status']}"})
            continue
        if not skill["signature_valid"]:
            skipped.append({"memory_id": skill["memory_id"], "reason": "invalid_signature"})
            continue
        slug = _skill_slug(skill["skill_name"])
        dest_dir = base_dir / slug
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / "SKILL.md"
        dest_file.write_text(skill["content"], encoding="utf-8")
        exported.append(
            {
                "skill_name": slug,
                "memory_id": skill["memory_id"],
                "version": skill["version"],
                "status": skill["status"],
                "path": str(dest_file),
            }
        )

    return {
        "ok": True,
        "output_dir": str(base_dir),
        "exported_count": len(exported),
        "skipped_count": len(skipped),
        "exported": exported,
        "skipped": skipped,
    }


def _retire_skill_row(
    conn: Any,
    row: dict[str, Any],
    *,
    reason: str,
    superseded_evidence_ids: list[str],
    now: str,
) -> dict[str, Any]:
    meta = _parse_json_value(row.get("metrics"), {})
    old_version = int(row.get("version") or meta.get("skill_version") or 1)
    new_version = old_version + 1
    evidence_ids = [str(x) for x in (meta.get("evidence_ids") or _parse_json_value(row.get("synthesized_from"), []))]
    scope = UserScope(
        user_id=str(row["user_id"]),
        tenant_id=str(row["tenant_id"]),
        agent_id=row.get("agent_id"),
        project=row.get("project"),
    )
    old_content = decrypt_if_encrypted(
        str(row.get("content") or ""),
        tenant_id=scope.tenant_id,
        user_id=scope.user_id,
    )
    try:
        parsed = parse_skill_markdown(old_content)
        existing_obs_text = parsed.get("description", "")
    except ValueError:
        existing_obs_text = str(meta.get("description") or meta.get("skill_name") or row["id"])

    candidate = {
        "observation_id": meta.get("observation_id") or "",
        "observation_version": meta.get("observation_version") or 1,
        "pattern_key": meta.get("pattern_key") or "",
        "observation_text": existing_obs_text,
        "skill_name": meta.get("skill_name") or row["id"],
        "evidence_ids": evidence_ids,
        "success_count": int(meta.get("success_count") or 0),
        "failure_count": int(meta.get("failure_count") or 0),
        "confidence": float(meta.get("confidence") or 0.5),
    }
    retired_md, signature, new_meta = render_skill_markdown(
        candidate,
        scope=scope,
        memory_id=row["id"],
        skill_version=new_version,
        retired=True,
        retired_reason=reason,
    )
    new_meta["retired_at"] = now
    new_meta["superseded_evidence_ids"] = sorted(set(superseded_evidence_ids))

    tags = _parse_json_value(row.get("tags"), [])
    if "retired" not in tags:
        tags.append("retired")
    stored_content = encrypt_if_enabled(
        retired_md,
        is_sensitive=False,
        tenant_id=scope.tenant_id,
        user_id=scope.user_id,
    )
    review_reason = f"Retired: {reason}"
    conn.execute(
        """UPDATE memories
        SET content = ?, updated_at = ?, tags = ?, metrics = ?, version = ?,
            is_latest = 0, inferred = 1, review_status = ?, review_reason = ?,
            valid_until = ?, content_hash = ?
        WHERE id = ? AND tenant_id = ? AND user_id = ?""",
        (
            stored_content,
            now,
            json.dumps(tags),
            json.dumps(new_meta),
            new_version,
            SKILL_REVIEW_RETIRED,
            review_reason,
            now,
            hashlib.sha256(retired_md.encode("utf-8")).hexdigest(),
            row["id"],
            scope.tenant_id,
            scope.user_id,
        ),
    )
    _record_skill_version(
        conn,
        {
            "memory_id": row["id"],
            "tenant_id": scope.tenant_id,
            "content": stored_content,
            "version": new_version,
            "tags": tags,
            "metadata": new_meta,
            "created_at": now,
        },
    )
    return {
        "memory_id": row["id"],
        "skill_name": new_meta["skill_name"],
        "version": new_version,
        "review_status": SKILL_REVIEW_RETIRED,
        "review_reason": review_reason,
        "signature": signature,
        "superseded_evidence_ids": sorted(set(superseded_evidence_ids)),
    }


def retire_skills_for_superseded_evidence(
    superseded_ids: list[str],
    *,
    user_id: str,
    tenant_id: str,
    reason: str = "underlying evidence superseded",
) -> list[dict[str, Any]]:
    """Auto-retire any active/staged skills whose evidence overlaps ``superseded_ids``."""
    target_ids = {str(x) for x in superseded_ids if x}
    if not target_ids:
        return []

    conn = _connect()
    retired: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT * FROM memories WHERE tenant_id = ? AND user_id = ? AND category = ? "
            "AND is_ghost = 0 AND (review_status IS NULL OR review_status NOT IN ('retired', 'declined'))",
            (tenant_id, user_id, SKILL_CATEGORY),
        ).fetchall()
        now = _now_iso()
        for row in rows:
            rd = dict(row)
            meta = _parse_json_value(rd.get("metrics"), {})
            evidence = {str(x) for x in (meta.get("evidence_ids") or _parse_json_value(rd.get("synthesized_from"), []))}
            overlap = sorted(evidence & target_ids)
            if not overlap:
                continue
            retired.append(
                _retire_skill_row(
                    conn,
                    rd,
                    reason=f"{reason} ({', '.join(overlap[:5])})",
                    superseded_evidence_ids=overlap,
                    now=now,
                )
            )
        if retired:
            conn.commit()
    finally:
        conn.close()

    if retired:
        _invalidate_caches(user_id, tenant_id)
    return retired


def retire_skills_for_observation(
    observation_id: str,
    *,
    user_id: str,
    tenant_id: str,
    reason: str = "underlying observation superseded or archived",
) -> list[dict[str, Any]]:
    """Auto-retire any skills derived from ``observation_id``."""
    if not observation_id:
        return []
    conn = _connect()
    retired: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT * FROM memories WHERE tenant_id = ? AND user_id = ? AND category = ? "
            "AND is_ghost = 0 AND (review_status IS NULL OR review_status NOT IN ('retired', 'declined'))",
            (tenant_id, user_id, SKILL_CATEGORY),
        ).fetchall()
        now = _now_iso()
        for row in rows:
            rd = dict(row)
            meta = _parse_json_value(rd.get("metrics"), {})
            if meta.get("observation_id") != observation_id:
                continue
            evidence = [str(x) for x in (meta.get("evidence_ids") or _parse_json_value(rd.get("synthesized_from"), []))]
            retired.append(
                _retire_skill_row(
                    conn,
                    rd,
                    reason=reason,
                    superseded_evidence_ids=evidence,
                    now=now,
                )
            )
        if retired:
            conn.commit()
    finally:
        conn.close()

    if retired:
        _invalidate_caches(user_id, tenant_id)
    return retired


def retire_stale_skills(scope: UserScope) -> list[dict[str, Any]]:
    """Re-verify evidence for all non-retired skills in ``scope`` and retire broken ones."""
    frag, ns_params = _ns_clause(scope.agent_id, scope.project)
    conn = _connect()
    retired: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            join_sql_parts(
                "SELECT * FROM memories WHERE tenant_id = ? AND user_id = ? AND category = ? ",
                "AND is_ghost = 0 AND (review_status IS NULL OR review_status NOT IN ('retired', 'declined')) ",
                frag,
            ),
            (scope.tenant_id, scope.user_id, SKILL_CATEGORY, *ns_params),
        ).fetchall()
        if not rows:
            return []

        now = _now_iso()
        for row in rows:
            rd = dict(row)
            meta = _parse_json_value(rd.get("metrics"), {})
            obs_id = str(meta.get("observation_id") or "")
            evidence_ids = [
                str(x) for x in (meta.get("evidence_ids") or _parse_json_value(rd.get("synthesized_from"), []))
            ]

            obs_row = None
            if obs_id:
                fetched_obs = conn.execute(
                    "SELECT * FROM observations WHERE id = ? AND tenant_id = ? AND user_id = ?",
                    (obs_id, scope.tenant_id, scope.user_id),
                ).fetchone()
                obs_row = dict(fetched_obs) if fetched_obs else None

            if obs_id and (obs_row is None or obs_row.get("status") == "archived"):
                retired.append(
                    _retire_skill_row(
                        conn,
                        rd,
                        reason=f"observation {obs_id} archived or missing",
                        superseded_evidence_ids=evidence_ids,
                        now=now,
                    )
                )
                continue

            synthetic_obs = {
                "id": obs_id,
                "category": "procedural",
                "pattern_key": meta.get("pattern_key") or "procedural:skill",
                "status": obs_row.get("status") if obs_row else "active",
                "supporting_memory_ids": evidence_ids,
            }
            proof = evaluate_observation_proof(
                conn,
                synthetic_obs,
                user_id=scope.user_id,
                tenant_id=scope.tenant_id,
                min_successes=1,
            )
            if proof.superseded_ids or proof.unresolved_failure_count > 0 or proof.has_sensitive:
                reason_bits: list[str] = []
                if proof.superseded_ids:
                    reason_bits.append(f"superseded evidence: {', '.join(proof.superseded_ids[:5])}")
                if proof.unresolved_failure_count > 0:
                    reason_bits.append(f"{proof.unresolved_failure_count} unresolved contradicting failure(s)")
                if proof.has_sensitive:
                    reason_bits.append("sensitive evidence detected")
                retired.append(
                    _retire_skill_row(
                        conn,
                        rd,
                        reason="; ".join(reason_bits),
                        superseded_evidence_ids=proof.superseded_ids or evidence_ids,
                        now=now,
                    )
                )

        if retired:
            conn.commit()
    finally:
        conn.close()

    if retired:
        _invalidate_caches(scope.user_id, scope.tenant_id)
    return retired


def _on_observation_archived(observation_id: str, user_id: str, tenant_id: str) -> None:
    retire_skills_for_observation(
        observation_id,
        user_id=user_id,
        tenant_id=tenant_id,
        reason=f"observation {observation_id} archived",
    )


register_archive_listener(_on_observation_archived)


__all__ = [
    "DEFAULT_MIN_SUCCESS_EPISODES",
    "MIN_SUCCESS_ENV_VAR",
    "SKILL_CATEGORY",
    "SKILL_MEMORY_TYPE",
    "SKILL_REVIEW_RETIRED",
    "SkillProofEvaluation",
    "crystallize_skills",
    "evaluate_observation_proof",
    "export_skills_to_dir",
    "is_skill_memory_loadable",
    "list_skills",
    "load_skill",
    "min_success_threshold",
    "parse_skill_markdown",
    "render_skill_markdown",
    "retire_skills_for_observation",
    "retire_skills_for_superseded_evidence",
    "retire_stale_skills",
    "select_skill_candidates",
    "verify_skill_markdown",
]
