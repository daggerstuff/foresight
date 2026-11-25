"""Memory CRUD and search commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from foresight.server import (
    ManageMemoriesParams,
    MemoryAction,
    MemoryUpdateOptions,
    SearchMemoriesParams,
    SearchOptions,
    StoreMemoryParams,
    _initialize_backend,
    delete_memory_embedding,
    get_current_account_id,
    get_db_connection,
    index_memory_embedding,
    init_db,
    manage_memories,
    reinforce_memory,
    search_memories,
    semantic_search_memories,
    store_memory,
)
from foresight_cli.parser import CommandSpec
from foresight_cli.utils import config as cfg, output as out
from foresight_cli.utils.output import _stdout_console


class MemoryStoreParams(BaseModel):
    """Arguments for :func:`store`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    content: str = Field(description="Memory content to store")
    scope: str = Field(default="session", description="Memory scope (session/arc/trait/fact)")
    retention: str = Field(default="short_term", description="Retention (ephemeral/short_term/long_term/permanent)")
    category: str = Field(default="fact", description="Category label")
    importance: float = Field(default=0.5, description="Importance (0.0-1.0)")
    user_id: str | None = Field(default=None, description="User ID override")


class MemoryGetParams(BaseModel):
    """Arguments for :func:`get`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Memory ID to retrieve")
    user_id: str | None = Field(default=None, description="User ID override")


class MemoryListParams(BaseModel):
    """Arguments for :func:`list_memories`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    limit: int = Field(default=20, description="Number of memories")
    offset: int = Field(default=0, description="Offset")
    user_id: str | None = Field(default=None, description="User ID override")


class MemoryQueryParams(BaseModel):
    """Arguments for :func:`query`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query_text: str = Field(description="Search query")
    limit: int = Field(default=10, description="Number of results")
    user_id: str | None = Field(default=None, description="User ID override")


class MemoryUpdateParams(BaseModel):
    """Arguments for :func:`update`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Memory ID")
    content: str | None = Field(default=None, description="New content")
    category: str | None = Field(default=None, description="New category")
    scope: str | None = Field(default=None, description="New scope")
    retention: str | None = Field(default=None, description="New retention")
    user_id: str | None = Field(default=None, description="User ID override")


class MemoryDeleteParams(BaseModel):
    """Arguments for :func:`delete`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Memory ID")
    user_id: str | None = Field(default=None, description="User ID override")


class MemoryReinforceParams(BaseModel):
    """Arguments for :func:`reinforce`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Memory ID to reinforce")
    user_id: str | None = Field(default=None, description="User ID override")


class MemorySearchParams(BaseModel):
    """Arguments for :func:`search`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query_text: str = Field(description="Search query")
    mode: str = Field(default="keyword", description="Search mode: keyword, semantic, hybrid")
    limit: int = Field(default=10, description="Number of results")
    category: str | None = Field(default=None, description="Filter by category")
    min_score: float = Field(default=0.0, description="Minimum relevance score")
    user_id: str | None = Field(default=None, description="User ID override")


class MemoryExportParams(BaseModel):
    """Arguments for :func:`export`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    output: str = Field(description="Output file path (.json or .jsonl)")
    limit: int = Field(default=0, description="Max memories (0 = all)")
    category: str | None = Field(default=None, description="Filter by category")
    user_id: str | None = Field(default=None, description="User ID override")


class MemoryImportParams(BaseModel):
    """Arguments for :func:`import_memories`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    input_file: str = Field(description="Input file path (.json or .jsonl)")
    scope: str = Field(default="session", description="Default scope for imported memories")
    retention: str = Field(default="short_term", description="Default retention")
    dry_run: bool = Field(default=False, description="Validate without importing")
    user_id: str | None = Field(default=None, description="User ID override")


class MemoryIndexParams(BaseModel):
    """Arguments for :func:`index`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Memory ID to index")
    text: str = Field(description="Text content to embed")
    provider: str | None = Field(default=None, description="Embedder provider name")
    user_id: str | None = Field(default=None, description="User ID override")


