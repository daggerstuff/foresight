"""Graph commands: link, relationships, traverse, entities, clusters."""

from __future__ import annotations

import argparse
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from foresight.server import (
    EntityAction,
    EntityQuery,
    LinkMemoriesParams,
    _initialize_backend,
    get_memory_relationships,
    init_db,
    link_memories,
    manage_entities,
    query_clusters,
    query_entities,
    run_clustering,
    traverse_memory_graph,
)
from foresight_cli.parser import CommandSpec
from foresight_cli.utils import config as cfg, output as out

QUERY_TYPES = ("by_type", "by_name", "relationships", "traverse")
DIRECTIONS = ("in", "out", "both")


class GraphLinkParams(BaseModel):
    """Arguments for :func:`link`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    source: str = Field(description="Source memory ID")
    target: str = Field(description="Target memory ID")
    relationship_type: str = Field(
        default="related",
        description="Relationship type (updates/extends/derives/contradicts/supports/related)",
    )
    confidence: float = Field(default=1.0, description="Confidence score (0.0-1.0)")
    user_id: str | None = Field(default=None, description="User ID override")


class GraphRelationshipsParams(BaseModel):
    """Arguments for :func:`relationships`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Memory ID")
    direction: str = Field(default="both", description="Direction (in/out/both)")
    rel_type: str | None = Field(default=None, description="Filter by relationship type")
    user_id: str | None = Field(default=None, description="User ID override")


class GraphTraverseParams(BaseModel):
    """Arguments for :func:`traverse`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    root: str = Field(description="Root memory ID")
    max_depth: int = Field(default=2, description="Max traversal depth (0-5)")
    limit: int = Field(default=100, description="Max nodes to return (1-1000)")
    user_id: str | None = Field(default=None, description="User ID override")


class GraphExtractParams(BaseModel):
    """Arguments for :func:`extract`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    content: str = Field(description="Text to extract entities from")
    user_id: str | None = Field(default=None, description="User ID override")


class GraphEntitiesParams(BaseModel):
    """Arguments for :func:`query_entities_cmd`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query_type: Literal["by_type", "by_name", "relationships", "traverse"] = Field(
        default="by_type", description="Query type"
    )
    entity_type: str | None = Field(default=None, description="Entity type filter")
    name: str | None = Field(default=None, description="Name for partial match")
    entity_id: str | None = Field(default=None, description="Entity ID for relationships/traverse")
    direction: Literal["in", "out", "both"] = Field(default="both", description="Relationship direction")
    max_depth: int = Field(default=2, description="Traversal depth")
    limit: int = Field(default=50, description="Result limit")
    user_id: str | None = Field(default=None, description="User ID override")


class GraphClusterParams(BaseModel):
    """Arguments for :func:`cluster`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    min_similarity: float = Field(default=0.25, description="Minimum Jaccard similarity")
    min_cluster_size: int = Field(default=2, description="Minimum memories per cluster")
    max_clusters: int = Field(default=20, description="Max clusters to create")
    user_id: str | None = Field(default=None, description="User ID override")


