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
from .hybrid_retriever import get_hybrid_retriever
from .llm_client import default_llm_call
from .observations import EpisodeObservationSpec, archive_observation, upsert_episode_observation
from .reflect import _connect, _fetch_all, _fetch_one
from .sensitivity import evaluate_secrets, resolve_is_sensitive

logger = logging.getLogger("foresight_experience")

VALID_OUTCOMES = ("success", "failure", "mixed", "unknown")
VALID_SCORE_SOURCES = ("user_feedback", "ci_result", "llm_rubric", "fallback")

_MAX_SUMMARY_CHARS = 200
_MAX_DETAIL_CHARS = 500
_MAX_EVIDENCE_IDS = 50
_SUCCESS_SCORE = 0.6
_FAILURE_SCORE = 0.4
_MIN_SUMMARY_LEN = 8
# Words that must follow a result report before it counts as an explanation.
_MIN_REPORT_TAIL_WORDS = 3
_MIN_INSTRUCTION_WORDS = 2

# Words that only qualify a run result. A tail made of nothing else - "without
# errors", "no warnings" - reports the run again, so it is not a recipe step.
_STATUS_QUALIFIER_WORDS = frozenset(
    {
        "any",
        "conflict",
        "conflicts",
        "error",
        "errors",
        "exception",
        "exceptions",
        "failure",
        "failures",
        "issue",
        "issues",
        "no",
        "none",
        "not",
        "problem",
        "problems",
        "regression",
        "regressions",
        "warning",
        "warnings",
        "without",
        "zero",
    }
)

# Secret verdicts whose content is safe to store or send: `redact` already
# replaced the credential, and `clean` found none. `flag` hands back the
# original text and `block` empties it, so neither may be persisted as-is.
_SECRET_SAFE_ACTIONS = ("clean", "redacted")


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


_RESULT_ROLES = ("assistant", "tool", "tool_use", "function")

# A step that only reports how the run ended (a test summary line, an exit
# code) is evidence of the outcome, not of the procedure that produced it.
_RESULT_REPORT_RE = re.compile(
    r"^(?:"
    r"exit\s*(?:code)?\s*:?\s*-?\d+|"
    r"\d+\s+(?:passed|failed|errors?|warnings?|skipped)\b|"
    r"(?:all\s+)?(?:tests?|checks?|build|suite|pipeline)\s+"
    r"(?:passed|failed|green|red|ok|succeeded)\b"
    r")",
    re.IGNORECASE,
)


def _result_transcript(events: list[dict[str, Any]]) -> str:
    """Keep only the messages that report what the run did.

    Falls back to the whole transcript when the session recorded no agent-side
    output, so a caller-supplied conversation still scores.
    """
    result_lines = [
        f"{e['role']}: {e['content']}" for e in events if e["role"].lower() in _RESULT_ROLES and e["content"].strip()
    ]
    if result_lines:
        return "\n".join(result_lines)
    return "\n".join(f"{e['role']}: {e['content']}" for e in events if e["content"].strip())


