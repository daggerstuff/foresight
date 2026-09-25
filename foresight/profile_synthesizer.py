"""
User Profile Synthesis — auto-build static + dynamic profiles from
context blocks and memories.

Inspired by supermemory.ai's profile concept: a single ~50ms call that
compacts a user's state into static (stable facts) and dynamic (recent
context) layers, directly injectable into LLM system prompts.

Static sources:
  - user_preferences context block
  - Memories with scope=trait|fact and retention=long_term|permanent

Dynamic sources:
  - project_context, session_patterns, pending_items context blocks
  - Recent memories with scope=session|arc
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .config import DB_PATH
from .connection_pool import get_pool
from .context_blocks import (
    PENDING_ITEMS,
    PROJECT_CONTEXT,
    SESSION_PATTERNS,
    USER_PREFERENCES,
    get_context_block_agent,
)
from .enhanced_synthesizer import get_enhanced_synthesizer
from .injection_budget import _truncate_to_chars
from .memory_types import EmotionalMetadata, MemoryObject

logger = logging.getLogger("foresight_profile")


@dataclass(frozen=True)
class ProfileBucket:
    """A topical bucket for grouping static profile facts."""

    name: str
    description: str
    keywords: tuple[str, ...] = ()


DEFAULT_PROFILE_BUCKETS: tuple[ProfileBucket, ...] = (
    ProfileBucket(
        name="preferences",
        description="user preferences, likes, dislikes, style, habits, communication rules",
        keywords=(
            "prefer",
            "prefers",
            "preference",
            "like",
            "dislike",
            "style",
            "habit",
            "tone",
            "concise",
            "always",
            "never",
            "avoid",
        ),
    ),
    ProfileBucket(
        name="goals",
        description="goals, plans, ambitions, targets, milestones, intended outcomes",
        keywords=(
            "goal",
            "wants",
            "plan",
            "aim",
            "target",
            "milestone",
            "objective",
            "intend",
            "ship",
            "launch",
            "deadline",
            "roadmap",
        ),
    ),
    ProfileBucket(
        name="work",
        description="work, projects, job, team, tools, stack, employer, day-to-day tasks",
        keywords=(
            "work",
            "job",
            "project",
            "team",
            "stack",
            "code",
            "repo",
            "pipeline",
            "deploy",
            "company",
            "employer",
            "tooling",
        ),
    ),
)


@dataclass
class ProfileConfig:
    """Tuning parameters for profile synthesis."""

    max_static_memories: int = 20
    max_dynamic_memories: int = 10
    include_synthesis: bool = True
    max_static_lines: int = 30
    max_dynamic_lines: int = 20
    buckets: tuple[ProfileBucket, ...] | None = None


@dataclass
class ScopeQueryOptions:
    """Options for querying memories by scope."""

    limit: int = 20
    order_by: str = "importance DESC, created_at DESC"


# Block labels used for each profile layer
_STATIC_BLOCK_LABELS = [USER_PREFERENCES]
_DYNAMIC_BLOCK_LABELS = [PROJECT_CONTEXT, SESSION_PATTERNS, PENDING_ITEMS]

# Placeholder lines that should not be surfaced in a profile
_PLACEHOLDER_PREFIXES = (
    "(No",
    "(no",
    "No ",
    "ROLE:",
    "WHAT I AM:",
    "WHAT I DO:",
    "COMMUNICATION STYLE:",
    "DEFAULT STATE:",
    "AVAILABLE TOOLS:",
    "MEMORY ARCHITECTURE EVOLUTION:",
    "LEARNING PROCEDURES:",
)


def _is_placeholder(line: str) -> bool:
    """Return True if *line* is a default/placeholder block entry."""
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith(_PLACEHOLDER_PREFIXES)


def _extract_block_lines(
    agent: Any,
    labels: list[str],
    *,
    max_per_block: int = 8,
) -> list[str]:
    """Extract non-placeholder lines from one or more context blocks."""
    lines: list[str] = []
    for label in labels:
        content = agent.get_block(label)
        if not content:
            continue
        for line in content.splitlines():
            clean = line.strip()
            if not _is_placeholder(clean):
                lines.append(clean)
                if len(lines) >= max_per_block * len(labels):
                    break
    return lines


def _query_memories_by_scope(
    user_id: str,
    tenant_id: str,
    scopes: tuple[str, ...],
    retentions: tuple[str, ...] | None = None,
    *,
    options: ScopeQueryOptions | None = None,
) -> list[dict[str, Any]]:
    """Query memories filtered by scope and optionally retention."""
    if options is None:
        options = ScopeQueryOptions()
    pool = get_pool(DB_PATH)
    conn = pool.acquire()
    try:
        params: list[Any] = [user_id, tenant_id]
        scope_placeholders = ",".join("?" for _ in scopes)
        params.extend(scopes)

        retention_clause = ""
        if retentions:
            ret_placeholders = ",".join("?" for _ in retentions)
            retention_clause = f"AND retention IN ({ret_placeholders})"
            params.extend(retentions)

        rows = conn.execute(
            f"""SELECT content, category, tags, importance, strength_trend, scope,
                        retention, created_at
                 FROM memories
                 WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0
                   AND scope IN ({scope_placeholders})
                   {retention_clause}
                 ORDER BY {options.order_by}
                 LIMIT ?""",  # nosec B608 - values parameterized; SQL identifiers are hardcoded literals
            (*params, options.limit),
        ).fetchall()
    finally:
        conn.close()

    results: list[dict[str, Any]] = []
    for r in rows:
        tags_raw = r["tags"]
        if isinstance(tags_raw, str):
            try:
                tags_raw = json.loads(tags_raw)
            except (json.JSONDecodeError, TypeError):
                tags_raw = []
        results.append(
            {
                "content": r["content"],
                "category": r["category"],
                "tags": tags_raw,
                "importance": r["importance"],
                "strength_trend": r["strength_trend"],
                "scope": r["scope"],
                "retention": r["retention"],
                "created_at": r["created_at"],
            }
        )
    return results


def _deduplicate_lines(lines: list[str]) -> list[str]:
    """Remove near-duplicate lines while preserving order."""
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        key = line.strip().lower()[:120]
        if key not in seen:
            seen.add(key)
            unique.append(line)
    return unique


_TOKEN_RE = re.compile(r"[a-z']+")
_STOPWORD_TOKENS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "at",
        "by",
        "from",
        "user",
        "it",
        "this",
        "that",
        "their",
        "they",
        "them",
        "when",
        "while",
    }
)


def _tokenize(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORD_TOKENS}


def _classify_line(line: str, buckets: Sequence[ProfileBucket]) -> str:
    """Classify a profile line into the best-matching bucket name.

    Scores each bucket by lexical overlap between the line's tokens and the
    bucket's vocabulary (description + keywords). Ties keep the earlier
    bucket; zero overlap falls back to ``general``.
    """
    line_tokens = _tokenize(line)
    if not line_tokens or not buckets:
        return "general"
    best_name = "general"
    best_score = 0
    for bucket in buckets:
        vocab = _tokenize(bucket.description) | {k.lower() for k in bucket.keywords}
        score = len(line_tokens & vocab)
        if score > best_score:
            best_score = score
            best_name = bucket.name
    return best_name


def _bucketize_lines(
    lines: list[str],
    buckets: Sequence[ProfileBucket],
) -> dict[str, list[str]]:
    """Group static profile lines into topical buckets.

    Lines matching no bucket land in the implicit ``general`` bucket;
    buckets with no lines are omitted.
    """
    grouped: dict[str, list[str]] = {}
    for line in lines:
        grouped.setdefault(_classify_line(line, buckets), []).append(line)
    return grouped


def _run_async(coro):
    """Run an async coroutine safely, handling existing event loops.

    When an event loop is already running (e.g. inside an MCP server),
    asyncio.run() raises RuntimeError. This helper offloads the coroutine
    to a fresh loop in a background thread instead.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def synthesize_profile(
    user_id: str,
    tenant_id: str = "default",
    config: ProfileConfig | None = None,
) -> dict[str, Any]:
    """
    Build a user profile with static (stable facts) and dynamic (recent context) layers.

    Args:
        user_id: User identifier.
        tenant_id: Tenant identifier.
        config: Tuning parameters (max memories, synthesis toggle, etc.).
            When ``config.buckets`` is set, static facts are additionally
            grouped under a ``buckets`` key.

    Returns:
        ``{"static": [str, ...], "dynamic": [str, ...]}`` plus
        ``{"buckets": {name: [str, ...]}}`` when bucketing is enabled.
    """
    cfg = config or ProfileConfig()
    pool = get_pool(DB_PATH)
    conn = pool.acquire()
    try:
        agent = get_context_block_agent(user_id, tenant_id)

        # ── Static layer ────────────────────────────────────────────────
        static_lines: list[str] = []

        # 1. Context-block preferences
        static_lines.extend(_extract_block_lines(agent, _STATIC_BLOCK_LABELS))

        # 2. Stable (trait/fact) memories with long retention
        static_mems = _query_memories_by_scope(
            user_id,
            tenant_id,
            scopes=("trait", "fact"),
            retentions=("long_term", "permanent"),
            options=ScopeQueryOptions(limit=cfg.max_static_memories),
        )
        for m in static_mems:
            tag_str = f" [{', '.join(m['tags'][:3])}]" if m.get("tags") else ""
            static_lines.append(f"{m['content']}{tag_str}")

        # ── Dynamic layer ───────────────────────────────────────────────
        dynamic_lines: list[str] = []

        # 1. Context-block project state, patterns, pending items
        dynamic_lines.extend(_extract_block_lines(agent, _DYNAMIC_BLOCK_LABELS))

        # 2. Recent session/arc memories
        dyn_mems = _query_memories_by_scope(
            user_id,
            tenant_id,
            scopes=("session", "arc"),
            options=ScopeQueryOptions(limit=cfg.max_dynamic_memories, order_by="created_at DESC"),
        )
        for m in dyn_mems:
            dynamic_lines.append(m["content"])

        # ── Optional synthesis on static memories ───────────────────────
        if cfg.include_synthesis and len(static_mems) >= 5:
            try:
                memories_objs: list[MemoryObject] = []
                for m in static_mems[:15]:
                    emo = EmotionalMetadata(intensity=0.5) if m.get("importance", 0.5) > 0.6 else None
                    memories_objs.append(
                        MemoryObject(
                            id=f"prof_{hash(m['content'])}",
                            timestamp=m.get("created_at", ""),
                            scope=m.get("scope", "trait"),
                            retention=m.get("retention", "long_term"),
                            content=m["content"],
                            tags=m.get("tags", []),
                            emotional_context=emo,
                        )
                    )
                synth_result = _run_async(get_enhanced_synthesizer().synthesize(memories_objs, user_id=user_id))
                # If contradictions found, note them
                if synth_result and synth_result.contradictions:
                    for c in synth_result.contradictions[:3]:
                        logger.info(
                            "Profile contradiction: %s — %s vs %s",
                            c.attribute,
                            c.old_value,
                            c.new_value,
                        )
            except Exception:
                logger.debug("Profile synthesis skipped (non-critical)", exc_info=True)

        # ── Deduplicate and trim ────────────────────────────────────────
        profile: dict[str, Any] = {
            "static": _deduplicate_lines(static_lines)[: cfg.max_static_lines],
            "dynamic": _deduplicate_lines(dynamic_lines)[: cfg.max_dynamic_lines],
        }

        # ── Optional topical bucketing of static facts ─────────────────
        if cfg.buckets:
            profile["buckets"] = _bucketize_lines(profile["static"], cfg.buckets)

        return profile
    finally:
        conn.close()


