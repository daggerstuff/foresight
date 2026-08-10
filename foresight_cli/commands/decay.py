"""Decay commands: apply, config, events, strength, source, recovery."""

from __future__ import annotations

import typer

from foresight.server import (
    apply_memory_decay,
    generate_recovery_payload,
    get_decay_config,
    get_decay_events,
    get_memory_source,
    get_memory_strength,
    get_relevant_memories,
    set_decay_config,
    init_db,
)
from foresight_cli.utils import config as cfg, output as out

app = typer.Typer(help="Decay, strength, recovery, and relevance operations.")


def _init_and_user(user_id_override: str | None = None):
    """Initialize DB backend and resolve user ID."""
    init_db()
    from foresight.server import _initialize_backend

    _initialize_backend()
    return cfg.get_user_id(user_id_override)


@app.command()
def apply(
    batch_size: int = typer.Option(500, "--batch-size", help="Pagination size"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Run a decay batch for a user's memories."""
    _init_and_user(user_id)
    result = apply_memory_decay(batch_size=batch_size, user_id=user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.done(f"Decay applied: {result}")


@app.command("config-get")
def config_get(
    category: str = typer.Option("general", "--category", "-c", help="Memory category"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Get decay configuration for a category."""
    _init_and_user(user_id)
    result = get_decay_config(category=category, user_id=user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Decay Config ({category})")


@app.command("config-set")
def config_set(
    category: str = typer.Option("general", "--category", "-c", help="Memory category"),
    half_life_hours: float | None = typer.Option(None, "--half-life", help="New Ebbinghaus half-life in hours"),
    min_importance: float | None = typer.Option(None, "--min-importance", help="Floor for current_strength"),
    activation_boost: float | None = typer.Option(None, "--boost", help="Multiplier applied on each access"),
    strengthening_threshold: int | None = typer.Option(None, "--strengthen", help="Activation count to mark 'strengthening'"),
    stale_threshold: float | None = typer.Option(None, "--stale", help="Below this strength, trend becomes 'stale'"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Set decay configuration for a category."""
    _init_and_user(user_id)
    result = set_decay_config(
        category=category,
        half_life_hours=half_life_hours,
        min_importance=min_importance,
        activation_boost=activation_boost,
        strengthening_threshold=strengthening_threshold,
        stale_threshold=stale_threshold,
        user_id=user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.done(f"Decay config updated: {result}")


@app.command("events")
def events(
    limit: int = typer.Option(50, "--limit", "-l", help="Max events to return"),
    memory_id: str | None = typer.Option(None, "--memory-id", "-m", help="Filter by memory ID"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Read recent decay events (audit log)."""
    _init_and_user(user_id)
    result = get_decay_events(limit=limit, memory_id=memory_id, user_id=user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Decay Events")


@app.command()
def strength(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Get the dynamic strength and trend for a memory."""
    _init_and_user(user_id)
    result = get_memory_strength(memory_id=memory_id, user_id=user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Strength: {memory_id}")


@app.command()
def source(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Reverse-lookup a memory's source document and chunk."""
    _init_and_user(user_id)
    result = get_memory_source(memory_id=memory_id, user_id=user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Source: {memory_id}")


@app.command()
def recovery(
    session_id: str = typer.Argument(..., help="Session identifier for recovery"),
    exclude: str | None = typer.Option(None, "--exclude", help="Comma-separated memory IDs to exclude"),
    max_chars: int | None = typer.Option(None, "--max-chars", help="Character budget for payload"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Generate a compact recovery payload for session resume."""
    _init_and_user(user_id)
    result = generate_recovery_payload(
        session_id=session_id,
        exclude_memory_ids=exclude,
        max_chars=max_chars,
        user_id=user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Recovery Payload")


@app.command()
def relevant(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(5, "--limit", "-l", help="Max results"),
    min_relevance: float = typer.Option(0.1, "--min-relevance", help="Minimum combined score"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Return structured list of relevant memories for a query."""
    _init_and_user(user_id)
    result = get_relevant_memories(
        query=query,
        limit=limit,
        min_relevance=min_relevance,
        user_id=user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Relevant: {query}")
