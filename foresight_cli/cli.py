#!/usr/bin/env python3
"""
Foresight CLI - Command-line interface for memory operations.
"""
import json

import click
import typer

# Import Foresight MCP components
from foresight_mcp import (
    AnalysisAction,
    MemoryAction,
    MemoryOptions,
    MemoryUpdateOptions,
    SearchOptions,
    SubconsciousAction,
    VersionAction,
    analyze_memories,
    get_system_status,
    manage_memories,
    manage_memory_versions,
    manage_subconscious,
    search_memories,
)
from rich.console import Console
from rich.json import JSON
from rich.text import Text

app = typer.Typer(
    name="foresight",
    help="Foresight Memory Management CLI",
    add_completion=True,
)
console = Console()

def output_json(data: dict) -> None:
    """Output data as formatted JSON."""
    console.print(JSON(json.dumps(data)))

@app.callback()
def callback(
    ctx: typer.Context,
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
    _json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Foresight Memory Management CLI."""
    ctx.obj = {"user_id": user_id, "json": _json}

@app.command("store")
def cmd_store(
    content: str = typer.Argument(..., help="Memory content to store"),
    scope: str = typer.Option("session", "--scope", "-s", help="Memory scope"),
    retention: str = typer.Option("short_term", "--retention", "-r", help="Retention"),
    category: str = typer.Option("fact", "--category", "-c", help="Category label"),
):
    """Store a new memory."""
    ctx = click.get_current_context()
    user_id = ctx.obj["user_id"]
    _json = ctx.obj["json"]

    result = manage_memories(
        options=MemoryAction(
            action="store",
            content=content,
            options=MemoryOptions(
                category=category,
                scope=scope,
                retention=retention,
            ),
        ),
        user_id=user_id,
    )

    if _json:
        output_json({"status": "stored", "result": result})
    else:
        console.print(Text(result, style="green"))

@app.command("get")
def cmd_get(
    memory_id: str = typer.Argument(..., help="Memory ID"),
):
    """Retrieve a specific memory by ID."""
    ctx = click.get_current_context()
    user_id = ctx.obj["user_id"]
    _json = ctx.obj["json"]

    result = search_memories(
        options=SearchOptions(query_type="id", memory_id=memory_id),
        user_id=user_id,
    )

    if _json:
        output_json({"id": memory_id, "result": result})
    else:
        console.print(result)

@app.command("list")
def cmd_list(
    limit: int = typer.Option(10, "--limit", "-l", help="Number of memories"),
    offset: int = typer.Option(0, "--offset", "-o", help="Offset"),
):
    """List all memories."""
    ctx = click.get_current_context()
    user_id = ctx.obj["user_id"]
    _json = ctx.obj["json"]

    result = search_memories(
        options=SearchOptions(query_type="list", limit=limit, offset=offset),
        user_id=user_id,
    )

    if _json:
        output_json({"memories": result})
    else:
        console.print(result)

@app.command("query")
def cmd_query(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(5, "--limit", "-l", help="Number of results"),
):
    """Search memories by content."""
    ctx = click.get_current_context()
    user_id = ctx.obj["user_id"]
    _json = ctx.obj["json"]

    result = search_memories(
        options=SearchOptions(query_type="keyword", query=query, limit=limit),
        user_id=user_id,
    )

    if _json:
        output_json({"query": query, "result": result})
    else:
        console.print(result)

@app.command("update")
def cmd_update(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    content: str | None = typer.Option(None, "--content", "-c", help="New content"),
    category: str | None = typer.Option(None, "--category", help="New category"),
    scope: str | None = typer.Option(None, "--scope", help="New scope"),
    retention: str | None = typer.Option(None, "--retention", help="New retention"),
):
    """Update an existing memory."""
    ctx = click.get_current_context()
    user_id = ctx.obj["user_id"]
    _json = ctx.obj["json"]

    result = manage_memories(
        options=MemoryAction(
            action="update",
            memory_id=memory_id,
            updates=MemoryUpdateOptions(
                content=content,
                category=category,
                scope=scope,
                retention=retention,
            ),
        ),
        user_id=user_id,
    )

    if _json:
        output_json({"id": memory_id, "result": result})
    else:
        console.print(Text(result, style="yellow"))

@app.command("delete")
def cmd_delete(
    memory_id: str = typer.Argument(..., help="Memory ID"),
):
    """Delete a memory by ID."""
    ctx = click.get_current_context()
    user_id = ctx.obj["user_id"]
    _json = ctx.obj["json"]

    result = manage_memories(
        options=MemoryAction(action="delete", memory_id=memory_id),
        user_id=user_id,
    )

    if _json:
        output_json({"id": memory_id, "result": result})
    else:
        console.print(Text(result, style="red"))

@app.command("synthesize")
def cmd_synthesize(
    limit: int = typer.Option(50, "--limit", "-l", help="Memory limit"),
    enhanced: bool = typer.Option(False, "--enhanced", help="Use enhanced synthesis"),
):
    """Run synthesis on memories."""
    ctx = click.get_current_context()
    user_id = ctx.obj["user_id"]
    _json = ctx.obj["json"]

    result = analyze_memories(
        options=AnalysisAction(action="synthesize", limit=limit, enhanced=enhanced),
        user_id=user_id,
    )

    if _json:
        output_json({"result": result})
    else:
        console.print(result)

@app.command("reflect")
def cmd_reflect(
    period: str = typer.Option("weekly", "--period", "-p", help="Reflection period"),
):
    """Run reflection analysis."""
    ctx = click.get_current_context()
    user_id = ctx.obj["user_id"]
    _json = ctx.obj["json"]

    result = analyze_memories(
        options=AnalysisAction(action="reflect", period=period),
        user_id=user_id,
    )

    if _json:
        output_json({"result": result})
    else:
        console.print(result)

@app.command("diff")
def cmd_diff(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    v1: int = typer.Argument(..., help="Version 1"),
    v2: int = typer.Argument(..., help="Version 2"),
):
    """Compare two versions of a memory."""
    ctx = click.get_current_context()
    user_id = ctx.obj["user_id"]
    _json = ctx.obj["json"]

    result = manage_memory_versions(
        options=VersionAction(action="diff", memory_id=memory_id, version1=v1, version2=v2),
        user_id=user_id,
    )

    if _json:
        output_json({"result": result})
    else:
        console.print(result)

@app.command("rollback")
def cmd_rollback(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    version: int = typer.Argument(..., help="Version to rollback to"),
):
    """Rollback a memory to a specific version."""
    ctx = click.get_current_context()
    user_id = ctx.obj["user_id"]
    _json = ctx.obj["json"]

    result = manage_memory_versions(
        options=VersionAction(action="rollback", memory_id=memory_id, to_version=version),
        user_id=user_id,
    )

    if _json:
        output_json({"result": result})
    else:
        console.print(result)

@app.command("status")
def cmd_status():
    """Get system status."""
    ctx = click.get_current_context()
    user_id = ctx.obj["user_id"]
    _json = ctx.obj["json"]
    result = get_system_status(user_id=user_id)

    if _json:
        output_json(json.loads(result))
    else:
        console.print(result)

@app.command("block-get")
def cmd_block_get(
    label: str = typer.Argument(..., help="Block label"),
):
    """Get a specific memory block."""
    ctx = click.get_current_context()
    user_id = ctx.obj["user_id"]
    _json = ctx.obj["json"]

    result = manage_subconscious(
        options=SubconsciousAction(action="get", label=label),
        user_id=user_id,
    )

    if _json:
        output_json({"label": label, "result": result})
    else:
        console.print(result)

def main():
    """CLI entry point."""
    app()

if __name__ == "__main__":
    app()