def _fit_section(header: str, lines: list[str], budget: int) -> str:
    """Greedily fit a header plus bullet lines within *budget* characters.

    The first line that does not fit whole is truncated at a word boundary;
    later lines are dropped. Returns ``""`` when the header or no line fits.
    """
    if budget <= 0 or not lines:
        return ""
    used = len(header)
    if used > budget:
        return ""
    pieces = [header]
    for line in lines:
        entry = f"- {line}"
        if used + 1 + len(entry) <= budget:
            pieces.append(entry)
            used += 1 + len(entry)
            continue
        # _truncate_to_chars may grow the text by up to 3 chars ("..."),
        # so leave room for the "- " prefix and that growth.
        avail = budget - used - 1
        if avail > 2:
            fitted_line = _truncate_to_chars(line, avail - 5)
            if fitted_line.strip(" .,;:!?"):
                pieces.append(f"- {fitted_line}")
        break
    if len(pieces) == 1:
        return ""
    return "\n".join(pieces)


def profile_to_prompt(
    profile: dict[str, Any],
    *,
    user_label: str = "User",
    max_chars: int | None = None,
) -> str:
    """
    Format a profile dict into an LLM system-prompt snippet.

    Args:
        profile: Output from ``synthesize_profile()``.
        user_label: Label to use for the user in the prompt.
        max_chars: Optional character budget applied to the formatted prompt
            only (the JSON payload from ``synthesize_profile()`` is never
            affected). Static lines take priority, then dynamic, then
            buckets; over-budget lines are truncated at word boundaries.

    Returns:
        Formatted prompt block, or ``""`` when a budget is set and no
        section fits.
    """
    sections: list[tuple[str, list[str]]] = []

    if profile.get("static"):
        sections.append((f"ABOUT {user_label.upper()}:", list(profile["static"])))

    if profile.get("dynamic"):
        sections.append(("CURRENT CONTEXT:", list(profile["dynamic"])))

    buckets = profile.get("buckets")
    if buckets:
        bucket_lines = [f"{name}: {line}" for name, lines in buckets.items() for line in lines]
        if bucket_lines:
            sections.append(("BY TOPIC:", bucket_lines))

    if max_chars is not None:
        fitted: list[str] = []
        remaining = max_chars
        for header, lines in sections:
            if remaining <= 0:
                break
            text = _fit_section(header, lines, remaining)
            if not text:
                continue
            fitted.append(text)
            remaining -= len(text) + 2  # account for the "\n\n" join
        return "\n\n".join(fitted)

    parts: list[str] = []
    for header, lines in sections:
        parts.append(f"{header}\n" + "\n".join(f"- {line}" for line in lines))

    if not parts:
        return f"# {user_label} Profile\nNo profile data available yet."

    return "\n\n".join(parts)