class MemoryUnindexParams(BaseModel):
    """Arguments for :func:`unindex`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Memory ID to remove embedding for")
    provider: str | None = Field(default=None, description="Embedder provider name")
    user_id: str | None = Field(default=None, description="User ID override")


def _importance(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError(f"{parsed} is not in the range 0.0<=x<=1.0")
    return parsed


def _init_and_user(user_id_override: str | None = None):
    """Initialize DB backend and resolve user ID."""
    init_db()

    _initialize_backend()
    return cfg.get_user_id(user_id_override)


def store(params: MemoryStoreParams) -> None:
    """Store a new memory."""
    _init_and_user(params.user_id)
    result = store_memory(
        StoreMemoryParams(
            content=params.content,
            scope=params.scope,
            retention=params.retention,
            category=params.category,
            importance=params.importance,
        )
    )
    out.done(f"Memory stored: {result}")


def get(params: MemoryGetParams) -> None:
    """Retrieve a specific memory by ID."""
    _init_and_user(params.user_id)
    result = search_memories(SearchMemoriesParams(options=SearchOptions(query_type="id", memory_id=params.memory_id)))
    if out.get_settings().mode == "agent":
        out.print_json(result)
    else:
        out.result_block(result, title=f"Memory {params.memory_id}")


def list_memories(params: MemoryListParams) -> None:
    """List all memories (latest first)."""
    _init_and_user(params.user_id)
    result = search_memories(
        SearchMemoriesParams(options=SearchOptions(query_type="list", limit=params.limit, offset=params.offset))
    )

    _stdout_console().print(result)


def query(params: MemoryQueryParams) -> None:
    """Search memories by content keyword."""
    _init_and_user(params.user_id)
    result = search_memories(
        SearchMemoriesParams(options=SearchOptions(query_type="keyword", query=params.query_text, limit=params.limit))
    )
    out.result_block(result, title=f"Search: {params.query_text}")


def update(params: MemoryUpdateParams) -> None:
    """Update an existing memory."""
    _init_and_user(params.user_id)
    result = manage_memories(
        ManageMemoriesParams(
            options=MemoryAction(
                action="update",
                memory_id=params.memory_id,
                updates=MemoryUpdateOptions(
                    content=params.content,
                    category=params.category,
                    scope=params.scope,
                    retention=params.retention,
                ),
            ),
        )
    )
    out.done(f"Updated {params.memory_id}: {result}")


def delete(params: MemoryDeleteParams) -> None:
    """Delete a memory by ID."""
    _init_and_user(params.user_id)
    result = manage_memories(ManageMemoriesParams(options=MemoryAction(action="delete", memory_id=params.memory_id)))
    out.warn(f"Deleted {params.memory_id}: {result}")


def reinforce(params: MemoryReinforceParams) -> None:
    """Boost a memory's strength without retrieving it."""
    _init_and_user(params.user_id)
    result = reinforce_memory(memory_id=params.memory_id, user_id=cfg.get_user_id(params.user_id))
    out.done(f"Reinforced {params.memory_id}: {result}")


def search(params: MemorySearchParams) -> None:
    """Advanced search across memories with filters."""
    _init_and_user(params.user_id)

    if params.mode == "semantic":
        try:
            result = semantic_search_memories(query=params.query_text, limit=params.limit, min_score=params.min_score)
        except ImportError:
            out.error("Semantic search requires embedding support. Use --mode keyword (default).")
            raise SystemExit(1) from None
    else:
        result = search_memories(
            SearchMemoriesParams(
                options=SearchOptions(query_type="keyword", query=params.query_text, limit=params.limit)
            )
        )

    out.result_block(result, title=f"Search ({params.mode}): {params.query_text}")


def export(params: MemoryExportParams) -> None:
    """Export memories to a JSON/JSONL file."""
    uid = _init_and_user(params.user_id)

    tenant_id = get_current_account_id()
    conn = get_db_connection()
    if params.category:
        sql = "SELECT * FROM memories WHERE user_id = ? AND tenant_id = ? AND category = ? ORDER BY created_at DESC"
        query_params: list = [uid, tenant_id, params.category]
    else:
        sql = "SELECT * FROM memories WHERE user_id = ? AND tenant_id = ? ORDER BY created_at DESC"
        query_params = [uid, tenant_id]
    if params.limit > 0:
        sql += " LIMIT ?"
        query_params.append(params.limit)
    rows = conn.execute(sql, query_params).fetchall()
    conn.close()
    all_memories = [dict(r) for r in rows]

    if len(all_memories) == 0:
        out.warn("No memories found to export.")

    if params.limit > 0:
        all_memories = all_memories[: params.limit]

    path = Path(params.output)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.suffix == ".jsonl":
        path.write_text("\n".join(json.dumps(m, default=str) for m in all_memories))
    else:
        path.write_text(__import__("json").dumps(all_memories, indent=2, default=str))

    out.done(f"Exported {len(all_memories)} memories to {path}")


