"""
Public Foresight-native context block helpers.

This module provides the renamed public surface for working with continuity
blocks while reusing the existing compatibility-backed implementation.
"""

from __future__ import annotations

from .block_registry import MemoryBlockSchema, get_registry, initialize_default_blocks
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


def get_context_block_agent(user_id: str, tenant_id: str = "default") -> ContextBlockAgent:
    """Return the Foresight-native context block agent facade."""
    return _get_context_block_agent(user_id, tenant_id)


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


def auto_distill_context_blocks(user_id: str, tenant_id: str = "default") -> dict[str, Any]:
    """Automatically synthesize and update context blocks from stored memories.

    Operates hands-off in the background to keep user preferences and
    project context continuously refined.
    """
    import json
    import logging
    from .subconscious import get_context_block_agent

    logger = logging.getLogger("foresight_context_distill")
    agent = get_context_block_agent(user_id, tenant_id)
    updated_blocks: list[str] = []

    try:
        from .server import search_memories, SearchOptions

        # 1. Distill Preferences / Traits
        pref_res = search_memories(
            options=SearchOptions(
                query_type="keyword",
                query="preference rule style format never always prefer workflow guideline",
                limit=15,
                min_importance=0.4,
            ),
            user_id=user_id,
        )
        pref_items = json.loads(pref_res) if isinstance(pref_res, str) else (pref_res or [])
        if pref_items and isinstance(pref_items, list):
            pref_lines = [f"- {p.get('content').strip()}" for p in pref_items if p.get("content")]
            if pref_lines:
                current_pref = agent.get_block("user_preferences") or ""
                new_pref = "\n".join(pref_lines[:8])
                if current_pref.strip() != new_pref.strip() and not current_pref.startswith("# Custom User"):
                    agent.update_block("user_preferences", new_pref)
                    updated_blocks.append("user_preferences")

        # 2. Distill Active Project Context from 'arc' and recent goals
        arc_res = search_memories(
            options=SearchOptions(
                query_type="keyword",
                query="project goal architecture stack repository task dependency build",
                limit=10,
                min_importance=0.4,
            ),
            user_id=user_id,
        )
        arc_items = json.loads(arc_res) if isinstance(arc_res, str) else (arc_res or [])
        if arc_items and isinstance(arc_items, list):
            arc_lines = [f"- {a.get('content').strip()}" for a in arc_items if a.get("content")]
            if arc_lines:
                current_proj = agent.get_block("project_context") or ""
                new_proj = "\n".join(arc_lines[:6])
                if not current_proj.strip() or current_proj.strip() == "Default project context.":
                    agent.update_block("project_context", new_proj)
                    updated_blocks.append("project_context")

    except Exception as e:
        logger.debug(f"auto_distill_context_blocks exception (non-fatal): {e}")

    return {"ok": True, "distilled_blocks": updated_blocks}
