"""Decay commands: apply, config, events, strength, source, recovery."""

from __future__ import annotations

import argparse

from pydantic import BaseModel, ConfigDict, Field

from foresight.server import (
    GetRelevantMemoriesParams,
    SetDecayConfigParams,
    _initialize_backend,
    apply_memory_decay,
    generate_recovery_payload,
    get_decay_config,
    get_decay_events,
    get_memory_source,
    get_memory_strength,
    get_relevant_memories,
    init_db,
    set_decay_config,
)
from foresight_cli.parser import CommandSpec
from foresight_cli.utils import config as cfg, output as out


class DecayApplyParams(BaseModel):
    """Arguments for :func:`apply`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    batch_size: int = Field(default=500, description="Pagination size")
    user_id: str | None = Field(default=None, description="User ID override")


class DecayConfigGetParams(BaseModel):
    """Arguments for :func:`config_get`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    category: str = Field(default="general", description="Memory category")
    user_id: str | None = Field(default=None, description="User ID override")


class DecayConfigSetParams(BaseModel):
    """Arguments for :func:`config_set`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    category: str = Field(default="general", description="Memory category")
    half_life_hours: float | None = Field(default=None, description="New Ebbinghaus half-life in hours")
    min_importance: float | None = Field(default=None, description="Floor for current_strength")
    activation_boost: float | None = Field(default=None, description="Multiplier applied on each access")
    strengthening_threshold: int | None = Field(default=None, description="Activation count to mark 'strengthening'")
    stale_threshold: float | None = Field(default=None, description="Below this strength, trend becomes 'stale'")
    user_id: str | None = Field(default=None, description="User ID override")


class DecayEventsParams(BaseModel):
    """Arguments for :func:`events`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    limit: int = Field(default=50, description="Max events to return")
    memory_id: str | None = Field(default=None, description="Filter by memory ID")
    user_id: str | None = Field(default=None, description="User ID override")


class DecayStrengthParams(BaseModel):
    """Arguments for :func:`strength`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Memory ID")
    user_id: str | None = Field(default=None, description="User ID override")


class DecaySourceParams(BaseModel):
    """Arguments for :func:`source`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Memory ID")
    user_id: str | None = Field(default=None, description="User ID override")


class DecayRecoveryParams(BaseModel):
    """Arguments for :func:`recovery`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    session_id: str = Field(description="Session identifier for recovery")
    exclude: str | None = Field(default=None, description="Comma-separated memory IDs to exclude")
    max_chars: int | None = Field(default=None, description="Character budget for payload")
    user_id: str | None = Field(default=None, description="User ID override")


class DecayRelevantParams(BaseModel):
    """Arguments for :func:`relevant`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query: str = Field(description="Search query")
    limit: int = Field(default=5, description="Max results")
    min_relevance: float = Field(default=0.1, description="Minimum combined score")
    user_id: str | None = Field(default=None, description="User ID override")


def _init_and_user(user_id_override: str | None = None):
    """Initialize DB backend and resolve user ID."""
    init_db()

    _initialize_backend()
    return cfg.get_user_id(user_id_override)