def _retire_stale_groups(
    active_keys: set[str],
    *,
    user_id: str,
    tenant_id: str,
    result: dict[str, Any],
) -> None:
    """Withdraw a distilled group that no longer has qualifying episodes.

    A corrected verdict can move the last episode out of a failure or success
    group, leaving an observation and a lesson that still claim evidence they
    no longer have. Both are retired, and a group that comes back is revived by
    the next upsert rather than being written a second time.
    """
    stale_keys: set[str] = set()
    stale_memory_ids: list[str] = []
    # Ghosted rows still name their key: maintenance may have ghosted the
    # memory long before a corrected verdict emptied its group, and skipping
    # them would leave the observation claiming evidence the group lost.
    for row in _fetch_all(
        """SELECT id, metrics, is_ghost FROM memories
           WHERE tenant_id = ? AND user_id = ? AND memory_type IN ('procedural', 'episodic')""",
        (tenant_id, user_id),
    ):
        try:
            metrics = json.loads(row.get("metrics") or "{}")
        except (TypeError, ValueError):
            continue
        key = str(metrics.get("exp_key") or "")
        if not key.startswith(("failure:", "success:")) or key in active_keys:
            continue
        stale_keys.add(key)
        if not int(row.get("is_ghost") or 0):
            stale_memory_ids.append(str(row["id"]))

    if not stale_keys:
        return

    conn = _connect()
    try:
        for memory_id in stale_memory_ids:
            conn.execute(
                "UPDATE memories SET is_ghost = 1, importance = 0.0, updated_at = ? "
                "WHERE id = ? AND tenant_id = ? AND user_id = ? AND is_ghost = 0",
                (_now_iso(), memory_id, tenant_id, user_id),
            )
        conn.commit()
    finally:
        conn.close()
    result["groups_retired"] = len(stale_memory_ids)

    for row in _fetch_all(
        """SELECT id, pattern_key FROM observations
           WHERE tenant_id = ? AND user_id = ? AND status IN ('active', 'refined')""",
        (tenant_id, user_id),
    ):
        if str(row.get("pattern_key") or "") not in stale_keys:
            continue
        archive_observation(str(row["id"]), user_id=user_id, tenant_id=tenant_id)


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

    # The capture pipeline redacts raw events, but episode summaries are built
    # from the caller's messages; apply the same secret policy before storing.
    summary_verdict = evaluate_secrets(_task_summary(messages), tenant_id=tenant_id)
    summary = summary_verdict.content if summary_verdict.action_taken in _SECRET_SAFE_ACTIONS else ""
    # Scan the whole transcript: truncating let sensitive content later in a
    # long session slip past the flag. A credential counts as sensitivity for
    # the same reason, so a secret-bearing session is never distilled.
    sensitivity_text = "\n".join(str(m.get("content") or "") for m in messages)
    secret_verdict = evaluate_secrets(sensitivity_text, tenant_id=tenant_id)
    is_sensitive = (
        1
        if (
            secret_verdict.flagged
            or summary_verdict.flagged
            or (sensitivity_text and resolve_is_sensitive(None, sensitivity_text)[0])
        )
        else 0
    )

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

    # Outcome details are free text from CI or a user; redact before they
    # become searchable episode data or a signature. A `flag` verdict keeps the
    # original text, so only a clean or redacted detail may be stored.
    detail_verdict = evaluate_secrets(verdict.detail or "", tenant_id=tenant_id)
    if detail_verdict.action_taken not in _SECRET_SAFE_ACTIONS:
        detail_text = None
        signature = None
        detail_is_sensitive = True
    else:
        detail_text = detail_verdict.content[:_MAX_DETAIL_CHARS] or None
        signature = normalize_error_signature(detail_text) if detail_text else None
        detail_is_sensitive = bool(detail_text) and (
            detail_verdict.flagged or resolve_is_sensitive(None, verdict.detail or "")[0]
        )

    conn = _connect()
    try:
        conn.execute(
            """UPDATE episodes SET outcome = ?, outcome_score = ?, score_source = ?, score_detail = ?,
                   error_signature = COALESCE(?, error_signature), resolved = ?,
                   is_sensitive = CASE WHEN ? THEN 1 ELSE is_sensitive END, updated_at = ?
               WHERE id = ? AND tenant_id = ? AND user_id = ?""",
            (
                outcome,
                score,
                verdict.score_source,
                detail_text,
                signature,
                {"success": 1, "failure": 0}.get(outcome, int(bool(episode.get("resolved")))),
                bool(detail_is_sensitive),
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
        events = [{"role": str(m.get("role") or "unknown"), "content": str(m.get("content") or "")} for m in messages]
    else:
        events = [
            {"role": str(r.get("role") or "unknown"), "content": str(r.get("content") or "")}
            for r in _fetch_all(
                """SELECT role, content FROM raw_events
                   WHERE tenant_id = ? AND user_id = ? AND session_id = ? AND tombstone = 0
                   ORDER BY position ASC, captured_at ASC""",
                (tenant_id, user_id, episode["session_id"]),
            )
        ]
    transcript = "\n".join(f"{e['role']}: {e['content']}" for e in events)
    # `flag` mode hands back the original text, so an unsafe transcript must not
    # reach a provider or a stored column. The redacted form is what the
    # signature is derived from, so a credential never becomes searchable.
    transcript_verdict = evaluate_secrets(transcript, tenant_id=tenant_id)
    redacted_transcript = evaluate_secrets(transcript, mode="redact", tenant_id=tenant_id).content
    safe_transcript = redacted_transcript if transcript_verdict.action_taken in _SECRET_SAFE_ACTIONS else ""
    # The deterministic scan reads result-bearing output only: the request and
    # any history it quotes describe the task, so their problem words say
    # nothing about how this session ended.
    assessment_text = evaluate_secrets(_result_transcript(events), mode="redact", tenant_id=tenant_id).content

    mode = _llm_scoring_enabled()
    # Never send a sensitive or secret-bearing transcript to a provider.
    scoring_text = "" if episode["is_sensitive"] else safe_transcript
    detail: str
    if mode in ("llm", "auto") and scoring_text:
        try:
            score, rationale = _llm_rubric(scoring_text, tenant_id, user_id)
            source = "llm_rubric"
            outcome = _score_to_outcome(score)
            detail = f"llm_rubric: {rationale}"[:_MAX_DETAIL_CHARS]
        except Exception as exc:
            logger.info("LLM rubric unavailable, using deterministic fallback: %s", exc)
            assessment = _deterministic_assessment(assessment_text)
            score, outcome = assessment["score"], assessment["outcome"]
            source = "fallback"
            detail = (
                f"degraded deterministic fallback after LLM failure ({type(exc).__name__}): "
                f"failure_signals={assessment['failures']} success_signals={assessment['successes']}"
            )[:_MAX_DETAIL_CHARS]
    else:
        assessment = _deterministic_assessment(assessment_text)
        score, outcome = assessment["score"], assessment["outcome"]
        source = "fallback"
        detail = (
            f"degraded deterministic fallback (no LLM rubric): "
            f"failure_signals={assessment['failures']} success_signals={assessment['successes']}"
        )[:_MAX_DETAIL_CHARS]

    # A signature taken from the whole transcript follows whatever the request
    # quoted; the result-bearing output is what a later session will match on.
    signature = normalize_error_signature(assessment_text or redacted_transcript)

    conn = _connect()
    try:
        conn.execute(
            """UPDATE episodes SET outcome = ?, outcome_score = ?, score_source = ?, score_detail = ?,
                   error_signature = COALESCE(?, error_signature), resolved = ?,
                   is_sensitive = CASE WHEN ? THEN 1 ELSE is_sensitive END, updated_at = ?
               WHERE id = ? AND tenant_id = ? AND user_id = ?
                 AND (score_source IS NULL OR score_source NOT IN ('user_feedback', 'ci_result'))""",
            (
                outcome,
                _clamp_score(score),
                source,
                detail,
                signature,
                {"success": 1, "failure": 0}.get(outcome, int(bool(episode.get("resolved")))),
                bool(transcript_verdict.flagged),
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


_ProceduralSnapshot = dict[str, tuple[str | None, str | None, str | None]]


@dataclass
class _DistillState:
    """Bookkeeping one distillation run threads through its group writers."""

    result: dict[str, Any]
    snapshot: _ProceduralSnapshot | None
    observations: dict[str, _ObservationBaseline]
    same_task_failures: dict[str, list[str]]
    session_events: dict[str, list[dict[str, Any]]]
    recipe_sessions: dict[str, str]


def _procedural_snapshot(*, user_id: str, tenant_id: str) -> _ProceduralSnapshot:
    """Read every live procedural row once, keyed by memory id.

    Distillation revisits each of the user's groups on every capture, and an
    unchanged group needs no connection at all. Reading the stored shape up
    front turns that case into a dict lookup instead of one round trip per
    group.
    """
    rows = _fetch_all(
        "SELECT id, content_hash, metrics, error_signature FROM memories "
        "WHERE tenant_id = ? AND user_id = ? AND memory_type = 'procedural' AND is_ghost = 0",
        (tenant_id, user_id),
    )
    return {str(row["id"]): (row["content_hash"], row["metrics"], row["error_signature"]) for row in rows}


def _observation_shape(
    observation_text: str,
    confidence: float,
    episode_ids: list[str],
    category: str,
    memory_type: str,
) -> tuple[str, str, tuple[str, ...], str, str]:
    """The comparable state of one distilled observation.

    Category and memory type belong in the shape: a group whose text and evidence
    already match but whose stored classification drifted still needs its write.
    """
    return (
        observation_text,
        f"{float(confidence):.6f}",
        tuple(dict.fromkeys(episode_ids)),
        category,
        memory_type,
    )


@dataclass(frozen=True)
class _ObservationBaseline:
    """One stored observation as the distillation scan saw it.

    ``version`` is the row version the shape was read at. A replacement computed
    from an episode scan that started before another writer committed must not
    publish over that write, because the scan never judged it, so the writer
    carries this version back with the evidence it did judge.
    """

    shape: tuple[str, str, tuple[str, ...], str, str]
    version: int
    episode_ids: tuple[str, ...]
    # When the stored text was written. Steps are reused only while no episode
    # in the group moved after this, because a recaptured session keeps its
    # episode id and would otherwise pin the first transcript forever.
    refined_at: str


def _observation_snapshot(*, user_id: str, tenant_id: str) -> dict[str, _ObservationBaseline]:
    """Read every live default-lane observation once, keyed by pattern key.

    Distillation revisits each of the user's groups on every capture, and a
    group whose stored observation already carries the same text and evidence
    needs no write. Reading the stored shape up front turns an untouched group
    into a dict lookup instead of another upsert transaction, so a capture with
    10,000 historical groups writes only the groups it actually changed.
    """
    rows = _fetch_all(
        "SELECT pattern_key, observation_text, confidence, supporting_episode_ids, "
        "category, memory_type, version, last_refined_at FROM observations "
        "WHERE tenant_id = ? AND user_id = ? AND status <> 'archived' "
        "AND COALESCE(agent_id, 'default') = 'default' AND COALESCE(project, 'default') = 'default'",
        (tenant_id, user_id),
    )
    shape: dict[str, _ObservationBaseline] = {}
    for row in rows:
        try:
            stored_ids = json.loads(row.get("supporting_episode_ids") or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(stored_ids, list):
            continue
        episode_ids = tuple(dict.fromkeys(str(eid) for eid in stored_ids))
        shape[str(row["pattern_key"])] = _ObservationBaseline(
            shape=_observation_shape(
                str(row.get("observation_text") or ""),
                float(row.get("confidence") or 0.0),
                list(episode_ids),
                str(row.get("category") or ""),
                str(row.get("memory_type") or ""),
            ),
            version=int(row.get("version") or 0),
            episode_ids=episode_ids,
            refined_at=str(row.get("last_refined_at") or ""),
        )
    return shape


def _upsert_procedural_memory(
    spec: ProceduralMemorySpec,
    *,
    user_id: str,
    tenant_id: str,
    snapshot: _ProceduralSnapshot | None = None,
) -> tuple[str, bool]:
    """Insert or refresh the retrieval-facing procedural memory for ``key``.

    Returns (memory_id, changed). The row carries ``memory_type='procedural'``
    and, for failures, ``error_signature`` — that is the token the keyword
    signal matches on when a matching error later appears in a session.
    ``snapshot`` is the pre-read procedural state from ``_procedural_snapshot``;
    when it already holds this row's exact shape the write is skipped without
    opening a connection.
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

    if snapshot is not None and snapshot.get(memory_id) == (content_hash, metrics, spec.error_signature):
        return memory_id, False

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT content_hash, metrics, error_signature, is_ghost FROM memories "
            "WHERE id = ? AND tenant_id = ? AND user_id = ?",
            (memory_id, tenant_id, user_id),
        ).fetchone()
        if (
            row is not None
            and not int(row["is_ghost"] or 0)
            and (
                row["content_hash"] == content_hash
                and row["metrics"] == metrics
                and row["error_signature"] == spec.error_signature
            )
        ):
            return memory_id, False
        if row is not None:
            conn.execute(
                """UPDATE memories SET content = ?, content_hash = ?, importance = ?,
                       metrics = ?, error_signature = ?, is_ghost = 0, updated_at = ?
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
        conn.commit()
        return memory_id, True
    finally:
        conn.close()


# A trailing line of a run's own output: a duration or an exit status on its
# own carries no procedure, so it never becomes a recipe step.
_RESULT_SUFFIX_RE = re.compile(
    r"^(?:in\s+\d+(?:\.\d+)?\s*(?:s|secs?|seconds?|ms)|"
    r"\(\d+(?:\.\d+)?\s*(?:s|secs?|seconds?|ms)\)|"
    r"exit\s+(?:code|status)\s*:?\s*-?\d+)$",
    re.IGNORECASE,
)


# The same report can arrive joined onto one line - "52 passed in 3.1s,
# exit code 0" - so a trailing result clause is dropped before the words that
# explain the work are counted.
_RESULT_CLAUSE_RE = re.compile(
    r"(?:^|[,;])\s*"
    r"(?:in\s+\d+(?:\.\d+)?\s*(?:s|secs?|seconds?|ms)|"
    r"\(\d+(?:\.\d+)?\s*(?:s|secs?|seconds?|ms)\)|"
    r"exit\s+(?:code|status)\s*:?\s*-?\d+)\s*$",
    re.IGNORECASE,
)


def _strip_result_clauses(text: str) -> str:
    """Drop trailing run-report clauses from a line that also explains work."""
    while True:
        stripped = _RESULT_CLAUSE_RE.sub("", text)
        if stripped == text:
            return text
        text = stripped


def _strip_result_lines(content: str) -> str:
    """Flatten a message for the recipe, minus the lines that only report a run.

    Tool output arrives line by line - "52 passed in 3.1s" then "exit code 0" -
    so flattening first glued the report into the prose and stored it as a
    recipe step. Each line is judged on its own: a pure result line or a bare
    duration/exit-status line is dropped, and a line that reports a result and
    then explains the work is kept whole.
    """
    kept: list[str] = []
    for raw_line in content.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or _RESULT_SUFFIX_RE.match(line):
            continue
        result_match = _RESULT_REPORT_RE.match(line)
        if result_match is not None:
            remainder = re.sub(
                r"^(?:[ .;,]+|-\s+)+",
                "",
                _strip_result_clauses(line[result_match.end() :]),
            ).strip()
            words = re.findall(r"[A-Za-z]{2,}", remainder)
            if len(words) < _MIN_REPORT_TAIL_WORDS:
                # "Tests passed. Run migrations." - the tail is the only step in
                # the recipe, so a short imperative is kept while a short result
                # ("exit code 0", "3.1s") still is not.
                if (
                    len(words) < _MIN_INSTRUCTION_WORDS
                    or all(word.lower() in _STATUS_QUALIFIER_WORDS for word in words)
                    or _RESULT_SUFFIX_RE.match(remainder)
                    or _RESULT_REPORT_RE.match(remainder) is not None
                ):
                    continue
                kept.append(remainder)
                continue
        kept.append(line)
    return " ".join(kept)


# The note the recipe writer appends after its steps. The writer and the
# parser share these fragments so the block ends only where the writer ends it:
# a step that merely mentions "The same task" stays part of the recipe.
_FAILURE_NOTE_PREFIX = "The same task also failed "
_FAILURE_NOTE_SUFFIX = " time(s) with an unresolved error;"
_STEP_BLOCK_RE = re.compile(
    r" Steps: (.+?)(?:\s+" + re.escape(_FAILURE_NOTE_PREFIX) + r"\d+" + re.escape(_FAILURE_NOTE_SUFFIX) + r"|$)"
)
_STEP_INDEX_RE = re.compile(r"(?:^|\s)\d+\)\s*")


def _steps_from_stored(observation_text: str) -> list[str]:
    """Recover the recipe steps a stored observation already carries.

    Steps are written as " 1) ... 2) ..." between the outcome sentence and the
    failure note, so an unchanged group rebuilds identical text without
    re-reading the transcript it came from.
    """
    match = _STEP_BLOCK_RE.search(observation_text)
    if match is None:
        return []
    # Trim only the whitespace the writer separates steps with: a step may end
    # in a period of its own ("run docker build ."), and dropping it here would
    # rebuild text that never matches the transcript, so the recipe would
    # rewrite itself on every pass.
    return [part.strip() for part in _STEP_INDEX_RE.split(match.group(1)) if part.strip()]


def _procedure_steps(
    tenant_id: str,
    user_id: str,
    session_id: str,
    *,
    limit: int = 5,
    events: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Derive the ordered steps an episode actually executed.

    Only agent-side output is read: the user's opening request describes the
    task, and replaying it as a step is how a recipe ended up saying "reuse the
    original request". Each step is passed through the secret policy first.

    ``events`` carries the rows a distillation run already read for this
    session, so a pass over many groups replays them from one query instead of
    one round trip per group.
    """
    if not session_id:
        return []
    steps: list[str] = []
    rows = events if events is not None else _raw_events_for_session(tenant_id, user_id, session_id)
    for event in rows:
        if (event.get("role") or "").lower() not in ("assistant", "tool", "tool_use", "function"):
            continue
        text = _strip_result_lines(str(event.get("content") or ""))
        if len(text) < _MIN_SUMMARY_LEN:
            continue
        verdict = evaluate_secrets(text, tenant_id=tenant_id)
        # `flag` mode hands back the original text, so a recipe step may only be
        # stored from a clean or already-redacted verdict.
        if verdict.action_taken not in _SECRET_SAFE_ACTIONS:
            continue
        text = verdict.content.strip()[:_MAX_DETAIL_CHARS]
        if len(text) < _MIN_SUMMARY_LEN or (steps and steps[-1] == text):
            continue
        steps.append(text)
        if len(steps) >= limit:
            break
    return steps


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

    Every group is measured on each run so its counts include its whole history,
    but only a group whose stored observation and lesson actually differ is
    written: an untouched group costs two reads, not two transactions.
    """
    # Both baselines are read before the episode scan: they are what the scan
    # is allowed to treat as already-judged, so reading them afterwards would
    # count evidence another writer committed mid-scan as seen when it was not.
    procedural_snapshot = _procedural_snapshot(user_id=user_id, tenant_id=tenant_id)
    observation_snapshot = _observation_snapshot(user_id=user_id, tenant_id=tenant_id)

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
        "observations_deferred": 0,
        "procedural_memories_written": 0,
        "recipes_with_steps": 0,
        "groups_retired": 0,
    }
    skipped = _fetch_one(
        "SELECT COUNT(*) AS n FROM episodes "
        "WHERE tenant_id = ? AND user_id = ? AND is_sensitive = 1 AND score_source IS NOT NULL",
        (tenant_id, user_id),
    )
    result["skipped_sensitive"] = int((skipped or {}).get("n") or 0)

    # A step rebuild needs its session's raw events, but only a group whose
    # evidence changed ever rebuilds: an unchanged group re-derives its steps
    # from the observation text it already stored. The cache therefore starts
    # empty and fills on demand, so a pass that changes nothing reads no events
    # at all and a pass that changes one group reads one session.
    recipe_sessions = {slug: str(group[-1].get("session_id") or "") for slug, group in successes.items()}
    session_events: dict[str, list[dict[str, Any]]] = {}
    state = _DistillState(
        result=result,
        snapshot=procedural_snapshot,
        observations=observation_snapshot,
        same_task_failures={},
        session_events=session_events,
        recipe_sessions=recipe_sessions,
    )
    try:
        _distill_failure_group(failures, state=state, user_id=user_id, tenant_id=tenant_id)
        _distill_success_group(successes, state=state, user_id=user_id, tenant_id=tenant_id)
        _retire_stale_groups(
            {f"failure:{sig}" for sig in failures} | {f"success:{slug}" for slug in successes},
            user_id=user_id,
            tenant_id=tenant_id,
            result=result,
        )
    finally:
        # A group that committed before a later write raised still left a recall
        # miss behind, so the eviction runs on the way out of the failure too.
        if result["procedural_memories_written"] or result["groups_retired"]:
            # One invalidation per distillation run, scoped to this user and tenant:
            # the eviction only touches entries keyed by this scope, and a batch of
            # upserts costs one pass over the cache instead of one per group.
            get_hybrid_retriever().invalidate_caches(user_id, tenant_id)
    return result


def _session_events(
    state: _DistillState,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> list[dict[str, Any]]:
    """Read one session's replayable events the first time a group needs them."""
    if not session_id:
        return []
    cached = state.session_events.get(session_id)
    if cached is None:
        cached = _raw_events_for_session(tenant_id, user_id, session_id)
        state.session_events[session_id] = cached
    return cached


def _baseline_from(stored: dict[str, Any]) -> _ObservationBaseline:
    """The row a write actually left behind, as the next pass's baseline.

    The in-memory snapshot has to describe the stored row rather than the
    candidate, because a refused or merged write leaves evidence this candidate
    never listed, and a baseline that disagrees would rebuild the same steps on
    every pass.
    """
    episodes = tuple(str(eid) for eid in (stored.get("supporting_episode_ids") or []))
    return _ObservationBaseline(
        shape=_observation_shape(
            str(stored.get("observation_text") or ""),
            float(stored.get("confidence") or 0.0),
            list(episodes),
            str(stored.get("category") or "procedural"),
            str(stored.get("memory_type") or "procedural"),
        ),
        version=int(stored.get("version") or 0),
        episode_ids=episodes,
        refined_at=str(stored.get("last_refined_at") or ""),
    )


def _steps_still_current(prior: _ObservationBaseline, group: list[dict[str, Any]]) -> bool:
    """Whether every episode in the group predates the stored recipe text.

    A recapture rewrites the session's episode in place, so its id stays the
    same while the transcript it describes grows. The update stamp is what
    separates "nothing changed" from "the same id now holds a new transcript",
    and an episode with no stamp cannot be proven current, so it re-derives.
    """
    if not prior.refined_at:
        return False
    return all(bool(ep.get("updated_at")) and str(ep.get("updated_at")) <= prior.refined_at for ep in group)


def _distill_failure_group(
    failures: dict[str, list[dict[str, Any]]],
    *,
    state: _DistillState,
    user_id: str,
    tenant_id: str,
) -> None:
    """Write one lesson per failure group.

    Records, for signature-keyed groups, the episode ids grouped by the task
    they were doing: the same task can also succeed, and that recipe has to see
    these failures before it can be offered back as a recipe.
    """
    result = state.result
    snapshot = state.snapshot
    observations = state.observations
    same_task_failures: dict[str, list[str]] = {}
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
        pattern_key = f"failure:{sig}"[:200]
        prior = observations.get(pattern_key)
        wanted = _observation_shape(text, confidence, ids, "procedural", "procedural")
        if prior is None or prior.shape != wanted:
            stored, written = upsert_episode_observation(
                EpisodeObservationSpec(
                    scan_base_episode_ids=prior.episode_ids if prior else (),
                    scan_base_version=prior.version if prior else 0,
                    pattern_key=pattern_key,
                    observation_text=text,
                    category="procedural",
                    confidence=confidence,
                    episode_ids=ids,
                    replace_episode_evidence=True,
                ),
                user_id=user_id,
                tenant_id=tenant_id,
            )
            # A refused write left the newer row alone: the snapshot keeps the
            # version it scanned, so this group is retried next pass instead of
            # being counted as published here.
            if written:
                observations[pattern_key] = _baseline_from(stored)
                result["observations_upserted"] += 1
            else:
                # A refused write is retried on the next pass rather than
                # dropped, and the count proves this run published less than it
                # derived instead of reporting a write that never happened.
                result["observations_deferred"] += 1
        content = (
            f"[procedural lesson] Error signature: {label}\n"
            f"Context: {sample_summary}\n"
            f"This approach failed {n} time(s). When `{label}` appears, stop and check the failing "
            f"step instead of retrying the same fix. Evidence: {n} prior episode(s)."
        )
        _, changed = _upsert_procedural_memory(
            ProceduralMemorySpec(
                key=f"failure:{sig}",
                content=content,
                episode_ids=ids,
                error_signature=sig if is_signature else None,
                importance=0.8,
            ),
            user_id=user_id,
            tenant_id=tenant_id,
            snapshot=snapshot,
        )
        result["failures_distilled"] += 1
        if changed:
            result["procedural_memories_written"] += 1
        if is_signature:
            # Each episode counts under the task it was actually doing: one
            # signature can hit several tasks, and a recipe may only see the
            # failures of its own task.
            for ep in group:
                summary = (ep.get("summary") or "").strip()
                if len(summary) < _MIN_SUMMARY_LEN:
                    continue
                same_task_failures.setdefault(_slug(summary), []).append(ep["id"])
    state.same_task_failures = same_task_failures


def _distill_success_group(
    successes: dict[str, list[dict[str, Any]]],
    *,
    state: _DistillState,
    user_id: str,
    tenant_id: str,
) -> None:
    result = state.result
    snapshot = state.snapshot
    observations = state.observations
    same_task_failures = state.same_task_failures
    for slug, group in successes.items():
        ids = [ep["id"] for ep in group]
        summary = group[-1].get("summary") or slug
        n = len(group)
        avg_score = sum((ep.get("outcome_score") or 0.5) for ep in group) / n
        confidence = min(0.95, 0.5 + 0.1 * n)
        session_id = state.recipe_sessions.get(slug, "")
        contradicted = [eid for eid in same_task_failures.get(slug, []) if eid not in ids]
        pattern_key = f"success:{slug}"
        evidence_ids = list(dict.fromkeys([*ids, *contradicted]))
        prior = observations.get(pattern_key)
        # A group whose evidence and transcripts are unchanged already stored
        # the steps its transcript yielded, so it rebuilds the same block from
        # that text instead of paying for a raw event read it does not need.
        if prior is not None and prior.episode_ids == tuple(evidence_ids) and _steps_still_current(prior, group):
            steps = _steps_from_stored(prior.shape[0])
        else:
            steps = _procedure_steps(
                tenant_id,
                user_id,
                session_id,
                events=_session_events(state, tenant_id=tenant_id, user_id=user_id, session_id=session_id),
            )
        step_block = " Steps: " + " ".join(f"{i}) {s}" for i, s in enumerate(steps, 1)) if steps else ""
        failure_note = (
            f" {_FAILURE_NOTE_PREFIX}{len(contradicted)}{_FAILURE_NOTE_SUFFIX} "
            "check those before reusing this approach."
            if contradicted
            else ""
        )
        # A success is still evidence about how this task ended, so it stays a
        # procedural observation; only the claim that it is a reusable recipe
        # waits for steps the transcript actually recorded.
        if steps:
            text = (
                f'[procedural] Reusable recipe - "{summary}": succeeded {n} time(s) '
                f"(avg score {avg_score:.2f}).{step_block}{failure_note}"
            )
            content = (
                f"[procedural recipe] Task: {summary}\n"
                f"This approach succeeded {n} time(s) with average score {avg_score:.2f}."
                f"{step_block}{failure_note}"
            )
        else:
            text = (
                f'[procedural] Task - "{summary}": succeeded {n} time(s) (avg score {avg_score:.2f}). '
                f"The transcript recorded no repeatable step, so no procedure is claimed.{failure_note}"
            )
            content = (
                f"[procedural] Task: {summary}\n"
                f"Succeeded {n} time(s) with average score {avg_score:.2f}; the transcript recorded "
                f"no repeatable step, so no procedure is claimed.{failure_note}"
            )
        wanted = _observation_shape(text, confidence, evidence_ids, "procedural", "procedural")
        if prior is None or prior.shape != wanted:
            stored, written = upsert_episode_observation(
                EpisodeObservationSpec(
                    scan_base_episode_ids=prior.episode_ids if prior else (),
                    scan_base_version=prior.version if prior else 0,
                    pattern_key=pattern_key,
                    observation_text=text,
                    category="procedural",
                    confidence=confidence,
                    episode_ids=evidence_ids,
                    replace_episode_evidence=True,
                ),
                user_id=user_id,
                tenant_id=tenant_id,
            )
            # A refused write left the newer row alone: the snapshot keeps the
            # version it scanned, so this group is retried next pass instead of
            # being counted as published here.
            if written:
                observations[pattern_key] = _baseline_from(stored)
                result["observations_upserted"] += 1
            else:
                # A refused write is retried on the next pass rather than
                # dropped, and the count proves this run published less than it
                # derived instead of reporting a write that never happened.
                result["observations_deferred"] += 1
        _, changed = _upsert_procedural_memory(
            ProceduralMemorySpec(
                key=f"success:{slug}",
                content=content,
                episode_ids=ids,
                importance=0.7,
            ),
            user_id=user_id,
            tenant_id=tenant_id,
            snapshot=snapshot,
        )
        result["successes_distilled"] += 1
        if changed:
            result["procedural_memories_written"] += 1
        if steps:
            result["recipes_with_steps"] += 1


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
