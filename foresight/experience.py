"""Experience memory from task outcomes (FOR-60 / PIX-5039).

Closes the procedural loop: a finished session becomes an *episode* — a row
linked to its raw events — carrying an outcome score and the source of that
score (explicit user feedback, CI/test result, LLM rubric, or a deterministic
fallback). Scored episodes are distilled into procedural observations whose
evidence is the episode ids, and failure signatures are indexed as procedural
memories so the keyword retrieval signal can surface prior failures when a
matching error shows up again.

Scoring order at session end:

1. Explicit outcome (``record_episode_outcome`` tool: user feedback or a
   CI/test result posted by automation) always wins and is never overwritten
   by automatic scoring.
2. ``FORESIGHT_EPISODE_SCORING=llm|auto`` — an LLM rubric scores the
   transcript (``llm_rubric``). Unavailable or failing models fall back.
3. Deterministic fallback (default): pattern-scans the transcript for
   failure/success signals (``fallback``). Always available, offline,
   flagged ``degraded`` in ``score_detail``.

``is_sensitive`` episodes are never distilled.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .error_signatures import normalize_error_signature
from .llm_client import default_llm_call
from .observations import EpisodeObservationSpec, upsert_episode_observation
from .reflect import _connect, _fetch_all, _fetch_one
from .sensitivity import resolve_is_sensitive

logger = logging.getLogger("foresight_experience")

VALID_OUTCOMES = ("success", "failure", "mixed", "unknown")
VALID_SCORE_SOURCES = ("user_feedback", "ci_result", "llm_rubric", "fallback")

_MAX_SUMMARY_CHARS = 200
_MAX_DETAIL_CHARS = 500
_MAX_EVIDENCE_IDS = 50
_SUCCESS_SCORE = 0.6
_FAILURE_SCORE = 0.4
_MIN_SUMMARY_LEN = 8


_FAILURE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"traceback \(most recent call last\)",
        r"\bfailed\b",
        r"\bfailing\b",
        r"\d+ failed\b",
        r"\berror[: ]",
        r"\bexception\b",
        r"\bpanic:",
        r"\bfatal:",
        r"exit(?:ed)?(?: with)?(?: code| status)[:=\s]+[1-9]\d*",
        r"\bassertionerror\b",
        r"\bcompilation failed\b",
        r"\bnon-zero exit\b",
    )
]
_SUCCESS_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\ball tests pass",
        r"\b\d+ passed\b",
        r"\btests? (?:passed|passing|green)\b",
        r"exit(?:ed)?(?: with)?(?: code| status)[:=\s]+0\b",
        r"\bbuild succeeded\b",
        r"\bdeployment (?:succeeded|complete)\b",
        r"✓",
    )
]


def _deterministic_assessment(transcript: str) -> dict[str, Any]:
    failures = [p.pattern for p in _FAILURE_PATTERNS if p.search(transcript)]
    successes = [p.pattern for p in _SUCCESS_PATTERNS if p.search(transcript)]
    if failures and not successes:
        return {"outcome": "failure", "score": 0.15, "failures": len(failures), "successes": 0}
    if successes and not failures:
        return {"outcome": "success", "score": 0.9, "failures": 0, "successes": len(successes)}
    if failures and successes:
        return {"outcome": "mixed", "score": 0.5, "failures": len(failures), "successes": len(successes)}
    return {"outcome": "unknown", "score": 0.5, "failures": 0, "successes": 0}


def _llm_scoring_enabled() -> str:
    return os.environ.get("FORESIGHT_EPISODE_SCORING", "fallback").strip().lower()


def _llm_rubric(transcript: str, tenant_id: str, user_id: str) -> tuple[float, str]:
    prompt = (
        "You are scoring a finished work session. Rubric:\n"
        "1.0 = the task was completed as asked and verified.\n"
        "0.5 = partial progress, mixed result, or unclear outcome.\n"
        "0.0 = failed, abandoned, or the error is unresolved.\n"
        "Reply with exactly one number between 0 and 1, then a one-sentence rationale.\n\n"
        f"Transcript (tail):\n{transcript[-4000:]}\n"
    )
    raw = default_llm_call(prompt, tenant_id, user_id)
    match = re.search(r"\b(0(?:\.\d+)?|1(?:\.0+)?)\b", raw)
    if not match:
        raise ValueError(f"LLM rubric response carried no score: {raw[:120]!r}")
    score = max(0.0, min(1.0, float(match.group(1))))
    return score, raw.strip()[:300]


def _score_to_outcome(score: float) -> str:
    if score >= _SUCCESS_SCORE:
        return "success"
    if score <= _FAILURE_SCORE:
        return "failure"
    return "mixed"


@dataclass(frozen=True)
class EpisodeVerdict:
    outcome: str
    score: float | None = None
    score_source: str = "user_feedback"
    detail: str | None = None
    episode_id: str | None = None
    session_id: str | None = None


def _episode_id(tenant_id: str, user_id: str, session_id: str) -> str:
    return hashlib.sha256(f"{tenant_id}|{user_id}|{session_id}".encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _episode_row(row: dict[str, Any]) -> dict[str, Any]:
    score = row.get("outcome_score")
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "user_id": row["user_id"],
        "session_id": row["session_id"],
        "agent_id": row.get("agent_id"),
        "project": row.get("project"),
        "outcome": row.get("outcome") or "unknown",
        "outcome_score": float(score) if score is not None else None,
        "resolved": bool(row.get("resolved")),
        "score_source": row.get("score_source"),
        "score_detail": row.get("score_detail"),
        "summary": row.get("summary") or "",
        "error_signature": row.get("error_signature"),
        "is_sensitive": bool(row.get("is_sensitive")),
        "raw_event_first_id": row.get("raw_event_first_id"),
        "raw_event_last_id": row.get("raw_event_last_id"),
        "raw_event_first_pos": row.get("raw_event_first_pos"),
        "raw_event_last_pos": row.get("raw_event_last_pos"),
        "raw_event_count": int(row.get("raw_event_count") or 0),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def get_episode(
    episode_id: str | None = None,
    *,
    session_id: str | None = None,
    user_id: str,
    tenant_id: str,
) -> dict[str, Any] | None:
    if episode_id:
        row = _fetch_one(
            "SELECT * FROM episodes WHERE id = ? AND tenant_id = ? AND user_id = ?",
            (episode_id, tenant_id, user_id),
        )
    elif session_id:
        row = _fetch_one(
            "SELECT * FROM episodes WHERE tenant_id = ? AND user_id = ? AND session_id = ?",
            (tenant_id, user_id, session_id),
        )
    else:
        return None
    return _episode_row(row) if row else None


def _task_summary(messages: list[dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("role") == "user":
            text = re.sub(r"\s+", " ", str(msg.get("content") or "")).strip()
            if text:
                return text[:_MAX_SUMMARY_CHARS]
    return ""


def _messages_to_text(messages: list[dict[str, Any]]) -> str:
    parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = str(msg.get("content") or "").strip()
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def _raw_events_for_session(tenant_id: str, user_id: str, session_id: str) -> list[dict[str, Any]]:
    return _fetch_all(
        """SELECT id, role, content, position FROM raw_events
           WHERE tenant_id = ? AND user_id = ? AND session_id = ? AND tombstone = 0
           ORDER BY position ASC, captured_at ASC""",
        (tenant_id, user_id, session_id),
    )


def build_episode(
    session_id: str,
    *,
    user_id: str,
    tenant_id: str,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create or refresh the episode for ``session_id``.

    Idempotent on (tenant_id, user_id, session_id): re-running refreshes the
    raw-event range and task summary but never clobbers an already-recorded
    outcome/score.
    """
    if not session_id:
        raise ValueError("session_id is required to build an episode")

    raw_events = _raw_events_for_session(tenant_id, user_id, session_id)
    if messages is None:
        messages = [{"role": row.get("role") or "unknown", "content": row.get("content") or ""} for row in raw_events]

    summary = _task_summary(messages)
    sensitivity_text = "\n".join(str(m.get("content") or "") for m in messages)[:20000]
    is_sensitive = 1 if (sensitivity_text and resolve_is_sensitive(None, sensitivity_text)[0]) else 0

    first = raw_events[0] if raw_events else None
    last = raw_events[-1] if raw_events else None
    episode_id = _episode_id(tenant_id, user_id, session_id)
    now = _now_iso()

    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO episodes (
                   id, tenant_id, user_id, session_id, outcome,
                   summary, is_sensitive,
                   raw_event_first_id, raw_event_last_id, raw_event_first_pos, raw_event_last_pos,
                   raw_event_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'unknown', ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                   summary = excluded.summary,
                   is_sensitive = CASE WHEN episodes.is_sensitive = 1 OR excluded.is_sensitive = 1 THEN 1 ELSE 0 END,
                   raw_event_first_id = excluded.raw_event_first_id,
                   raw_event_last_id = excluded.raw_event_last_id,
                   raw_event_first_pos = excluded.raw_event_first_pos,
                   raw_event_last_pos = excluded.raw_event_last_pos,
                   raw_event_count = excluded.raw_event_count,
                   updated_at = excluded.updated_at""",
            (
                episode_id,
                tenant_id,
                user_id,
                session_id,
                summary,
                is_sensitive,
                first["id"] if first else None,
                last["id"] if last else None,
                first["position"] if first else None,
                last["position"] if last else None,
                len(raw_events),
                now,
                now,
            ),
        )
        conn.execute("DELETE FROM episode_events WHERE episode_id = ?", (episode_id,))
        for row in raw_events:
            conn.execute(
                "INSERT INTO episode_events (episode_id, raw_event_id, position) VALUES (?, ?, ?) "
                "ON CONFLICT (episode_id, raw_event_id) DO NOTHING",
                (episode_id, row["id"], row.get("position") or 0),
            )
        conn.commit()
    finally:
        conn.close()

    episode = get_episode(episode_id, user_id=user_id, tenant_id=tenant_id)
    if episode is None:
        raise RuntimeError(f"episode {episode_id!r} missing after upsert")
    return episode


def _clamp_score(score: float) -> float:
    return max(0.0, min(1.0, float(score)))


def record_outcome(user_id: str, tenant_id: str, verdict: EpisodeVerdict) -> dict[str, Any]:
    """Explicitly score an episode (user feedback or CI/test result)."""
    outcome = (verdict.outcome or "").strip().lower()
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(VALID_OUTCOMES)}")
    if verdict.score_source not in VALID_SCORE_SOURCES:
        raise ValueError(f"score_source must be one of {', '.join(VALID_SCORE_SOURCES)}")

    episode = get_episode(verdict.episode_id, session_id=verdict.session_id, user_id=user_id, tenant_id=tenant_id)
    if episode is None:
        if not verdict.session_id:
            raise LookupError("no episode for episode_id and no session_id to build one from")
        episode = build_episode(verdict.session_id, user_id=user_id, tenant_id=tenant_id)

    score = verdict.score
    if score is None:
        score = {"success": 1.0, "failure": 0.0, "mixed": 0.5, "unknown": 0.5}[outcome]
    score = _clamp_score(score)

    signature = normalize_error_signature(verdict.detail) if verdict.detail else None
    detail_text = (verdict.detail or "")[:_MAX_DETAIL_CHARS] or None

    conn = _connect()
    try:
        conn.execute(
            """UPDATE episodes SET outcome = ?, outcome_score = ?, score_source = ?, score_detail = ?,
                   error_signature = COALESCE(?, error_signature), resolved = ?, updated_at = ?
               WHERE id = ? AND tenant_id = ? AND user_id = ?""",
            (
                outcome,
                score,
                verdict.score_source,
                detail_text,
                signature,
                1 if outcome == "success" else int(bool(episode.get("resolved"))),
                _now_iso(),
                episode["id"],
                tenant_id,
                user_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    updated = get_episode(episode["id"], user_id=user_id, tenant_id=tenant_id)
    if updated is None:
        raise RuntimeError(f"episode {episode['id']!r} missing after outcome update")
    return updated


def auto_score_episode(
    episode_id: str,
    *,
    user_id: str,
    tenant_id: str,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Score an episode that has no explicit score yet.

    Explicit outcomes (user_feedback / ci_result) win and are kept. Otherwise
    the LLM rubric runs when ``FORESIGHT_EPISODE_SCORING=llm|auto`` and the
    provider is reachable; any failure degrades to the deterministic
    transcript scan, flagged ``fallback`` + degraded in ``score_detail``.
    """
    episode = get_episode(episode_id, user_id=user_id, tenant_id=tenant_id)
    if episode is None:
        return None
    if episode["score_source"] in ("user_feedback", "ci_result"):
        return episode

    if messages is not None:
        transcript = _messages_to_text(messages)
    else:
        rows = _fetch_all(
            """SELECT role, content FROM raw_events
               WHERE tenant_id = ? AND user_id = ? AND session_id = ? AND tombstone = 0
               ORDER BY position ASC, captured_at ASC""",
            (tenant_id, user_id, episode["session_id"]),
        )
        transcript = "\n".join(f"{r.get('role') or 'unknown'}: {r.get('content') or ''}" for r in rows)

    mode = _llm_scoring_enabled()
    detail: str
    if mode in ("llm", "auto") and transcript:
        try:
            score, rationale = _llm_rubric(transcript, tenant_id, user_id)
            source = "llm_rubric"
            outcome = _score_to_outcome(score)
            detail = f"llm_rubric: {rationale}"[:_MAX_DETAIL_CHARS]
        except Exception as exc:
            logger.info("LLM rubric unavailable, using deterministic fallback: %s", exc)
            assessment = _deterministic_assessment(transcript)
            score, outcome = assessment["score"], assessment["outcome"]
            source = "fallback"
            detail = (
                f"degraded deterministic fallback after LLM failure ({type(exc).__name__}): "
                f"failure_signals={assessment['failures']} success_signals={assessment['successes']}"
            )[:_MAX_DETAIL_CHARS]
    else:
        assessment = _deterministic_assessment(transcript)
        score, outcome = assessment["score"], assessment["outcome"]
        source = "fallback"
        detail = (
            f"degraded deterministic fallback (no LLM rubric): "
            f"failure_signals={assessment['failures']} success_signals={assessment['successes']}"
        )[:_MAX_DETAIL_CHARS]

    signature = normalize_error_signature(transcript)

    conn = _connect()
    try:
        conn.execute(
            """UPDATE episodes SET outcome = ?, outcome_score = ?, score_source = ?, score_detail = ?,
                   error_signature = COALESCE(?, error_signature), resolved = ?, updated_at = ?
               WHERE id = ? AND tenant_id = ? AND user_id = ?""",
            (
                outcome,
                _clamp_score(score),
                source,
                detail,
                signature,
                1 if outcome == "success" else int(bool(episode.get("resolved"))),
                _now_iso(),
                episode["id"],
                tenant_id,
                user_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_episode(episode_id, user_id=user_id, tenant_id=tenant_id)


def _slug(text: str, length: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:length] or "task"


def _experience_memory_id(tenant_id: str, user_id: str, key: str) -> str:
    return hashlib.sha256(f"experience|{tenant_id}|{user_id}|{key}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ProceduralMemorySpec:
    key: str
    content: str
    episode_ids: list[str]
    error_signature: str | None = None
    importance: float = 0.7


def _upsert_procedural_memory(
    spec: ProceduralMemorySpec,
    *,
    user_id: str,
    tenant_id: str,
) -> tuple[str, bool]:
    """Insert or refresh the retrieval-facing procedural memory for ``key``.

    Returns (memory_id, created). The row carries ``memory_type='procedural'``
    and, for failures, ``error_signature`` — that is the token the keyword
    signal matches on when a matching error later appears in a session.
    """
    memory_id = _experience_memory_id(tenant_id, user_id, spec.key)
    now = _now_iso()
    content_hash = hashlib.sha256(spec.content.encode()).hexdigest()
    metrics = json.dumps(
        {
            "exp_key": spec.key,
            "episode_ids": spec.episode_ids[-_MAX_EVIDENCE_IDS:],
            "occurrences": len(spec.episode_ids),
        }
    )

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id FROM memories WHERE id = ? AND tenant_id = ? AND user_id = ?",
            (memory_id, tenant_id, user_id),
        ).fetchone()
        if row is not None:
            conn.execute(
                """UPDATE memories SET content = ?, content_hash = ?, importance = ?,
                       metrics = ?, error_signature = ?, updated_at = ?
                   WHERE id = ? AND tenant_id = ? AND user_id = ?""",
                (
                    spec.content,
                    content_hash,
                    spec.importance,
                    metrics,
                    spec.error_signature,
                    now,
                    memory_id,
                    tenant_id,
                    user_id,
                ),
            )
            created = False
        else:
            conn.execute(
                """INSERT INTO memories
                   (id, content, content_hash, scope, retention, category, user_id, bank_id, tenant_id,
                    created_at, updated_at, tags, emotional_context, metrics,
                    is_ghost, synthesized_from, importance, memory_type, is_sensitive, error_signature)
                   VALUES (?, ?, ?, 'arc', 'long_term', 'lesson', ?, 'default', ?,
                           ?, ?, ?, '{}', ?, 0, '[]', ?, 'procedural', 0, ?)""",
                (
                    memory_id,
                    spec.content,
                    content_hash,
                    user_id,
                    tenant_id,
                    now,
                    now,
                    json.dumps(
                        ["procedural", "experience", "failure-signature" if spec.error_signature else "success-recipe"]
                    ),
                    metrics,
                    spec.importance,
                    spec.error_signature,
                ),
            )
            created = True
        conn.commit()
        return memory_id, created
    finally:
        conn.close()


def _episode_side(episode: dict[str, Any]) -> str | None:
    outcome = episode.get("outcome")
    score = episode.get("outcome_score")
    signature = episode.get("error_signature")
    if outcome == "success" or (
        score is not None and score >= _SUCCESS_SCORE and not signature and outcome != "failure"
    ):
        return "success"
    if outcome == "failure" or (score is not None and score <= _FAILURE_SCORE):
        return "failure"
    if signature and outcome != "success":
        return "failure"
    return None


def distill_experiences(
    *,
    user_id: str,
    tenant_id: str,
    limit: int = 200,
) -> dict[str, Any]:
    """Distill scored, non-sensitive episodes into procedural knowledge.

    Successes become reusable recipes keyed by task summary; failures become
    signature-keyed lessons. Both land as procedural observations whose
    evidence is the episode ids, and as procedural memories (failures carry
    ``error_signature``) so the keyword signal surfaces them later.

    ``limit`` is the fetch batch size, not a ceiling: every eligible episode is
    distilled. A single ``ORDER BY created_at ASC LIMIT`` re-read the oldest
    batch forever once a user crossed it, so newer episodes never distilled.
    """
    batch_size = max(1, int(limit))
    sql = (
        "SELECT * FROM episodes WHERE tenant_id = ? AND user_id = ? AND score_source IS NOT NULL "
        "AND is_sensitive = 0 AND outcome != 'unknown' "
        "AND (created_at > ? OR (created_at = ? AND id > ?)) "
        "ORDER BY created_at ASC, id ASC LIMIT ?"
    )
    episodes: list[dict[str, Any]] = []
    cursor_created, cursor_id = "", ""
    while True:
        batch = [
            _episode_row(r)
            for r in _fetch_all(
                sql,
                (tenant_id, user_id, cursor_created, cursor_created, cursor_id, batch_size),
            )
        ]
        episodes.extend(batch)
        if len(batch) < batch_size:
            break
        cursor_created = str(batch[-1]["created_at"] or "")
        cursor_id = batch[-1]["id"]

    failures: dict[str, list[dict[str, Any]]] = {}
    successes: dict[str, list[dict[str, Any]]] = {}
    for ep in episodes:
        side = _episode_side(ep)
        if side == "failure":
            key = ep.get("error_signature") or f"task:{_slug(ep.get('summary') or 'unspecified')}"
            failures.setdefault(key, []).append(ep)
        elif side == "success":
            summary = (ep.get("summary") or "").strip()
            if len(summary) < _MIN_SUMMARY_LEN:
                continue
            successes.setdefault(_slug(summary), []).append(ep)

    result = {
        "episodes_scanned": len(episodes),
        "skipped_sensitive": 0,
        "failures_distilled": 0,
        "successes_distilled": 0,
        "observations_upserted": 0,
        "procedural_memories_written": 0,
    }
    skipped = _fetch_one(
        "SELECT COUNT(*) AS n FROM episodes "
        "WHERE tenant_id = ? AND user_id = ? AND is_sensitive = 1 AND score_source IS NOT NULL",
        (tenant_id, user_id),
    )
    result["skipped_sensitive"] = int((skipped or {}).get("n") or 0)

    _distill_failure_group(failures, user_id=user_id, tenant_id=tenant_id, result=result)
    _distill_success_group(successes, user_id=user_id, tenant_id=tenant_id, result=result)
    return result


def _distill_failure_group(
    failures: dict[str, list[dict[str, Any]]],
    *,
    user_id: str,
    tenant_id: str,
    result: dict[str, Any],
) -> None:
    for sig, group in failures.items():
        ids = [ep["id"] for ep in group]
        sample = next((ep for ep in reversed(group) if ep.get("summary")), group[-1])
        sample_summary = sample.get("summary") or "unspecified task"
        n = len(group)
        confidence = min(0.95, 0.5 + 0.1 * n)
        is_signature = not sig.startswith("task:")
        label = sig if is_signature else sample_summary
        text = (
            f'[procedural] Known failure - signature `{label}`: task like "{sample_summary}" '
            f"failed {n} time(s) with this signature. Verify the failing step against `{label}` "
            f"before retrying the same approach."
        )
        upsert_episode_observation(
            EpisodeObservationSpec(
                pattern_key=f"failure:{sig}"[:200],
                observation_text=text,
                category="procedural",
                confidence=confidence,
                episode_ids=ids,
            ),
            user_id=user_id,
            tenant_id=tenant_id,
        )
        content = (
            f"[procedural lesson] Error signature: {label}\n"
            f"Context: {sample_summary}\n"
            f"This approach failed {n} time(s). When `{label}` appears, stop and check the failing "
            f"step instead of retrying the same fix. Evidence: {n} prior episode(s)."
        )
        _upsert_procedural_memory(
            ProceduralMemorySpec(
                key=f"failure:{sig}",
                content=content,
                episode_ids=ids,
                error_signature=sig if is_signature else None,
                importance=0.8,
            ),
            user_id=user_id,
            tenant_id=tenant_id,
        )
        result["failures_distilled"] += 1
        result["observations_upserted"] += 1
        result["procedural_memories_written"] += 1


def _distill_success_group(
    successes: dict[str, list[dict[str, Any]]],
    *,
    user_id: str,
    tenant_id: str,
    result: dict[str, Any],
) -> None:
    for slug, group in successes.items():
        ids = [ep["id"] for ep in group]
        summary = group[-1].get("summary") or slug
        n = len(group)
        avg_score = sum((ep.get("outcome_score") or 0.5) for ep in group) / n
        confidence = min(0.95, 0.5 + 0.1 * n)
        text = (
            f'[procedural] Reusable recipe - "{summary}": succeeded {n} time(s) '
            f"(avg score {avg_score:.2f}). When this task recurs, reuse the approach that worked."
        )
        upsert_episode_observation(
            EpisodeObservationSpec(
                pattern_key=f"success:{slug}",
                observation_text=text,
                category="procedural",
                confidence=confidence,
                episode_ids=ids,
            ),
            user_id=user_id,
            tenant_id=tenant_id,
        )
        content = (
            f"[procedural recipe] Task: {summary}\n"
            f"This approach succeeded {n} time(s) with average score {avg_score:.2f}. "
            f"Reuse it when the task recurs instead of improvising from scratch."
        )
        _upsert_procedural_memory(
            ProceduralMemorySpec(
                key=f"success:{slug}",
                content=content,
                episode_ids=ids,
                importance=0.7,
            ),
            user_id=user_id,
            tenant_id=tenant_id,
        )
        result["successes_distilled"] += 1
        result["observations_upserted"] += 1
        result["procedural_memories_written"] += 1


def capture_session_outcome(
    session_id: str,
    *,
    user_id: str,
    tenant_id: str,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Session-end hook: build the episode, auto-score it, distil experiences."""
    episode = build_episode(session_id, user_id=user_id, tenant_id=tenant_id, messages=messages)
    scored = auto_score_episode(episode["id"], user_id=user_id, tenant_id=tenant_id, messages=messages) or episode
    distillation = distill_experiences(user_id=user_id, tenant_id=tenant_id)
    return {"episode": scored, "distillation": distillation}