def apply(params: DecayApplyParams) -> None:
    """Run a decay batch for a user's memories."""
    _init_and_user(params.user_id)
    result = apply_memory_decay(batch_size=params.batch_size, user_id=params.user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.done(f"Decay applied: {result}")


def config_get(params: DecayConfigGetParams) -> None:
    """Get decay configuration for a category."""
    _init_and_user(params.user_id)
    result = get_decay_config(category=params.category, user_id=params.user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Decay Config ({params.category})")


def config_set(params: DecayConfigSetParams) -> None:
    """Set decay configuration for a category."""
    _init_and_user(params.user_id)
    result = set_decay_config(
        SetDecayConfigParams(
            category=params.category,
            half_life_hours=params.half_life_hours,
            min_importance=params.min_importance,
            activation_boost=params.activation_boost,
            strengthening_threshold=params.strengthening_threshold,
            stale_threshold=params.stale_threshold,
            user_id=params.user_id,
        )
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.done(f"Decay config updated: {result}")


def events(params: DecayEventsParams) -> None:
    """Read recent decay events (audit log)."""
    _init_and_user(params.user_id)
    result = get_decay_events(limit=params.limit, memory_id=params.memory_id, user_id=params.user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Decay Events")


def strength(params: DecayStrengthParams) -> None:
    """Get the dynamic strength and trend for a memory."""
    _init_and_user(params.user_id)
    result = get_memory_strength(memory_id=params.memory_id, user_id=params.user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Strength: {params.memory_id}")


def source(params: DecaySourceParams) -> None:
    """Reverse-lookup a memory's source document and chunk."""
    _init_and_user(params.user_id)
    result = get_memory_source(memory_id=params.memory_id, user_id=params.user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Source: {params.memory_id}")


def recovery(params: DecayRecoveryParams) -> None:
    """Generate a compact recovery payload for session resume."""
    _init_and_user(params.user_id)
    result = generate_recovery_payload(
        session_id=params.session_id,
        exclude_memory_ids=params.exclude,
        max_chars=params.max_chars,
        user_id=params.user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Recovery Payload")


def relevant(params: DecayRelevantParams) -> None:
    """Return structured list of relevant memories for a query."""
    _init_and_user(params.user_id)
    result = get_relevant_memories(
        GetRelevantMemoriesParams(
            query=params.query,
            limit=params.limit,
            min_relevance=params.min_relevance,
            user_id=params.user_id,
        )
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Relevant: {params.query}")


def _apply_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--batch-size", type=int, default=500, help="Pagination size")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _config_get_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--category", "-c", default="general", help="Memory category")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _config_set_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--category", "-c", default="general", help="Memory category")
    parser.add_argument(
        "--half-life",
        dest="half_life_hours",
        type=float,
        default=None,
        help="New Ebbinghaus half-life in hours",
    )
    parser.add_argument("--min-importance", type=float, default=None, help="Floor for current_strength")
    parser.add_argument(
        "--boost",
        dest="activation_boost",
        type=float,
        default=None,
        help="Multiplier applied on each access",
    )
    parser.add_argument(
        "--strengthen",
        dest="strengthening_threshold",
        type=int,
        default=None,
        help="Activation count to mark 'strengthening'",
    )
    parser.add_argument(
        "--stale",
        dest="stale_threshold",
        type=float,
        default=None,
        help="Below this strength, trend becomes 'stale'",
    )
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _events_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", "-l", type=int, default=50, help="Max events to return")
    parser.add_argument("--memory-id", "-m", default=None, help="Filter by memory ID")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _strength_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Memory ID")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _source_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Memory ID")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _recovery_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("session_id", help="Session identifier for recovery")
    parser.add_argument("--exclude", default=None, help="Comma-separated memory IDs to exclude")
    parser.add_argument("--max-chars", type=int, default=None, help="Character budget for payload")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _relevant_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", help="Search query")
    parser.add_argument("--limit", "-l", type=int, default=5, help="Max results")
    parser.add_argument("--min-relevance", type=float, default=0.1, help="Minimum combined score")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


COMMANDS = {
    "apply": CommandSpec(apply, DecayApplyParams, _apply_parser),
    "config-get": CommandSpec(config_get, DecayConfigGetParams, _config_get_parser),
    "config-set": CommandSpec(config_set, DecayConfigSetParams, _config_set_parser),
    "events": CommandSpec(events, DecayEventsParams, _events_parser),
    "strength": CommandSpec(strength, DecayStrengthParams, _strength_parser),
    "source": CommandSpec(source, DecaySourceParams, _source_parser),
    "recovery": CommandSpec(recovery, DecayRecoveryParams, _recovery_parser),
    "relevant": CommandSpec(relevant, DecayRelevantParams, _relevant_parser),
}
