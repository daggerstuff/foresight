"""Graph commands: link, relationships, traverse, entities, clusters."""

from __future__ import annotations

from typing import Literal

import typer

from foresight import (
    EntityQuery,
    get_memory_relationships,
    link_memories,
    manage_entities,
    query_clusters,
    query_entities,
    run_clustering,
    traverse_memory_graph,
)
from foresight.server import EntityAction, init_db
from foresight_cli.utils import config as cfg, output as out

app = typer.Typer(help="Graph, entity, and clustering operations.")


def _init_and_user(user_id_override: str | None = None):
    """Initialize DB backend and resolve user ID."""
    init_db()
    from foresight.server import _initialize_backend

    _initialize_backend()
    return cfg.get_user_id(user_id_override)


@app.command()
def link(
    source: str = typer.Argument(..., help="Source memory ID"),
    target: str = typer.Argument(..., help="Target memory ID"),
    relationship_type: str = typer.Option("related", "--type", "-t", help="Relationship type (updates/extends/derives/contradicts/supports/related)"),
    confidence: float = typer.Option(1.0, "--confidence", "-c", help="Confidence score (0.0-1.0)"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Create a typed relationship between two memories."""
    _init_and_user(user_id)
    result = link_memories(
        source_memory_id=source,
        target_memory_id=target,
        relationship_type=relationship_type,
        confidence=confidence,
        user_id=user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.done(f"Linked {source} → {target} ({relationship_type})")


@app.command()
def relationships(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    direction: str = typer.Option("both", "--direction", "-d", help="Direction (in/out/both)"),
    rel_type: str | None = typer.Option(None, "--type", "-t", help="Filter by relationship type"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Show relationships for a memory."""
    _init_and_user(user_id)
    result = get_memory_relationships(
        memory_id=memory_id,
        direction=direction,
        relationship_type=rel_type,
        user_id=user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Relationships for {memory_id}")


@app.command()
def traverse(
    root: str = typer.Argument(..., help="Root memory ID"),
    max_depth: int = typer.Option(2, "--depth", "-d", help="Max traversal depth (0-5)"),
    limit: int = typer.Option(100, "--limit", "-l", help="Max nodes to return (1-1000)"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """BFS-traverse the memory relationship graph from a root."""
    _init_and_user(user_id)
    result = traverse_memory_graph(
        root_memory_id=root,
        max_depth=max_depth,
        limit=limit,
        user_id=user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Graph from {root}")


@app.command()
def extract(
    content: str = typer.Argument(..., help="Text to extract entities from"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Extract entities from text."""
    _init_and_user(user_id)
    result = manage_entities(
        action=EntityAction(action="extract", content=content),
        user_id=user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Extracted Entities")


@app.command("entities")
def query_entities_cmd(
    query_type: Literal["by_type", "by_name", "relationships", "traverse"] = typer.Option("by_type", "--type", "-t", help="Query type"),
    entity_type: str | None = typer.Option(None, "--entity-type", help="Entity type filter"),
    name: str | None = typer.Option(None, "--name", help="Name for partial match"),
    entity_id: str | None = typer.Option(None, "--entity-id", help="Entity ID for relationships/traverse"),
    direction: Literal["in", "out", "both"] = typer.Option("both", "--direction", help="Relationship direction"),
    max_depth: int = typer.Option(2, "--depth", help="Traversal depth"),
    limit: int = typer.Option(50, "--limit", "-l", help="Result limit"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Query entities and graph relationships."""
    _init_and_user(user_id)
    result = query_entities(
        query=EntityQuery(
            query_type=query_type,
            entity_type=entity_type,
            name=name,
            entity_id=entity_id,
            direction=direction,
            max_depth=max_depth,
            limit=limit,
        ),
        user_id=user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Entity Query")


@app.command()
def cluster(
    min_similarity: float = typer.Option(0.25, "--min-similarity", help="Minimum Jaccard similarity"),
    min_cluster_size: int = typer.Option(2, "--min-size", help="Minimum memories per cluster"),
    max_clusters: int = typer.Option(20, "--max-clusters", help="Max clusters to create"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Group memories into semantic clusters."""
    _init_and_user(user_id)
    result = run_clustering(
        min_similarity=min_similarity,
        min_cluster_size=min_cluster_size,
        max_clusters=max_clusters,
        user_id=user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Clustering Results")


@app.command("clusters")
def query_clusters_cmd(
    limit: int = typer.Option(50, "--limit", "-l", help="Max clusters to return"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Query existing cluster entities."""
    _init_and_user(user_id)
    result = query_clusters(limit=limit, user_id=user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Clusters")