def import_memories(params: MemoryImportParams) -> None:
    """Import memories from a JSON/JSONL file."""
    _init_and_user(params.user_id)
    path = Path(params.input_file)

    if not path.exists():
        out.error(f"File not found: {params.input_file}")
        raise SystemExit(1)

    raw = path.read_text()
    if path.suffix == ".jsonl":
        items = [json.loads(line) for line in raw.strip().split("\n") if line.strip()]
    else:
        items = json.loads(raw)
        if isinstance(items, dict):
            items = [items]

    if not isinstance(items, list):
        out.error("Invalid format: expected a list of memories.")
        raise SystemExit(1)

    if params.dry_run:
        out.info(f"Would import {len(items)} memories (dry run)")
        return

    imported = 0
    for item in items:
        content = item.get("content", item.get("text", ""))
        if not content:
            continue
        store_memory(
            StoreMemoryParams(
                content=content,
                scope=item.get("scope", params.scope),
                retention=item.get("retention", params.retention),
                category=item.get("category", "fact"),
                importance=item.get("importance", 0.5),
            )
        )
        imported += 1

    out.done(f"Imported {imported} memories from {path}")


def index(params: MemoryIndexParams) -> None:
    """Compute and store an embedding vector for a memory."""
    _init_and_user(params.user_id)

    result = index_memory_embedding(
        memory_id=params.memory_id,
        text=params.text,
        user_id=params.user_id,
        provider=params.provider,
    )
    out.done(f"Indexed {params.memory_id}: {result}")


def unindex(params: MemoryUnindexParams) -> None:
    """Remove a memory's stored embedding vector."""
    _init_and_user(params.user_id)

    result = delete_memory_embedding(
        memory_id=params.memory_id,
        user_id=params.user_id,
        provider=params.provider,
    )
    out.warn(f"Unindexed {params.memory_id}: {result}")


def _store_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("content", help="Memory content to store")
    parser.add_argument("--scope", "-s", default="session", help="Memory scope (session/arc/trait/fact)")
    parser.add_argument(
        "--retention",
        "-r",
        default="short_term",
        help="Retention (ephemeral/short_term/long_term/permanent)",
    )
    parser.add_argument("--category", "-c", default="fact", help="Category label")
    parser.add_argument("--importance", "-i", type=_importance, default=0.5, help="Importance (0.0-1.0)")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _get_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Memory ID to retrieve")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _list_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", "-l", type=int, default=20, help="Number of memories")
    parser.add_argument("--offset", "-o", type=int, default=0, help="Offset")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _query_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query_text", help="Search query")
    parser.add_argument("--limit", "-l", type=int, default=10, help="Number of results")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _update_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Memory ID")
    parser.add_argument("--content", "-c", default=None, help="New content")
    parser.add_argument("--category", default=None, help="New category")
    parser.add_argument("--scope", default=None, help="New scope")
    parser.add_argument("--retention", default=None, help="New retention")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _delete_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Memory ID")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _reinforce_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Memory ID to reinforce")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _search_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query_text", help="Search query")
    parser.add_argument("--mode", "-m", default="keyword", help="Search mode: keyword, semantic, hybrid")
    parser.add_argument("--limit", "-l", type=int, default=10, help="Number of results")
    parser.add_argument("--category", "-c", default=None, help="Filter by category")
    parser.add_argument("--min-score", type=float, default=0.0, help="Minimum relevance score")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _export_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("output", help="Output file path (.json or .jsonl)")
    parser.add_argument("--limit", "-l", type=int, default=0, help="Max memories (0 = all)")
    parser.add_argument("--category", "-c", default=None, help="Filter by category")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _import_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input_file", help="Input file path (.json or .jsonl)")
    parser.add_argument("--scope", "-s", default="session", help="Default scope for imported memories")
    parser.add_argument("--retention", "-r", default="short_term", help="Default retention")
    parser.add_argument("--dry-run", action="store_true", help="Validate without importing")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _index_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Memory ID to index")
    parser.add_argument("text", help="Text content to embed")
    parser.add_argument("--provider", default=None, help="Embedder provider name")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _unindex_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Memory ID to remove embedding for")
    parser.add_argument("--provider", default=None, help="Embedder provider name")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


COMMANDS = {
    "store": CommandSpec(store, MemoryStoreParams, _store_parser),
    "get": CommandSpec(get, MemoryGetParams, _get_parser),
    "list": CommandSpec(list_memories, MemoryListParams, _list_parser),
    "query": CommandSpec(query, MemoryQueryParams, _query_parser),
    "update": CommandSpec(update, MemoryUpdateParams, _update_parser),
    "delete": CommandSpec(delete, MemoryDeleteParams, _delete_parser),
    "reinforce": CommandSpec(reinforce, MemoryReinforceParams, _reinforce_parser),
    "search": CommandSpec(search, MemorySearchParams, _search_parser),
    "export": CommandSpec(export, MemoryExportParams, _export_parser),
    "import": CommandSpec(import_memories, MemoryImportParams, _import_parser),
    "index": CommandSpec(index, MemoryIndexParams, _index_parser),
    "unindex": CommandSpec(unindex, MemoryUnindexParams, _unindex_parser),
}
