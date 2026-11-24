"""
Public Foresight-native context block helpers.

This module provides the renamed public surface for working with continuity
blocks while reusing the existing compatibility-backed implementation.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from .block_registry import MemoryBlockSchema, get_registry, initialize_default_blocks
from .connection_pool import get_db_connection
from .subconscious import (
    DEFAULT_MEMORY_BLOCKS,
    PENDING_ITEMS,
    PROJECT_CONTEXT,
    SESSION_PATTERNS,
    USER_PREFERENCES,
    ContextBlockAgent,
    ContextBlockState,
    MemoryBlock,
    get_context_block_agent as _get_context_block_agent,
    normalize_block_line,
)

DEFAULT_CONTEXT_BLOCKS = DEFAULT_MEMORY_BLOCKS
ContextBlock = MemoryBlock

__all__ = [
    "DEFAULT_CONTEXT_BLOCKS",
    "PENDING_ITEMS",
    "PROJECT_CONTEXT",
    "SESSION_PATTERNS",
    "USER_PREFERENCES",
    "ContextBlock",
    "ContextBlockAgent",
    "ContextBlockState",
    "MemoryBlockSchema",
    "add_context_guidance",
    "add_subconscious_guidance",
    "clear_context_block",
    "clear_subconscious_block",
    "get_context_block",
    "get_context_block_agent",
    "get_context_snapshot",
    "get_context_whisper",
    "get_subconscious_block",
    "get_subconscious_context",
    "get_subconscious_whisper",
    "list_context_block_schemas",
    "list_context_blocks",
    "register_context_block_schema",
    "reset_context_block",
    "reset_subconscious_block",
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
