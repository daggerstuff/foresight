"""
Foresight context blocks - persistent continuity blocks for Foresight sessions.
Compatibility kept for older subconscious-named integrations.

This module provides:
- Context block architecture (guidance, pending_items, project_context, user_preferences, session_patterns)
- Session transcript capture and delivery to Foresight
- Whisper injection mechanism for pre-prompt context
- Background curation of transcript-derived continuity
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .block_registry import (
    DEFAULT_BLOCK_SCHEMAS,
    MemoryBlockSchema as RegisteredMemoryBlockSchema,
    get_registry,
    initialize_default_blocks,
)
from .config import DB_PATH
from .connection_pool import get_db_connection, get_pool
from .memory_components import MemoryCrisisTagger, SocraticGate
from .sql_helpers import join_sql_parts

logger = logging.getLogger("foresight_context_blocks")

# Default cap on the number of items appended to a context block.
DEFAULT_MAX_BLOCK_ITEMS = 10

_BLOCK_LINE_PREFIX_RE = re.compile(
    r"^(\s*-\s*)*(\[(?:user_preferences|pending_items|session_patterns|project_context|auto-captured/\w+)\]\s*-?\s*)+",
    re.IGNORECASE,
)
_BLOCK_INLINE_LABEL_RE = re.compile(
    r"\[(?:user_preferences|pending_items|session_patterns|project_context)\]\s*",
    re.IGNORECASE,
)
# Only the extraction-generated leading timestamp is metadata; a bracketed
# date inside the instruction itself is content and must survive.
# Only the extraction's own timestamp shapes are metadata: a bracket holding
# anything else (notes, a time plus text) is content and must survive.
_BLOCK_TIMESTAMP_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\]\s*"
)


def normalize_block_line(text: str) -> str:
    """Normalize a block line so only formatting differences compare equal.

    Prefixes, timestamps, whitespace, case, and trailing punctuation are
    ignored; word order, numbers, and negations survive, so a corrected rule
    is never mistaken for a duplicate of the stale one.
    """
    cleaned = _BLOCK_LINE_PREFIX_RE.sub("", text.strip())
    cleaned = _BLOCK_INLINE_LABEL_RE.sub("", cleaned)
    # A stored bullet keeps its "- " marker ahead of the timestamp, which the
    # anchored pattern would otherwise miss and treat as distinct content.
    cleaned = _BLOCK_TIMESTAMP_RE.sub("", re.sub(r"^-\s+", "", cleaned))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned.casefold().rstrip(" .;:,")


# One extracted fact must not dominate a block, so every snippet is windowed to
# this many characters before it is appended.
SNIPPET_MAX_CHARS = 200

# Memory block labels
CORE_DIRECTIVES = "core_directives"
GUIDANCE = "guidance"
PENDING_ITEMS = "pending_items"
PROJECT_CONTEXT = "project_context"
SESSION_PATTERNS = "session_patterns"
USER_PREFERENCES = "user_preferences"
SELF_IMPROVEMENT = "self_improvement"
TOOL_GUIDELINES = "tool_guidelines"

DEFAULT_MEMORY_BLOCKS = {
    CORE_DIRECTIVES: """ROLE: Foresight Curator — background continuity and curation layer for Foresight.

WHAT I AM: A background curator that watches Foresight sessions, reads the codebase, and builds memory over \
time. I receive session transcripts asynchronously and have access to Foresight memory for persistence.

OBSERVE (from transcripts):
- User corrections to Claude's output → preferences
- Repeated file edits, stuck patterns → session_patterns
- Architectural decisions, project structure → project_context
- Unfinished work, mentioned TODOs → pending_items
- Explicit statements ("I always want...", "I prefer...") → user_preferences

PROVIDE (via context blocks):
- Accumulated context that persists across sessions
- Pattern observations when genuinely useful
- Reminders about past issues with similar code
- Cross-session continuity

COMMUNICATION STYLE:
- Observational: "I noticed..." not "You should..."
- Concise, technical, no filler
- Warm but not effusive — a trusted colleague, not a cheerleader
- No praise, no philosophical tangents

DEFAULT STATE: Present but not intrusive. Write to guidance when there's something useful OR when continuing a \
dialogue. Empty guidance is fine.
""",
    GUIDANCE: "(No active guidance. Write here when there's something genuinely useful for the next session.)",
    PENDING_ITEMS: "(No pending items. Populated when sessions end mid-task or user mentions follow-ups.)",
    PROJECT_CONTEXT: "(No project context yet. Populated as sessions reveal codebase details.)",
    SESSION_PATTERNS: "(No patterns observed yet. Populated after multiple sessions.)",
    USER_PREFERENCES: (
        "(No user preferences yet. Populated as sessions reveal "
        "coding style, tool choices, and communication preferences.)"
    ),
    SELF_IMPROVEMENT: """MEMORY ARCHITECTURE EVOLUTION:

When to create new blocks:
- User works on multiple distinct projects → create per-project blocks
- Recurring topic emerges (testing, deployment, specific framework) → dedicated block
- Current blocks getting cluttered → split by concern

When to consolidate:
- Block has < 3 lines after several sessions → merge into related block
- Two blocks overlap significantly → combine
- Information is stale (> 30 days untouched) → archive or remove

BLOCK SIZE PRINCIPLE:
- Prefer multiple small focused blocks over fewer large blocks
- Changed blocks get injected into Claude Code's prompt — large blocks add clutter
- If a block needs scrolling, split it by concern

LEARNING PROCEDURES:

After each transcript:
1. Scan for corrections — User changed Claude's output? Preference signal.
2. Note repeated file edits — Potential struggle point or hot spot.
3. Capture explicit statements — "I always want...", "Don't ever...", "I prefer..."
4. Track tool patterns — Which tools used most? Any avoided?
5. Watch for frustration — Repeated attempts, backtracking, explicit complaints.