class GraphClustersParams(BaseModel):
    """Arguments for :func:`query_clusters_cmd`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    limit: int = Field(default=50, description="Max clusters to return")
    user_id: str | None = Field(default=None, description="User ID override")


def _init_and_user(user_id_override: str | None = None):
    """Initialize DB backend and resolve user ID."""
    init_db()

    _initialize_backend()
    return cfg.get_user_id(user_id_override)


def link(params: GraphLinkParams) -> None:
    """Create a typed relationship between two memories."""
    _init_and_user(params.user_id)
    result = link_memories(
        LinkMemoriesParams(
            source_memory_id=params.source,
            target_memory_id=params.target,
            relationship_type=params.relationship_type,
            confidence=params.confidence,
            user_id=params.user_id,
        )
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.done(f"Linked {params.source} → {params.target} ({params.relationship_type})")


def relationships(params: GraphRelationshipsParams) -> None:
    """Show relationships for a memory."""
    _init_and_user(params.user_id)
    result = get_memory_relationships(
        memory_id=params.memory_id,
        direction=params.direction,
        relationship_type=params.rel_type,
        user_id=params.user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Relationships for {params.memory_id}")


def traverse(params: GraphTraverseParams) -> None:
    """BFS-traverse the memory relationship graph from a root."""
    _init_and_user(params.user_id)
    result = traverse_memory_graph(
        root_memory_id=params.root,
        max_depth=params.max_depth,
        limit=params.limit,
        user_id=params.user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Graph from {params.root}")


def extract(params: GraphExtractParams) -> None:
    """Extract entities from text."""
    _init_and_user(params.user_id)
    result = manage_entities(
        action=EntityAction(action="extract", content=params.content),
        user_id=params.user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Extracted Entities")


def query_entities_cmd(params: GraphEntitiesParams) -> None:
    """Query entities and graph relationships."""
    _init_and_user(params.user_id)
    result = query_entities(
        query=EntityQuery(
            query_type=params.query_type,
            entity_type=params.entity_type,
            name=params.name,
            entity_id=params.entity_id,
            direction=params.direction,
            max_depth=params.max_depth,
            limit=params.limit,
        ),
        user_id=params.user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Entity Query")


def cluster(params: GraphClusterParams) -> None:
    """Group memories into semantic clusters."""
    _init_and_user(params.user_id)
    result = run_clustering(
        min_similarity=params.min_similarity,
        min_cluster_size=params.min_cluster_size,
        max_clusters=params.max_clusters,
        user_id=params.user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Clustering Results")


def query_clusters_cmd(params: GraphClustersParams) -> None:
    """Query existing cluster entities."""
    _init_and_user(params.user_id)
    result = query_clusters(limit=params.limit, user_id=params.user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Clusters")


def _link_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("source", help="Source memory ID")
    parser.add_argument("target", help="Target memory ID")
    parser.add_argument(
        "--type",
        "-t",
        dest="relationship_type",
        default="related",
        help="Relationship type (updates/extends/derives/contradicts/supports/related)",
    )
    parser.add_argument("--confidence", "-c", type=float, default=1.0, help="Confidence score (0.0-1.0)")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _relationships_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Memory ID")
    parser.add_argument("--direction", "-d", default="both", help="Direction (in/out/both)")
    parser.add_argument("--type", "-t", dest="rel_type", default=None, help="Filter by relationship type")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _traverse_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("root", help="Root memory ID")
    parser.add_argument("--depth", "-d", dest="max_depth", type=int, default=2, help="Max traversal depth (0-5)")
    parser.add_argument("--limit", "-l", type=int, default=100, help="Max nodes to return (1-1000)")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _extract_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("content", help="Text to extract entities from")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _entities_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--type", "-t", dest="query_type", choices=QUERY_TYPES, default="by_type", help="Query type")
    parser.add_argument("--entity-type", default=None, help="Entity type filter")
    parser.add_argument("--name", default=None, help="Name for partial match")
    parser.add_argument("--entity-id", default=None, help="Entity ID for relationships/traverse")
    parser.add_argument("--direction", choices=DIRECTIONS, default="both", help="Relationship direction")
    parser.add_argument("--depth", dest="max_depth", type=int, default=2, help="Traversal depth")
    parser.add_argument("--limit", "-l", type=int, default=50, help="Result limit")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _cluster_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--min-similarity", type=float, default=0.25, help="Minimum Jaccard similarity")
    parser.add_argument(
        "--min-size",
        dest="min_cluster_size",
        type=int,
        default=2,
        help="Minimum memories per cluster",
    )
    parser.add_argument("--max-clusters", type=int, default=20, help="Max clusters to create")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _clusters_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", "-l", type=int, default=50, help="Max clusters to return")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


COMMANDS = {
    "link": CommandSpec(link, GraphLinkParams, _link_parser),
    "relationships": CommandSpec(relationships, GraphRelationshipsParams, _relationships_parser),
    "traverse": CommandSpec(traverse, GraphTraverseParams, _traverse_parser),
    "extract": CommandSpec(extract, GraphExtractParams, _extract_parser),
    "entities": CommandSpec(query_entities_cmd, GraphEntitiesParams, _entities_parser),
    "cluster": CommandSpec(cluster, GraphClusterParams, _cluster_parser),
    "clusters": CommandSpec(query_clusters_cmd, GraphClustersParams, _clusters_parser),
}
