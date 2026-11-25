"""
Public Foresight-native context block helpers.

This module provides the renamed public surface for working with continuity
blocks while reusing the existing compatibility-backed implementation.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .block_registry import (
    RULES_BLOCK_LABEL,
    MemoryBlockSchema,
    RuleEntry,
    RuleStrength,
    get_registry,
    initialize_default_blocks,
    parse_rules_block,
    serialize_rules_block,
)
from .connection_pool import get_db_connection
from .context_cache import get_context_cache
from .observations import _ns_clause
from .sensitivity import resolve_is_sensitive
from .sql_helpers import join_sql_parts, placeholders
from .subconscious import (
    DEFAULT_MEMORY_BLOCKS,
    PENDING_ITEMS,
    PROJECT_CONTEXT,
    RULES,
    SESSION_PATTERNS,
    USER_PREFERENCES,
    ContextBlockAgent,
    ContextBlockState,
    MemoryBlock,
    get_context_block_agent as _get_context_block_agent,
    normalize_block_line,
)

logger = logging.getLogger("foresight_context_blocks")

# Serializes the read-modify-write of the rules block: two concurrent add_rule
# calls otherwise read the same list and the later write drops the earlier rule.
_RULES_LOCK = threading.Lock()

DEFAULT_CONTEXT_BLOCKS = DEFAULT_MEMORY_BLOCKS
ContextBlock = MemoryBlock

__all__ = [
    "DEFAULT_CONTEXT_BLOCKS",
    "PENDING_ITEMS",
    "PROJECT_CONTEXT",
    "RULES",
    "SESSION_PATTERNS",
    "USER_PREFERENCES",
    "ContextBlock",
    "ContextBlockAgent",
    "ContextBlockState",
    "MemoryBlockSchema",
    "RuleEntry",
    "RuleGraduateOptions",
    "RuleStrength",
    "add_context_guidance",
    "add_rule",
    "add_subconscious_guidance",
    "clear_context_block",
    "clear_subconscious_block",
    "compile_experience_to_rule",
    "get_context_block",
    "get_context_block_agent",
    "get_context_snapshot",
    "get_context_whisper",
    "get_subconscious_block",
    "get_subconscious_context",
    "get_subconscious_whisper",
    "graduate_experience_to_rule",
    "list_context_block_schemas",
    "list_context_blocks",
    "list_rules",
    "register_context_block_schema",
    "reset_context_block",
    "reset_subconscious_block",
    "retrieve_matching_rules",
    "update_context_block",
    "update_subconscious_block",
]


def register_context_block_schema(schema: MemoryBlockSchema) -> None:
    """Register a custom context block schema for subsequent block updates."""
    get_registry().register(schema)


def list_context_block_schemas() -> list[dict]:
    """List registered context block schemas as API-safe dictionaries."""
    registry = initialize_default_blocks()
    return [schema.to_dict() for schema in registry.list_schemas()]


def get_context_block_agent(
    user_id: str,
    tenant_id: str = "default",
    agent_id: str | None = None,
    project: str | None = None,
) -> ContextBlockAgent:
    """Return the Foresight-native context block agent facade."""
    return _get_context_block_agent(user_id, tenant_id, agent_id=agent_id, project=project)


def list_context_blocks(user_id: str, tenant_id: str = "default") -> list[dict]:
    """List non-empty context blocks for a user."""
    return get_context_block_agent(user_id, tenant_id).get_all_blocks()


def get_context_block(label: str, user_id: str, tenant_id: str = "default") -> str | None:
    """Return a single context block by label."""
    return get_context_block_agent(user_id, tenant_id).get_block(label)


def update_context_block(label: str, content: str, user_id: str, tenant_id: str = "default") -> None:
    """Update a context block."""
    agent = get_context_block_agent(user_id, tenant_id)
    agent.update_block(label, content)


def add_context_guidance(line: str, user_id: str, tenant_id: str = "default") -> None:
    """Append a line to the guidance block."""
    get_context_block_agent(user_id, tenant_id).add_guidance_line(line)


def reset_context_block(label: str, user_id: str, tenant_id: str = "default") -> None:
    """Reset a context block to its default content."""
    get_context_block_agent(user_id, tenant_id).reset_block(label)


def clear_context_block(label: str, user_id: str, tenant_id: str = "default") -> None:
    """Clear a context block."""
    get_context_block_agent(user_id, tenant_id).clear_block(label)


def get_context_whisper(user_id: str, tenant_id: str = "default") -> str:
    """Return the whisper-ready guidance block payload."""
    return get_context_block_agent(user_id, tenant_id).get_whisper()


def get_context_snapshot(user_id: str, tenant_id: str = "default") -> str:
    """Return the full XML snapshot of non-empty context blocks."""
    return get_context_block_agent(user_id, tenant_id).get_full_context()


def get_subconscious_block(label: str, user_id: str, tenant_id: str = "default") -> str | None:
    """Compatibility alias for older subconscious-named integrations."""
    return get_context_block(label, user_id, tenant_id)


def update_subconscious_block(label: str, content: str, user_id: str, tenant_id: str = "default") -> None:
    """Compatibility alias for older subconscious-named integrations."""
    update_context_block(label, content, user_id, tenant_id)


def add_subconscious_guidance(line: str, user_id: str, tenant_id: str = "default") -> None:
    """Compatibility alias for older subconscious-named integrations."""
    add_context_guidance(line, user_id, tenant_id)


def get_subconscious_whisper(user_id: str, tenant_id: str = "default") -> str:
    """Compatibility alias for older subconscious-named integrations."""
    return get_context_whisper(user_id, tenant_id)


def get_subconscious_context(user_id: str, tenant_id: str = "default") -> str:
    """Compatibility alias for older subconscious-named integrations."""
    return get_context_snapshot(user_id, tenant_id)


def reset_subconscious_block(label: str, user_id: str, tenant_id: str = "default") -> None:
    """Compatibility alias for older subconscious-named integrations."""
    reset_context_block(label, user_id, tenant_id)


def clear_subconscious_block(label: str, user_id: str, tenant_id: str = "default") -> None:
    """Compatibility alias for older subconscious-named integrations."""
    clear_context_block(label, user_id, tenant_id)


_LEAKED_BLOCK_RE = re.compile(
    r"^\[(?:session_patterns|user_preferences|pending_items)\]",
    re.IGNORECASE,
)


def _placeholder_for(label: str) -> str:
    """The registry placeholder text for *label* ("" when unregistered)."""
    schema = get_registry().get_schema(label) or initialize_default_blocks().get_schema(label)
    return schema.content if schema else ""


def _clear_generated_block(agent: Any, label: str) -> bool:
    """Restore a block's placeholder once no live source remains for it.

    Only blocks distillation owns are touched, so a hand-edited block is never
    emptied when its source memories expire; the check and the write happen
    under the agent lock so a concurrent manual edit is not overwritten.
    """
    return agent.restore_generated_block(label, _placeholder_for(label))


def _distill_preferences(conn: Any, agent: Any, user_id: str, tenant_id: str) -> bool:
    now_iso = datetime.now(UTC).isoformat()
    pref_rows = conn.execute(
        """
        SELECT content FROM memories
        WHERE user_id = ? AND tenant_id = ?
          AND (category = 'preference' OR scope = 'trait')
          AND (is_ghost = 0 OR is_ghost IS NULL)
          AND expired_at IS NULL
          AND (expires_at IS NULL OR expires_at >= ?)
        ORDER BY importance DESC, updated_at DESC
        LIMIT 10
        """,
        (user_id, tenant_id, now_iso),
    ).fetchall()
    if not pref_rows:
        return _clear_generated_block(agent, "user_preferences")

    unique_rules: list[str] = []
    seen_rules: set[str] = set()

    for r in pref_rows:
        content = (r["content"] or "").strip()
        if not content:
            continue
        clean = re.sub(
            r"^(\s*-\s*)*(\[(?:user_preferences|pending_items|session_patterns|project_context|auto-captured/\w+)\]\s*-?\s*)+",
            "",
            content,
            flags=re.IGNORECASE,
        ).strip()
        clean = re.sub(
            r"\[(?:user_preferences|pending_items|session_patterns|project_context)\]\s*",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip(" -")
        if not clean:
            continue
        # Suppress formatting-only repeats; a rule that differs by a number,
        # scope word, or negation is a distinct instruction.
        key = normalize_block_line(clean)
        if key in seen_rules:
            continue
        seen_rules.add(key)
        unique_rules.append(f"- {clean}")

    if not unique_rules:
        return False

    current_pref = agent.get_block("user_preferences") or ""
    new_pref = "\n".join(unique_rules[:8])
    if current_pref.strip() != new_pref.strip():
        # Only content this pass wrote is marked generated; an unchanged block
        # keeps its ownership, so a manual edit is never cleared later.
        agent.update_block("user_preferences", new_pref, auto_generated=True)
        return True
    return False


def _distill_project_context(conn: Any, agent: Any, user_id: str, tenant_id: str) -> bool:
    now_iso = datetime.now(UTC).isoformat()
    arc_rows = conn.execute(
        """
        SELECT content FROM memories
        WHERE user_id = ? AND tenant_id = ?
          AND (scope = 'arc' OR category IN ('decision', 'fact'))
          AND (is_ghost = 0 OR is_ghost IS NULL)
          AND expired_at IS NULL
          AND (expires_at IS NULL OR expires_at >= ?)
        ORDER BY importance DESC, updated_at DESC
        LIMIT 12
        """,
        (user_id, tenant_id, now_iso),
    ).fetchall()
    if not arc_rows:
        return _clear_generated_block(agent, PROJECT_CONTEXT)

    unique_arc: list[str] = []
    seen_arc: set[str] = set()

    for r in arc_rows:
        content = (r["content"] or "").strip()
        if not content:
            continue
        if _LEAKED_BLOCK_RE.match(content) or content.lower().startswith("hot files:"):
            continue

        clean = re.sub(
            r"^(\s*-\s*)*(\[(?:project_context|auto-captured/\w+)\]\s*-?\s*)+",
            "",
            content,
            flags=re.IGNORECASE,
        ).strip(" -")
        if not clean:
            continue

        key = normalize_block_line(clean)
        if key in seen_arc:
            continue
        seen_arc.add(key)
        unique_arc.append(f"- {clean}")

    if not unique_arc:
        return False

    new_proj = "\n".join(unique_arc[:6])
    # The agent decides atomically: fill an empty/placeholder block, refresh
    # its own generated output, and never overwrite a hand-edited block.
    return agent.refresh_project_context(new_proj, _placeholder_for(PROJECT_CONTEXT))


def auto_distill_context_blocks(user_id: str, tenant_id: str = "default") -> dict[str, Any]:
    """Automatically synthesize and update context blocks from stored memories.

    Operates hands-off in the background to keep user preferences and
    project context continuously refined.
    """
    logger = logging.getLogger("foresight_context_distill")
    agent = _get_context_block_agent(user_id, tenant_id)
    updated_blocks: list[str] = []

    try:
        conn = get_db_connection()
        try:
            if _distill_preferences(conn, agent, user_id, tenant_id):
                updated_blocks.append("user_preferences")
            if _distill_project_context(conn, agent, user_id, tenant_id):
                updated_blocks.append("project_context")
        finally:
            conn.close()

    except Exception as e:
        logger.debug("auto_distill_context_blocks exception (non-fatal): %s", e)

    return {"ok": True, "distilled_blocks": updated_blocks}


# =============================================================================
# Rules block helpers & graduation from procedural experiences (FOR-32)
# =============================================================================

_PROCEDURAL_RULE_CATEGORIES = frozenset({"procedural", "procedure", "workflow", "recipe"})
_MUST_NOT_RE = re.compile(r"\b(?:must\s+not|must_not|never)\b", re.IGNORECASE)
_MUST_RE = re.compile(r"\bmust\b", re.IGNORECASE)
_SHOULD_RE = re.compile(r"\b(?:should|prefer|recommended|consider)\b", re.IGNORECASE)


@dataclass(frozen=True)
class RuleGraduateOptions:
    """Options controlling rule graduation from a procedural experience.

    There is deliberately no caller-supplied ``reviewed`` flag: review must be
    evidenced by a persisted, approved skill record, so any caller cannot
    promote an unreviewed instruction into the rules lane.
    """

    requested_strength: RuleStrength | str | None = None
    applies_when: str = ""
    exception: str = ""
    agent_id: str | None = None
    project: str | None = None


def list_rules(
    user_id: str = "default",
    tenant_id: str = "default",
    agent_id: str | None = None,
    project: str | None = None,
) -> list[RuleEntry]:
    """Return all structured rules stored in the ``rules`` context block."""
    agent = get_context_block_agent(user_id, tenant_id, agent_id=agent_id, project=project)
    raw = agent.get_block(RULES_BLOCK_LABEL) or ""
    try:
        return parse_rules_block(raw)
    except (TypeError, ValueError):
        return []


def add_rule(
    entry: RuleEntry,
    *,
    user_id: str = "default",
    tenant_id: str = "default",
    agent_id: str | None = None,
    project: str | None = None,
) -> RuleEntry:
    """Validate and persist a structured rule in the ``rules`` context block."""
    with _RULES_LOCK:
        return _add_rule_locked(entry, user_id=user_id, tenant_id=tenant_id, agent_id=agent_id, project=project)


def _add_rule_locked(
    entry: RuleEntry,
    *,
    user_id: str,
    tenant_id: str,
    agent_id: str | None,
    project: str | None,
) -> RuleEntry:
    agent = get_context_block_agent(user_id, tenant_id, agent_id=agent_id, project=project)
    existing = list_rules(user_id=user_id, tenant_id=tenant_id, agent_id=agent_id, project=project)
    key = (
        normalize_block_line(entry.statement),
        entry.strength.value,
        entry.applies_when.strip().lower(),
        entry.exception.strip().lower(),
    )
    for prior in existing:
        prior_key = (
            normalize_block_line(prior.statement),
            prior.strength.value,
            prior.applies_when.strip().lower(),
            prior.exception.strip().lower(),
        )
        if prior_key == key:
            return prior
    updated = [*existing, entry]
    agent.update_block(RULES_BLOCK_LABEL, serialize_rules_block(updated))
    # A repeated inject_context for the same query must not serve the pre-rule
    # payload from the 60s injection cache.
    get_context_cache().invalidate_user(user_id)
    return entry


def retrieve_matching_rules(
    context_text: str,
    *,
    terms: list[str] | None = None,
    user_id: str = "default",
    tenant_id: str = "default",
    scope: Any = None,
) -> list[RuleEntry]:
    """Return rules whose ``applies_when`` matches and ``exception`` does not."""
    uid = getattr(scope, "user_id", None) or user_id
    tid = getattr(scope, "tenant_id", None) or tenant_id
    aid = getattr(scope, "agent_id", None)
    proj = getattr(scope, "project", None)
    rules = list_rules(user_id=uid, tenant_id=tid, agent_id=aid, project=proj)
    return [rule for rule in rules if rule.matches(context_text, terms=terms)]


def _infer_experience_strength(observation: dict[str, Any]) -> RuleStrength:
    explicit = observation.get("strength")
    if explicit is not None:
        return RuleStrength(str(explicit).strip().lower())
    text = str(observation.get("observation_text") or "").strip()
    # "should never" is a weak recommendation, not a prohibition: the should
    # check must run before the must_not one, which matches bare "never".
    if _SHOULD_RE.search(text):
        return RuleStrength.SHOULD
    if _MUST_NOT_RE.search(text):
        return RuleStrength.MUST_NOT
    if _MUST_RE.search(text):
        return RuleStrength.MUST
    return RuleStrength.SHOULD


def compile_experience_to_rule(
    observation: dict[str, Any],
    *,
    requested_strength: RuleStrength | str | None = None,
    applies_when: str = "",
    exception: str = "",
) -> RuleEntry:
    """Compile a procedural observation into a ``RuleEntry``.

    Preserves strength: a ``should`` experience never upgrades to ``must`` or
    ``must_not`` even if ``requested_strength`` asks for a stronger level.
    """
    source_strength = _infer_experience_strength(observation)
    if requested_strength is None:
        final_strength = source_strength
    else:
        desired = RuleStrength(str(requested_strength).strip().lower())
        if source_strength == RuleStrength.SHOULD:
            # Never upgrade 'should' to 'must' / 'must_not'.
            final_strength = RuleStrength.SHOULD
        elif desired == RuleStrength.SHOULD:
            final_strength = RuleStrength.SHOULD
        else:
            final_strength = source_strength

    statement = str(observation.get("observation_text") or "").strip()
    if not statement:
        raise ValueError("Procedural observation has no text to compile into a rule")

    obs_id = str(observation.get("id") or "").strip()
    source = f"observation:{obs_id}" if obs_id else "experience"
    return RuleEntry(
        statement=statement,
        strength=final_strength,
        applies_when=applies_when or str(observation.get("applies_when") or ""),
        exception=exception or str(observation.get("exception") or ""),
        source=source,
    )


def _experience_has_sensitive_evidence(
    conn: Any,
    observation: dict[str, Any],
    *,
    user_id: str,
    tenant_id: str,
) -> bool:
    text = str(observation.get("observation_text") or "")
    if text and resolve_is_sensitive(None, text)[0]:
        return True

    mem_ids = json.loads(observation.get("supporting_memory_ids") or "[]")
    if mem_ids:
        slots = placeholders(len(mem_ids))
        rows = conn.execute(
            join_sql_parts(
                "SELECT is_sensitive FROM memories WHERE tenant_id = ? AND user_id = ? AND id IN (",
                slots,
                ")",
            ),
            (tenant_id, user_id, *mem_ids),
        ).fetchall()
        if any(bool(dict(r).get("is_sensitive")) for r in rows):
            return True

    ep_ids = json.loads(observation.get("supporting_episode_ids") or "[]")
    if ep_ids:
        # Fail closed: a lookup error must abort graduation, never look like
        # "no sensitive evidence".
        slots = placeholders(len(ep_ids))
        rows = conn.execute(
            join_sql_parts(
                "SELECT is_sensitive FROM episodes WHERE tenant_id = ? AND user_id = ? AND id IN (",
                slots,
                ")",
            ),
            (tenant_id, user_id, *ep_ids),
        ).fetchall()
        if any(bool(dict(r).get("is_sensitive")) for r in rows):
            return True
    return False


def _experience_has_approved_skill(conn: Any, observation_id: str, *, user_id: str, tenant_id: str) -> bool:
    rows = conn.execute(
        "SELECT metrics FROM memories WHERE tenant_id = ? AND user_id = ? "
        "AND category = 'skill' AND review_status = 'approved' AND is_ghost = 0",
        (tenant_id, user_id),
    ).fetchall()
    for row in rows:
        try:
            meta = json.loads(dict(row).get("metrics") or "{}")
        except (TypeError, ValueError):
            meta = {}
        if meta.get("observation_id") == observation_id:
            return True
    return False


def graduate_experience_to_rule(
    observation_id: str,
    *,
    user_id: str = "default",
    tenant_id: str = "default",
    options: RuleGraduateOptions | None = None,
) -> RuleEntry:
    """Graduate a reviewed, non-sensitive procedural observation into the ``rules`` block."""
    opts = options or RuleGraduateOptions()
    conn = get_db_connection()
    try:
        # Scope the lookup to the caller's lane plus the shared lane, so one
        # lane's private observation cannot be graduated through another.
        ns_frag, ns_params = _ns_clause(opts.agent_id, opts.project)
        row = conn.execute(
            join_sql_parts(
                "SELECT * FROM observations WHERE id = ? AND tenant_id = ? AND user_id = ? ",
                ns_frag,
            ),
            (observation_id, tenant_id, user_id, *ns_params),
        ).fetchone()
        if row is None:
            raise LookupError(f"Observation {observation_id!r} not found")
        obs = dict(row)

        if obs.get("status") == "archived":
            raise ValueError(f"Observation {observation_id!r} is archived and cannot graduate to a rule")

        category = str(obs.get("category") or "").strip().lower()
        memory_type = str(obs.get("memory_type") or "").strip().lower()
        pattern_key = str(obs.get("pattern_key") or "").strip().lower()
        if (
            memory_type != "procedural"
            and category not in _PROCEDURAL_RULE_CATEGORIES
            and not pattern_key.startswith("procedural:")
        ):
            raise ValueError(f"Observation {observation_id!r} is not procedural")

        if _experience_has_sensitive_evidence(conn, obs, user_id=user_id, tenant_id=tenant_id):
            raise ValueError(f"Observation {observation_id!r} carries sensitive evidence and cannot become a rule")

        if not _experience_has_approved_skill(conn, observation_id, user_id=user_id, tenant_id=tenant_id):
            raise ValueError(f"Observation {observation_id!r} must be reviewed by an approved skill before graduating")
    finally:
        conn.close()

    rule = compile_experience_to_rule(
        obs,
        requested_strength=opts.requested_strength,
        applies_when=opts.applies_when,
        exception=opts.exception,
    )
    return add_rule(
        rule,
        user_id=user_id,
        tenant_id=tenant_id,
        agent_id=opts.agent_id,
        project=opts.project,
    )
