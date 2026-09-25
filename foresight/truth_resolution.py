"""PIX-4702 truth resolution — write-time supersede detection.

When a newly stored memory contradicts an existing latest-truth memory for
the same user and tenant, the older memory is marked ``is_latest = 0`` with
``superseded_by`` pointing at the new memory, and an ``updates`` edge is
created between the two. Superseded and inferred memories stay retrievable:
the hybrid retriever only down-ranks them, it never filters them out.

Supersede detection is OFF by default and must be enabled with the
``FORESIGHT_SUPERSEDE_DETECTION`` environment variable. The detector is
deliberately conservative:

- Sentiment conflict: Jaccard overlap >= threshold AND one text contains the
  sentiment opposite of a word in the other (e.g. "I love therapy" vs
  "I hate therapy").
- Negation flip: Jaccard overlap >= threshold AND the NEW text carries a
  stop/quit marker (``no longer``, ``not anymore``, ``stopped``, ``quit``,
  ``gave up``) that is absent from the old text. The asymmetry (marker must
  be in the new text only) keeps restatements of an already-quitted state
  from being misread as contradictions.

Known limitation: a relapse phrased without a marker in the new text (e.g.
old "I quit smoking" / new "I started smoking again") is not flagged yet.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from .encryption import decrypt_if_encrypted
from .enhanced_synthesizer import compute_overlap_score, find_sentiment_conflict

logger = logging.getLogger("foresight_truth_resolution")

# Candidate window for supersession detection. A contradictory memory older
# than this window (ranked by created_at DESC) is never marked superseded —
# widen deliberately if stores become high-frequency per user/tenant.
CANDIDATE_FETCH_LIMIT = 100
OVERLAP_THRESHOLD = 0.30

# A new text carrying one of these markers (and the old text not) flips the
# recorded state, so the old memory is superseded by the new one.
NEGATION_FLIP_MARKERS = ("no longer", "not anymore", "stopped", "quit", "gave up")

ENABLE_ENV_VAR = "FORESIGHT_SUPERSEDE_DETECTION"


@dataclass
class SupersessionSignal:
    """Result of a pure supersession check between two memory contents."""

    overlap: float
    signal: str  # 'sentiment_conflict' | 'negation_flip'
    confidence: float

    def to_dict(self) -> dict:
        return {"overlap": self.overlap, "signal": self.signal, "confidence": self.confidence}


@dataclass
class SupersessionMatch:
    """An older memory that the new memory supersedes."""

    superseded_memory_id: str
    content: str
    overlap: float
    signal: str
    confidence: float

    def to_dict(self) -> dict:
        return {
            "superseded_memory_id": self.superseded_memory_id,
            "overlap": self.overlap,
            "signal": self.signal,
            "confidence": self.confidence,
        }


def supersede_detection_enabled() -> bool:
    """Whether write-time supersede detection is enabled (off by default)."""
    return os.environ.get(ENABLE_ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def detect_supersession(new_content: str, old_content: str) -> SupersessionSignal | None:
    """Check whether ``new_content`` supersedes ``old_content``.

    Requires a Jaccard overlap of at least :data:`OVERLAP_THRESHOLD` plus one
    of the contradiction signals above. Returns ``None`` for unrelated or
    merely rephrased content.
    """
    overlap = compute_overlap_score(new_content, old_content)
    if overlap < OVERLAP_THRESHOLD:
        return None

    if find_sentiment_conflict(new_content, old_content) is not None:
        signal = "sentiment_conflict"
    else:
        new_lower = new_content.lower()
        old_lower = old_content.lower()
        if not any(marker in new_lower and marker not in old_lower for marker in NEGATION_FLIP_MARKERS):
            return None
        signal = "negation_flip"

    return SupersessionSignal(overlap=overlap, signal=signal, confidence=min(overlap * 1.5, 1.0))


def detect_and_mark_supersessions(
    conn,
    new_memory_id: str,
    new_content: str,
    uid: str,
    tenant_id: str,
) -> list[SupersessionMatch]:
    """Mark older memories superseded by ``new_memory_id`` and return them.

    Fetches the newest latest-truth candidates for the same user and tenant,
    decrypts their content, runs :func:`detect_supersession`, and sets
    ``is_latest = 0`` / ``superseded_by`` on every match. Commits the
    updates on the given connection; the caller owns closing it.
    """
    rows = conn.execute(
        "SELECT id, content FROM memories "
        "WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0 AND is_latest = 1 AND id != ? "
        "ORDER BY created_at DESC LIMIT ?",
        (uid, tenant_id, new_memory_id, CANDIDATE_FETCH_LIMIT),
    ).fetchall()

    matches: list[SupersessionMatch] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for row in rows or []:
        old_content = decrypt_if_encrypted(row["content"], tenant_id=tenant_id, user_id=uid)
        detection = detect_supersession(new_content, old_content)
        if detection is None:
            continue
        conn.execute(
            "UPDATE memories SET is_latest = 0, superseded_by = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ? AND tenant_id = ? AND is_latest = 1",
            (new_memory_id, now_iso, row["id"], uid, tenant_id),
        )
        matches.append(
            SupersessionMatch(
                superseded_memory_id=row["id"],
                content=old_content,
                overlap=detection.overlap,
                signal=detection.signal,
                confidence=detection.confidence,
            )
        )

    if matches:
        conn.commit()
        logger.info(
            "Supersede: memory %s supersedes %d older memories (%s)",
            new_memory_id,
            len(matches),
            ", ".join(m.superseded_memory_id for m in matches),
        )
    return matches


def create_supersede_edges(
    new_memory_id: str,
    matches: list[SupersessionMatch],
    uid: str,
    tenant_id: str,
) -> int:
    """Create ``updates`` edges (new memory → each superseded memory)."""
    from .memory_relationships import LinkMemoriesOptions, get_memory_relationship_store

    store = get_memory_relationship_store()
    created = 0
    for match in matches:
        store.link_memories(
            source_memory_id=new_memory_id,
            target_memory_id=match.superseded_memory_id,
            relationship_type="updates",
            user_id=uid,
            options=LinkMemoriesOptions(
                tenant_id=tenant_id,
                confidence=match.confidence,
                metadata={"signal": match.signal, "overlap": match.overlap},
            ),
        )
        created += 1
    return created