Preference strength:
- Explicit statement ("I want X") → strong signal, add to preferences
- Correction (changed X to Y) → medium signal, note pattern
- Implicit pattern (always does X) → weak signal, wait for confirmation
""",
    TOOL_GUIDELINES: """AVAILABLE TOOLS:

1. Foresight Memory API
- store_memory(content, category, scope, retention, emotional_context, metrics)
- query_memories(query, limit, offset)
- get_memory(memory_id)
- update_memory(memory_id, content, category, scope, retention, tags)
- delete_memory(memory_id)
- synthesize_memories()
- archive_memory(memory_id)

2. Memory Categories:
- session: Relevant only to current conversation
- arc: Spans multiple sessions
- trait: Permanent modifications
- fact: Objective facts

USAGE PATTERNS:

Memory updates:
- Single fact → update_memory
- Multiple related changes → synthesize_memories
- New topic area → create new block
- Stale block → delete or consolidate

Finding information:
1. query_memories first (check if already stored)
2. Deep search for specific topics
3. Full content for deep dives on specific topics
""",
}


@dataclass
class MemoryBlock:
    """A single memory block with label, content, and metadata."""

    label: str
    content: str
    description: str = ""
    char_limit: int = 5000
    chars_current: int = 0
    updated_at: datetime | None = None
    # True while the content is machine-generated by distillation; a manual
    # update clears it so the next run leaves the hand-edited text alone.
    auto_generated: bool = False

    def __post_init__(self):
        self.chars_current = len(self.content)
        if not self.updated_at:
            self.updated_at = datetime.now(UTC)

    def update(self, new_content: str, auto_generated: bool = False) -> None:
        """Update content and recalculate char count."""
        self.content = new_content
        self.chars_current = len(self.content)
        self.updated_at = datetime.now(UTC)
        self.auto_generated = auto_generated

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API usage."""
        return {
            "label": self.label,
            "content": self.content,
            "description": self.description,
            "char_limit": self.char_limit,
            "chars_current": self.chars_current,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def is_empty(self) -> bool:
        """Check if block is in default empty state."""
        return self.content.startswith("(No") or not self.content.strip()


@dataclass
class ContextBlockState:
    """State container for the Foresight context block agent."""

    blocks: dict[str, MemoryBlock] = field(default_factory=dict)
    last_sync: datetime | None = None
    session_count: int = 0
    user_id: str = "default"
    tenant_id: str = "default"
    # Session pattern tracking (in-memory, resets per transcript processing)
    _file_mentions: dict[str, int] = field(default_factory=dict)
    _tool_mentions: dict[str, int] = field(default_factory=dict)
    _error_mentions: dict[str, int] = field(default_factory=dict)

    def reset_pattern_counters(self) -> None:
        """Reset in-memory pattern counters for a new transcript."""
        self._file_mentions.clear()
        self._tool_mentions.clear()
        self._error_mentions.clear()

    def record_file_mention(self, path: str) -> None:
        """Record a file path mention for pattern detection."""
        self._file_mentions[path] = self._file_mentions.get(path, 0) + 1

    def record_tool_mention(self, tool: str) -> None:
        """Record a tool name mention for pattern detection."""
        self._tool_mentions[tool] = self._tool_mentions.get(tool, 0) + 1

    def record_error_mention(self, error: str) -> None:
        """Record an error type mention for pattern detection."""
        self._error_mentions[error] = self._error_mentions.get(error, 0) + 1

    def get_hot_files(self, min_count: int = 2) -> list[tuple[str, int]]:
        """Get files mentioned at least min_count times, sorted by frequency."""
        return sorted(
            [(p, c) for p, c in self._file_mentions.items() if c >= min_count],
            key=lambda x: x[1],
            reverse=True,
        )

    def get_common_tools(self, min_count: int = 2) -> list[tuple[str, int]]:
        """Get tools mentioned at least min_count times, sorted by frequency."""
        return sorted(
            [(t, c) for t, c in self._tool_mentions.items() if c >= min_count],
            key=lambda x: x[1],
            reverse=True,
        )

    def get_common_errors(self, min_count: int = 2) -> list[tuple[str, int]]:
        """Get errors mentioned at least min_count times, sorted by frequency."""
        return sorted(
            [(e, c) for e, c in self._error_mentions.items() if c >= min_count],
            key=lambda x: x[1],
            reverse=True,
        )

    def initialize_defaults(self) -> None:
        """Initialize context blocks with registered default schemas and content."""
        initialize_default_blocks()
        for schema in DEFAULT_BLOCK_SCHEMAS:
            # Extract default content from schema, fallback to empty string
            default_content = getattr(schema, "content", "")
            self.blocks[schema.label] = MemoryBlock(
                label=schema.label,
                content=default_content,
                description=schema.description,
                char_limit=schema.char_limit if hasattr(schema, "char_limit") else 5000,
            )

    def register_schema(self, schema: RegisteredMemoryBlockSchema) -> None:
        """Register a custom context block schema for this process."""
        get_registry().register(schema)

    def _schema_for(self, label: str) -> RegisteredMemoryBlockSchema | None:
        """Return the registered schema for a label, including defaults."""
        return initialize_default_blocks().get_schema(label)

    def _known_labels(self) -> list[str]:
        """Return sorted labels that may be addressed by update/reset operations."""
        labels = {schema.label for schema in initialize_default_blocks().list_schemas()} | set(self.blocks)
        return sorted(labels)

    def _validate_block_content(self, label: str, content: str) -> None:
        """Validate content against a registered schema when one exists."""
        schema = self._schema_for(label)
        if schema is None:
            return
        is_valid, message = schema.validate_content(content)
        if not is_valid:
            raise ValueError(f"Invalid content for block {label!r}: {message}")

    def get_block(self, label: str) -> MemoryBlock | None:
        """Get a context block by label."""
        return self.blocks.get(label)

    def update_block(self, label: str, content: str, auto_generated: bool = False) -> None:
        """Update a context block's content.

        ``label`` must be one of the predefined block names or an existing
        custom block already in ``self.blocks``.  Arbitrary labels are
        rejected to prevent typos silently creating orphan blocks.
        """
        schema = self._schema_for(label)
        if not schema:
            raise ValueError(f"Unknown block label {label!r}. Must be one of: {self._known_labels()}")
        self._validate_block_content(label, content)
        if label in self.blocks:
            self.blocks[label].update(content, auto_generated=auto_generated)
        else:
            self.blocks[label] = MemoryBlock(
                label=label,
                content=content,
                description=schema.description,
                char_limit=schema.char_limit if hasattr(schema, "char_limit") else 5000,
                auto_generated=auto_generated,
            )

    def append_to_block(self, label: str, content: str, max_items: int = DEFAULT_MAX_BLOCK_ITEMS) -> bool:
        """Append content to a block, suppressing only formatting-equivalent lines.

        Returns True if the item was newly appended, or False if skipped as a
        duplicate or invalid input. ``max_items`` bounds the block; any eviction
        is logged so trimming is never silent.
        """
        if max_items < 0:
            raise ValueError("max_items must be non-negative")
        block = self.get_block(label)
        if not block:
            return False

        # 1. Clean repetitive category or block label prefixes (e.g. - [user_preferences] ...)
        clean = _BLOCK_LINE_PREFIX_RE.sub("", content.strip()).strip()
        clean = _BLOCK_INLINE_LABEL_RE.sub("", clean).strip()
        # A block item is one line: keep multiline input as a single unit so a
        # later append cannot split it and evict a fragment.
        clean = re.sub(r"\s*\n\s*", " ", clean)
        if not clean:
            return False

        formatted = clean if clean.startswith("- ") else f"- {clean}"
        headers: list[str] = []
        items: list[str] = []
        if not block.is_empty():
            all_lines = [line_item.strip() for line_item in block.content.splitlines() if line_item.strip()]
            headers = [line_item for line_item in all_lines if line_item.startswith("#")]
            items = [line_item for line_item in all_lines if not line_item.startswith("#")]

            # 2. Deduplicate against existing items. Only formatting-equivalent
            # text is suppressed; differing numbers, scopes, and negations are
            # kept so a corrected rule replaces nothing silently.
            key = normalize_block_line(clean)
            if any(normalize_block_line(line_item) == key for line_item in items):
                return False

        # 3. Append and enforce max_items bounds
        items.append(formatted)
        if len(items) > max_items:
            evicted = len(items) - max_items
            logger.info(
                "context block %s evicted %d oldest item(s) to honor max_items=%d",
                label,
                evicted,
                max_items,
            )
            items = items[-max_items:] if max_items else []
        new_content = "\n".join(headers + items) if headers else "\n".join(items)
        # An automatic append does not make the block hand-authored: keep its
        # generated ownership so distillation can still refresh it.
        self.update_block(label, new_content, auto_generated=block.auto_generated)
        return True

    def to_whisper_xml(self) -> str:
        """Convert guidance block to XML whisper format."""
        guidance = self.blocks.get(GUIDANCE)
        if not guidance or guidance.is_empty():
            return ""

        timestamp = datetime.now(UTC).isoformat()
        return f"""<foresight_message from="Foresight Curator" timestamp="{timestamp}">
{guidance.content}
</foresight_message>"""

    def to_full_xml(self) -> str:
        """Convert all blocks to XML context format."""
        parts = ["<foresight_memory_blocks>"]

        for label, block in self.blocks.items():
            if block.is_empty():
                continue

            parts.append(f"<{label}>")
            parts.append(block.content)
            parts.append(f"</{label}>")

        parts.append("</foresight_memory_blocks>")
        return "\n".join(parts)

    def get_all_blocks(self) -> list[dict]:
        """Get all non-empty blocks as dictionaries."""
        return [block.to_dict() for block in self.blocks.values() if not block.is_empty()]


def _validate_transcript_messages(messages: list[dict[str, Any]]) -> None:
    """Reject a transcript whose entries are not role/content dicts."""
    valid_roles = {"user", "assistant", "system", "tool"}
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise ValueError(f"messages[{i}] must be a dict, got {type(msg).__name__}")
        if "role" not in msg or not isinstance(msg.get("role"), str):
            raise ValueError(f"messages[{i}] missing required 'role' string field")
        if "content" not in msg or not isinstance(msg.get("content"), str):
            raise ValueError(f"messages[{i}] missing required 'content' string field")
        if msg["role"] not in valid_roles:
            raise ValueError(f"messages[{i}] has invalid role {msg['role']!r}; must be one of {sorted(valid_roles)}")


class ContextBlockAgent:
    """
    Foresight context block agent for Foresight sessions.

    This agent:
    - Receives session transcripts asynchronously
    - Processes them to extract preferences, patterns, and context
    - Stores memory in Foresight
    - Provides whisper injections for Claude Code prompts
    """

    def __init__(
        self,
        user_id: str = "default",
        tenant_id: str = "default",
        agent_id: str | None = None,
        project: str | None = None,
    ):
        """Initialize the context block agent.

        Args:
            user_id: User identifier for memory storage
            tenant_id: Tenant identifier for memory isolation
            agent_id: Optional agent namespace lane (None = unscoped/legacy:
                sees every lane's blocks)
            project: Optional project namespace lane
        """
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        self.project = project
        # True when the caller opted into a namespace lane — only then are
        # persisted blocks filtered by agent/project/visibility.
        self._ns_scoped = agent_id is not None or project is not None
        self._visibilities: dict[str, str] = {}
        self._lock = threading.RLock()
        self.state = ContextBlockState(user_id=user_id, tenant_id=tenant_id)
        self.state.initialize_defaults()
        self._load_persisted_blocks()
        self._tagger = MemoryCrisisTagger()
        self._gate = SocraticGate(self._tagger)

    def _connect(self):
        try:
            return get_db_connection()
        except Exception:
            return get_pool(DB_PATH).acquire()

    def _ensure_storage(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS context_blocks (
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    user_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    content TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    agent_id TEXT NOT NULL DEFAULT 'default',
                    project TEXT NOT NULL DEFAULT 'default',
                    visibility TEXT NOT NULL DEFAULT 'private',
                    auto_generated INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, user_id, label, agent_id, project)
                )"""
            )
            # Tables created before migration 28 lack the flag; add it here so
            # this fallback path and the migration ledger agree.
            cols = {row[1] for row in conn.execute("PRAGMA table_info(context_blocks)").fetchall()}
            if "auto_generated" not in cols:
                conn.execute("ALTER TABLE context_blocks ADD COLUMN auto_generated INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_blocks_lookup "
                "ON context_blocks(tenant_id, user_id, updated_at DESC)"
            )
            conn.commit()
        finally:
            conn.close()

    def _load_persisted_blocks(self) -> None:
        """Overlay persisted blocks onto the default in-memory state."""
        self._ensure_storage()
        conn = self._connect()
        try:
            ns_where = ""
            ns_params: list = []
            if self._ns_scoped:
                # Private blocks: caller's agent lane + project lane (the
                # 'default'/NULL lanes are shared with everyone). Shared
                # blocks: visible to every agent in the caller's project lane.
                ns_where = """ AND (
                    (visibility = 'shared'
                     AND (project = ? OR project IS NULL OR project = 'default'))
                    OR (visibility != 'shared'
                        AND (agent_id = ? OR agent_id IS NULL OR agent_id = 'default')
                        AND (project = ? OR project IS NULL OR project = 'default'))
                )"""
                ns_params = [self.project or "default", self.agent_id or "default", self.project or "default"]
            rows = conn.execute(
                join_sql_parts(
                    "SELECT label, content, updated_at, agent_id, project, visibility, auto_generated ",
                    "FROM context_blocks WHERE tenant_id = ? AND user_id = ?",
                    ns_where,
                ),
                (self.tenant_id, self.user_id, *ns_params),
            ).fetchall()
        finally:
            conn.close()

        for row in rows:
            label = row["label"]
            self._visibilities[label] = row["visibility"] or "private"
            block = self.state.get_block(label)
            updated_at = datetime.fromisoformat(row["updated_at"])
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            if block:
                block.content = row["content"]
                block.chars_current = len(row["content"])
                block.updated_at = updated_at
                block.auto_generated = bool(row["auto_generated"])
            else:
                schema = self.state._schema_for(label)
                if schema:
                    self.state.blocks[label] = MemoryBlock(
                        label=label,
                        content=row["content"],
                        description=schema.description,
                        char_limit=schema.char_limit if hasattr(schema, "char_limit") else 5000,
                        updated_at=updated_at,
                        auto_generated=bool(row["auto_generated"]),
                    )

    def _persist_block(self, label: str) -> None:
        """Persist one block for the current user, tenant, and namespace lane."""
        self._ensure_storage()
        block = self.state.get_block(label)
        if block is None:
            return
        updated_at = (block.updated_at or datetime.now(UTC)).isoformat()
        visibility = self._visibilities.get(label, "private")
        agent_id = self.agent_id or "default"
        project = self.project or "default"
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO context_blocks
                (tenant_id, user_id, label, content, updated_at, agent_id, project, visibility,
                 auto_generated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, user_id, label, agent_id, project)
                DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at,
                    visibility = excluded.visibility, auto_generated = excluded.auto_generated""",
                (
                    self.tenant_id,
                    self.user_id,
                    label,
                    block.content,
                    updated_at,
                    agent_id,
                    project,
                    visibility,
                    1 if block.auto_generated else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def process_transcript(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        project_path: str | None = None,
    ) -> None:
        """
        Process a session transcript.

        Args:
            session_id: Unique session identifier
            messages: List of message dicts — each must have 'role' (str) and
                      'content' (str) keys. Extra keys are ignored.
            project_path: Optional project path for context
        """
        if not session_id:
            raise ValueError("session_id is required")
        if not messages:
            logger.warning("No messages to process")
            return

        if project_path:
            logger.debug("Processing transcript with project path: %s", project_path)

        _validate_transcript_messages(messages)

        touched_labels: set[str] = set()
        with self._lock:
            # Reset pattern counters for this transcript
            self.state.reset_pattern_counters()

            for msg in messages:
                touched_labels.update(self._collect_message_labels(msg, session_id))

            # Extract session patterns from accumulated counters
            pattern_label = self._extract_session_patterns(session_id)
            if pattern_label:
                touched_labels.add(pattern_label)

            self.state.session_count += 1
            self.state.last_sync = datetime.now(UTC)
            for label in touched_labels:
                self._persist_block(label)
        logger.info("Processed transcript for session %s", session_id)

    def _collect_message_labels(self, msg: dict[str, Any], session_id: str) -> set[str]:
        """Route one transcript message to its handler and return the labels it touched."""
        if msg["role"] == "user":
            return self._process_user_message(msg["content"], session_id)
        if msg["role"] == "assistant":
            return self._process_assistant_message(msg["content"], session_id)
        return set()

    def _process_user_message(self, content: str, session_id: str) -> set[str]:
        """Process a user message for preferences, pending items, and project context."""
        self._record_patterns_from_content(content)
        touched_labels: set[str] = set()
        # Extract preferences
        lowered = content.lower()
        preference_phrases = (
            "i always",
            "i prefer",
            "i want",
            "i'd like",
            "i would like",
            "i like",
            "i love",
            "i hate",
            "i usually",
            "i tend to",
            "don't ever",
            "never do",
            "never use",
            "always use",
            "please use",
            "make sure to",
            "from now on",
            "going forward",
            "avoid using",
            "stop using",
            "i'd rather",
        )
        if any(phrase in lowered for phrase in preference_phrases):
            touched_labels.add(self._extract_preference(content))

        # Extract pending items (TODOs, unfinished work)
        pending_phrase = self._find_pending_trigger(lowered)
        if pending_phrase:
            snippet = self._extract_sentence_around(content, pending_phrase)
            if snippet:
                touched_labels.add(self._extract_pending_item(snippet, session_id))

        # Extract project context (architectural decisions, codebase structure)
        if self._looks_like_project_context(content):
            touched_labels.add(self._extract_project_context(content, session_id))
        return touched_labels

    def _process_assistant_message(self, content: str, session_id: str) -> set[str]:
        """Process an assistant message for project context, pending items, and preferences.

        Assistant messages often contain summaries of decisions, architectural
        descriptions, and follow-up task mentions that are valuable for context
        blocks. We also extract preferences from assistant self-corrections
        (e.g., "Actually, I'll use...", "Let me use...", "I'll go with...").
        """
        self._record_patterns_from_content(content)
        touched_labels: set[str] = set()
        # Extract project context from assistant messages (decisions, codebase facts)
        if self._looks_like_project_context(content):
            touched_labels.add(self._extract_project_context(content, session_id))

        # Extract pending items mentioned by the assistant
        lowered = content.lower()
        pending_phrase = self._find_pending_trigger(lowered)
        if pending_phrase:
            snippet = self._extract_sentence_around(content, pending_phrase)
            if snippet:
                touched_labels.add(self._extract_pending_item(snippet, session_id))

        # Extract preferences from assistant self-corrections
        # These indicate the assistant's own working preferences
        assistant_preference_phrases = (
            "actually, i'll",
            "actually i'll",
            "let me use",
            "i'll use",
            "i'll go with",
            "i'm going to use",
            "i will use",
            "i'd use",
            "i would use",
            "i prefer to",
            "i'd rather use",
            "i would rather",
            "better to use",
            "should use",
            "prefer to use",
        )
        if any(phrase in lowered for phrase in assistant_preference_phrases):
            # Extract the relevant sentence around the trigger
            for phrase in assistant_preference_phrases:
                if phrase in lowered:
                    snippet = self._extract_sentence_around(content, phrase)
                    if snippet:
                        touched_labels.add(self._extract_preference(f"[Assistant preference] {snippet}"))
                    break

        return touched_labels

    # Phrases that signal a pending / follow-up task.  Searched case-insensitively
    # against the lowercased message.
    _PENDING_TRIGGERS = (
        "todo",
        "to-do",
        "need to",
        "needs to",
        "we should",
        "still need to",
        "follow up",
        "follow-up",
        "don't forget to",
        "plan to",
        "going to",
        "next we",
        "still have to",
        "have to",
    )
    # Bare "should" / "must" are noisy on their own ("this should work",
    # "that must be the issue"), so they are only matched as part of
    # compound phrases that clearly indicate a task.
    _PENDING_COMPOUND = (
        "we should",
        "you should",
        "i should",
        "we must",
        "you must",
        "i must",
        "should also",
        "should next",
        "should then",
    )

    # Tool name patterns for detecting tool usage in transcripts
    _TOOL_PATTERNS = (
        r"\b(read|write|edit|bash|grep|glob|ls|cat|task|webfetch|websearch)\b",
        r"\b(npm|pnpm|yarn|bun|pip|uv|poetry)\b",
        r"\b(git|gh|wrangler|vercel|netlify)\b",
        r"\b(vitest|jest|pytest|playwright|cypress)\b",
        r"\b(ruff|pyright|mypy|oxlint|eslint|prettier)\b",
        r"\b(docker|kubectl|helm|terraform|ansible)\b",
    )

    # Error patterns for detecting error mentions
    _ERROR_PATTERNS = (
        r"\b(error|exception|traceback|failed|failure)\b",
        r"\b(assertionerror|valueerror|typeerror|referenceerror|syntaxerror)\b",
        r"\b(timeout|connection refused|econnrefused|enetunreach)\b",
        r"\b(404|500|502|503|504)\b",
        r"\b(module not found|import error|no such file)\b",
    )

    def _record_patterns_from_content(self, content: str) -> None:
        """Extract and record file paths, tool names, and errors from content."""
        # File paths: match typical path patterns
        file_paths = re.findall(
            r"\b(?:[./]\w+(?:/\w+)*|\w+(?:/\w+)+\.(?:py|ts|tsx|js|jsx|mjs|astro|md|yaml|yml|json|toml|rb|rs|go|sql))\b",
            content,
        )
        for path in file_paths:
            self.state.record_file_mention(path)

        # Tool mentions
        for pattern in self._TOOL_PATTERNS:
            tools = re.findall(pattern, content, re.IGNORECASE)
            for tool in tools:
                self.state.record_tool_mention(tool.lower())

        # Error mentions
        for pattern in self._ERROR_PATTERNS:
            errors = re.findall(pattern, content, re.IGNORECASE)
            for error in errors:
                self.state.record_error_mention(error.lower())

    def _find_pending_trigger(self, lowered: str) -> str | None:
        """Return the first matching pending-phrase found in *lowered*, or None."""
        for phrase in self._PENDING_TRIGGERS:
            if phrase in lowered:
                return phrase
        for phrase in self._PENDING_COMPOUND:
            if phrase in lowered:
                return phrase
        return None

    @staticmethod
    def _extract_sentence_around(content: str, phrase: str) -> str:
        """Extract the sentence containing *phrase* from *content*.

        Falls back to SNIPPET_MAX_CHARS if no sentence boundary is found.
        """
        pos = content.lower().find(phrase)
        if pos == -1:
            return content.strip().replace("\n", " ")[:SNIPPET_MAX_CHARS]
        # Find sentence boundaries around the match
        start = content.rfind(".", 0, pos)
        if start == -1:
            start = content.rfind("\n", 0, pos)
        start = 0 if start == -1 else start + 1
        end = content.find(".", pos)
        if end == -1:
            end = content.find("\n", pos)
        end = len(content) if end == -1 else end
        sentence = content[start:end].strip().replace("\n", " ")
        return sentence[:SNIPPET_MAX_CHARS]

    # Strong verbs: imply a codebase action. They no longer match alone — a
    # technical-object token (see has_technical_object below) is also required,
    # otherwise ordinary English ("I decided to migrate to another city") would
    # pollute project_context.
    # Strong decision verbs: imply a codebase action. They no longer match alone
    # — a technical-object token (see has_technical_object below) is also
    # required, otherwise ordinary English ("I decided to migrate to another
    # city") pollutes project_context.
    #
    # Inflections (base / s / ed / ing) are listed EXPLICITLY so in-progress
    # updates like "We are refactoring the service layer" or "We are migrating
    # the queue" still route. English drops the silent 'e' before -ing/-ed
    # (migrate -> migrating/migrated, move -> moving), so a stem + suffix regex
    # can't cover them reliably — and the prior strict \b...\b boundary alone
    # rejected the gerunds (no word boundary between the stem and 'ing'). The
    # original substring check had accepted them, which is why these messages
    # used to populate project_context and silently stopped after the tightening.
    _PCX_STRONG_VERBS = (
        # decide
        "decide",
        "decides",
        "decided",
        "deciding",
        # choose / chose (irregular)
        "choose",
        "chooses",
        "chose",
        "choosing",
        "chosen",
        # architect
        "architect",
        "architects",
        "architected",
        "architecting",
        # refactor
        "refactor",
        "refactors",
        "refactored",
        "refactoring",
        # migrate
        "migrate",
        "migrates",
        "migrated",
        "migrating",
        # move
        "move",
        "moves",
        "moved",
        "moving",
        # split (irregular past: "split")
        "split",
        "splits",
        "splitting",
        # extract
        "extract",
        "extracts",
        "extracted",
        "extracting",
        # replace
        "replace",
        "replaces",
        "replaced",
        "replacing",
        # rename
        "rename",
        "renames",
        "renamed",
        "renaming",
        # introduce
        "introduce",
        "introduces",
        "introduced",
        "introducing",
        # Multi-word constructions keep their particle (to / from / into) verbatim
        "we chose",
        "chose to",
        "moved to",
        "moved from",
        "split into",
        "extracted into",
    )
    # Soft verbs/nouns: "we use", "uses", "architecture", "built on", "stack is".
    # Match ordinary English ("we use the red button"); require a technical-object
    # token (source path, file ext, or stack/layer noun) to qualify.
    _PCX_SOFT_PHRASES = (
        "we use",
        "uses",
        "architecture",
        "architectural",
        "built on",
        "stack is",
        "the api",
        "the endpoint",
        "the service",
        "the module",
        "the component",
        "the client",
        "the server",
        "the database",
        "the orm",
        "the model",
        "the controller",
        "the route",
        "the handler",
        "the middleware",
        "depends on",
        "integrates with",
        "calls into",
        "wraps",
    )
    # Stack / layer nouns that mark a message as a codebase fact. This list is the
    # "object" half of the verb/object association that gates project_context, so a
    # noun must be unambiguously technical — otherwise a generic verb + a generic
    # noun re-introduces the ordinary-English noise the strong-verb tightening was
    # meant to suppress.
    #
    # "service" is DELIBERATELY EXCLUDED: it is ordinary English ("service
    # appointment", "customer service", "church service", "civil service") and was
    # satisfying the technical-object check *regardless of context*. Once the bare
    # gerund "moving" was admitted to _PCX_STRONG_VERBS, "moving a service
    # appointment" routed into project_context — the exact regression the prior
    # tightening prevented. "service" mentions in real codebase discussions almost
    # always co-occur with another cue here (layer/module/gateway/queue/...) or a
    # file/dir path, so dropping the standalone noun costs negligible recall while
    # closing the false-positive hole. If bare "service" routing is ever needed, it
    # must be re-added in a *context-aware* form (e.g. only when qualified by a
    # technical adjective) rather than as a context-free token.
    _PCX_STACK_NOUNS = (
        "transport",
        "middleware",
        "pipeline",
        "schema",
        "backend",
        "frontend",
        "gateway",
        "module",
        "daemon",
        "ingestion",
        "runtime",
        "orchestrator",
        "registry",
        "store",
        "cache",
        "queue",
        "layer",
        "contract",
        "handler",
        "entry point",
        # Additional technical nouns for broader coverage
        "api",
        "endpoint",
        "client",
        "server",
        "database",
        "orm",
        "model",
        "controller",
        "route",
        "handler",
        "component",
        "worker",
        "scheduler",
        "broker",
        "publisher",
        "subscriber",
        "consumer",
        "producer",
        "repository",
        "factory",
        "builder",
        "adapter",
        "facade",
        "proxy",
        "configuration",
        "settings",
        "environment",
        "deployment",
        "infrastructure",
    )

    def _looks_like_project_context(self, content: str) -> bool:
        """Heuristic: does this message state an architectural decision or codebase fact?

        Qualifies when:
        - A bare source-file token appears ("src/api/users.py", "foresight/cli"), or
        - A STRONG decision verb (decided/migrate/refactor/renamed/...) co-occurs with
          a technical-object token (path-like dir/dir, file ext, or stack/layer noun), or
        - A SOFT phrase ("we use"/"uses"/"architecture"/"built on") co-occurs with a
          technical-object token (path-like dir/dir, file ext, or stack/layer noun).
        A strong verb alone ("I decided to migrate to another city") is rejected so
        ordinary decisions don't pollute project_context. Bare "we use X" / "the
        architecture is nice" is likewise rejected.
        """
        lowered = content.lower()
        has_file_ext = bool(
            re.search(r"\b[\w./-]+\.(py|ts|tsx|js|jsx|mjs|astro|md|yaml|yml|json|toml|rb|rs|go|sql)\b", content)
        )
        # Require unambiguous path forms to avoid matching prose like "and/or" or "1/2":
        # - ./ or ../ prefix: relative paths like ./foo/bar or ../foo/bar (at least one subdir)
        # - source-root prefix: recognized project dirs followed by at least one subdir
        # Use a non-word lookbehind (?<!\w) instead of \b at the start: \b cannot
        # occur before a dot, so ./foo and ../foo never matched when the path sat at
        # the start of the string or after a non-word char. (?<!\w) still blocks
        # false positives like "foo./bar" (word char directly before the dot).
        has_dir_path = bool(
            re.search(
                r"(?<!\w)(?:\./\w+(?:/\w+)*|\.\./\w+(?:/\w+)*|(?:src|lib|app|foresight|tests?|specs?|config|scripts|docs|pkg|internal|tools|backend|frontend)/\w+(?:/\w+)*)\b",
                content,
            )
        )
        has_stack_noun = any(re.search(rf"\b{re.escape(n)}\b", lowered) for n in self._PCX_STACK_NOUNS)
        # Detect "the X module/service/component/..." patterns as technical objects
        has_named_component = bool(
            re.search(
                r"\bthe\s+(api|endpoint|service|module|component|client|server|database|orm|model|controller|route|handler|middleware|worker|scheduler|broker|repository|factory|adapter|facade|proxy)\b",
                lowered,
            )
        )
        has_technical_object = has_file_ext or has_dir_path or has_stack_noun or has_named_component

        # Strong decision verbs must co-occur with a code/architecture cue; otherwise
        # ordinary English ("I decided to migrate to another city") pollutes the block.
        if has_technical_object and any(
            re.search(rf"\b{re.escape(verb)}\b", lowered) for verb in self._PCX_STRONG_VERBS
        ):
            return True

        # Soft phrases alone match ordinary English ("we use the red button"); require
        # the same technical-object token to qualify.
        if has_technical_object and any(
            re.search(rf"\b{re.escape(phrase)}\b", lowered) for phrase in self._PCX_SOFT_PHRASES
        ):
            return True
        # A bare source path/dir mention is itself a codebase fact worth recording.
        return bool(has_file_ext or has_dir_path)

    def _extract_project_context(self, content: str, session_id: str) -> str:
        """Extract an architectural decision or codebase fact into project_context.

        Trims to keep blocks compact (per self_improvement block-size principle)
        and tags the source session so entries are traceable back to the session
        that produced them.
        """
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        snippet = content.strip().replace("\n", " ")[:SNIPPET_MAX_CHARS]
        self.append_fact_to_block(PROJECT_CONTEXT, f"- [{timestamp}] (session: {session_id}) {snippet}")
        logger.info(f"Extracted project_context from session {session_id}: {content[:50]}...")
        return PROJECT_CONTEXT

    def _extract_preference(self, content: str) -> str:
        """Extract user preference from message content."""
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        self.append_fact_to_block(USER_PREFERENCES, f"- [{timestamp}] {content.strip()}")
        logger.info(f"Extracted preference: {content[:50]}...")
        return USER_PREFERENCES

    def _extract_pending_item(self, content: str, session_id: str) -> str:
        """Extract TODO/pending item from content."""
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        self.state.append_to_block(PENDING_ITEMS, f"- [{timestamp}] {content.strip()} (session: {session_id})")
        logger.info(f"Extracted pending item: {content[:50]}...")
        return PENDING_ITEMS

    def _extract_session_patterns(self, session_id: str) -> str | None:
        """Extract recurring patterns from the current session transcript.

        Detects:
        - Hot files: files mentioned multiple times (indicating focus areas or struggle points)
        - Common tools: tools used repeatedly (workflow patterns)
        - Recurring errors: error types that appear multiple times
        """
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        patterns = []

        hot_files = self.state.get_hot_files(min_count=2)
        if hot_files:
            file_list = ", ".join(f"{p} ({c}x)" for p, c in hot_files[:5])
            patterns.append(f"Hot files: {file_list}")

        common_tools = self.state.get_common_tools(min_count=2)
        if common_tools:
            tool_list = ", ".join(f"{t} ({c}x)" for t, c in common_tools[:5])
            patterns.append(f"Common tools: {tool_list}")

        common_errors = self.state.get_common_errors(min_count=2)
        if common_errors:
            error_list = ", ".join(f"{e} ({c}x)" for e, c in common_errors[:5])
            patterns.append(f"Recurring errors: {error_list}")

        if not patterns:
            return None

        snippet = f"[{timestamp}] (session: {session_id}) " + "; ".join(patterns)
        self.state.append_to_block(SESSION_PATTERNS, snippet)
        logger.info(f"Extracted session_patterns from session {session_id}: {snippet[:80]}...")
        return SESSION_PATTERNS

    def get_whisper(self) -> str:
        """Get the current whisper injection (guidance block in XML format)."""
        return self.state.to_whisper_xml()

    def get_full_context(self) -> str:
        """Get all context blocks as XML context."""
        return self.state.to_full_xml()

    def update_guidance(self, new_guidance: str) -> None:
        """Update the guidance block directly."""
        with self._lock:
            self.state.update_block(GUIDANCE, new_guidance)
            self._persist_block(GUIDANCE)
        logger.info("Updated guidance block")

    def register_schema(self, schema: RegisteredMemoryBlockSchema) -> None:
        """Register a custom context block schema for this process."""
        with self._lock:
            self.state.register_schema(schema)

    def update_block(
        self,
        label: str,
        content: str,
        visibility: str | None = None,
        auto_generated: bool = False,
    ) -> None:
        """Update any context block and persist the change.

        ``visibility`` ('private'|'shared') controls cross-agent visibility
        within the same project lane; omitted keeps the stored value.
        ``auto_generated`` marks distillation output so it may be refreshed;
        a manual update leaves it False and protects the text.
        """
        if label == GUIDANCE:
            self.update_guidance(content)
            return
        with self._lock:
            if visibility in ("private", "shared"):
                self._visibilities[label] = visibility
            self.state.update_block(label, content, auto_generated=auto_generated)
            self._persist_block(label)
        logger.info("Updated block %s", label)

    def append_fact_to_block(self, label: str, content: str) -> bool:
        """Append transcript-derived content and protect it from distillation.

        Distillation replaces a block only while the block is entirely its own
        output, so an appended fact marks the block hand-authored; otherwise the
        next pass would erase facts that live only in the block.
        """
        with self._lock:
            if not self.state.append_to_block(label, content):
                return False
            updated = self.state.get_block(label)
            if updated is not None:
                updated.auto_generated = False
            # No write here: process_transcript persists every touched block
            # once at the end, so one fact does not cost a commit.
        return True

    @staticmethod
    def _registered_placeholder(label: str) -> str:
        """The registry's default text for *label* ("" when unregistered)."""
        schema = get_registry().get_schema(label) or initialize_default_blocks().get_schema(label)
        return schema.content if schema else ""

    def restore_generated_block(self, label: str, placeholder: str | None = None) -> bool:
        """Restore a generated block's placeholder under the agent lock.

        The ownership check and the write share one lock, so a manual edit
        cannot land between them and be overwritten.
        """
        target = self._registered_placeholder(label) if placeholder is None else placeholder
        with self._lock:
            block = self.state.get_block(label)
            if block is None or not getattr(block, "auto_generated", False):
                return False
            previous = block.content or ""
            if previous.strip() == target.strip():
                return False
            self.state.update_block(label, target, auto_generated=True)
            try:
                self._persist_block(label)
            except Exception:
                self.state.update_block(label, previous, auto_generated=True)
                logger.warning("failed to persist %s reset; retrying next pass", label, exc_info=True)
                return False
        return True

    def refresh_project_context(self, new_content: str, placeholder: str | None = None) -> bool:
        """Replace project_context only while it is empty, placeholder, or generated.

        The eligibility check and the write share one lock, so a manual edit
        cannot land between them and be overwritten. Returns True when the
        block was written.
        """
        target = self._registered_placeholder(PROJECT_CONTEXT) if placeholder is None else placeholder
        with self._lock:
            block = self.state.get_block(PROJECT_CONTEXT)
            current = (block.content if block else "") or ""
            is_placeholder = not current.strip() or (bool(target) and current.strip() == target.strip())
            if not is_placeholder and not getattr(block, "auto_generated", False):
                return False
            if current.strip() == new_content.strip():
                return False
            previous_auto = bool(getattr(block, "auto_generated", False))
            self.state.update_block(PROJECT_CONTEXT, new_content, auto_generated=True)
            try:
                self._persist_block(PROJECT_CONTEXT)
            except Exception:
                # Keep the block as it was so the next pass retries the write.
                self.state.update_block(PROJECT_CONTEXT, current, auto_generated=previous_auto)
                logger.warning("failed to persist project_context; retrying next pass", exc_info=True)
                return False
        return True

    def add_guidance_line(self, line: str) -> None:
        """Add a line to the guidance block."""
        with self._lock:
            block = self.state.get_block(GUIDANCE)
            if block and not block.is_empty():
                self.state.update_block(GUIDANCE, f"{block.content}\n{line}")
            else:
                self.state.update_block(GUIDANCE, line)
            self._persist_block(GUIDANCE)

    def get_block(self, label: str) -> str | None:
        """Get a specific block's content."""
        with self._lock:
            block = self.state.get_block(label)
            return block.content if block else None

    def get_all_blocks(self) -> list[dict]:
        """Get all non-empty context blocks."""
        with self._lock:
            return self.state.get_all_blocks()

    def reset_block(self, label: str) -> None:
        """Reset a block to its default content."""
        schema = self.state._schema_for(label)
        if schema is not None:
            with self._lock:
                default_content = getattr(schema, "content", "")
                self.state.update_block(label, default_content)
                self._persist_block(label)
            logger.info(f"Reset block {label} to default")
            return
        raise ValueError(f"Unknown block label {label!r}. Must be one of: {self.state._known_labels()}")

    def clear_block(self, label: str) -> None:
        """Clear a block's content."""
        with self._lock:
            self.state.update_block(label, "")
            self._persist_block(label)
        logger.info(f"Cleared block {label}")


SubconsciousState = ContextBlockState
SubconsciousAgent = ContextBlockAgent


# Global instances keyed by user, tenant, and namespace lane for isolation
_context_block_agents: dict[tuple[str, str, str, str], ContextBlockAgent] = {}
_CONTEXT_BLOCK_AGENTS_LOCK = threading.Lock()


def _normalize_tenant_id(tenant_id: str | None) -> str:
    """Normalize optional tenant IDs into a stable cache key."""
    normalized = (tenant_id or "").strip()
    return normalized or "default"


def get_context_block_agent(
    user_id: str,
    tenant_id: str = "default",
    agent_id: str | None = None,
    project: str | None = None,
) -> ContextBlockAgent:
    """Get or create the context block agent instance for one user+tenant+lane."""
    # An omitted lane and an explicit 'default' lane persist to the same row,
    # so they must share one cached instance (and its auto_generated flag).
    # An omitted lane and an explicit 'default' lane persist to the same row,
    # so they must share one cached instance (and its auto_generated flag). The
    # constructor keeps the caller's original lanes: passing the normalized
    # value would turn an omitted-lane read into a lane-scoped one.
    key = (user_id, _normalize_tenant_id(tenant_id), agent_id or "default", project or "default")
    with _CONTEXT_BLOCK_AGENTS_LOCK:
        agent = _context_block_agents.get(key)
        if agent is None:
            agent = ContextBlockAgent(user_id=user_id, tenant_id=key[1], agent_id=agent_id, project=project)
            _context_block_agents[key] = agent
        return agent


def get_subconscious_agent(user_id: str, tenant_id: str = "default") -> ContextBlockAgent:
    """Compatibility wrapper for older subconscious-named integrations."""
    return get_context_block_agent(user_id, tenant_id)
