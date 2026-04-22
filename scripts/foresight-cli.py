#!/usr/bin/env python3
"""
Foresight CLI - Command-line interface for memory operations.

Provides rich terminal output for interacting with the Foresight memory system.
"""
import sys
import json
from datetime import datetime
from typing import Optional, List

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.syntax import Syntax
from rich import print as rprint

app = typer.Typer(
    name="foresight",
    help="Foresight memory system CLI",
    add_completion=False,
)
console = Console()


def get_client():
    """Get Foresight client from server module."""
    from foresight_mcp.server import (
        store_memory, query_memories, list_memories, get_memory,
        update_memory, delete_memory, synthesize_memories,
        archive_memory, rollback_memory, diff_memories, memory_status,
        get_subconscious_blocks, reset_subconscious_block, clear_subconscious_block
    )
    return {
        'store_memory': store_memory,
        'query_memories': query_memories,
        'list_memories': list_memories,
        'get_memory': get_memory,
        'update_memory': update_memory,
        'delete_memory': delete_memory,
        'synthesize_memories': synthesize_memories,
        'archive_memory': archive_memory,
        'rollback_memory': rollback_memory,
        'diff_memories': diff_memories,
        'memory_status': memory_status,
        'get_subconscious_blocks': get_subconscious_blocks,
        'reset_subconscious_block': reset_subconscious_block,
        'clear_subconscious_block': clear_subconscious_block,
    }


@app.command()
def store(
    content: str = typer.Argument(..., help="Memory content to store"),
    category: str = typer.Option("fact", "--category", "-c", help="Memory category"),
    scope: str = typer.Option("session", "--scope", "-s", help="Memory scope"),
    retention: str = typer.Option("short_term", "--retention", "-r", help="Retention policy"),
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="User ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Store a new memory."""
    clients = get_client()

    result = clients['store_memory'](
        content=content,
        category=category,
        scope=scope,
        retention=retention,
        user_id=user_id,
    )

    if json_output:
        console.print_json(json.dumps({"result": result}))
    else:
        console.print(Panel(result, title="[green]Memory Stored[/green]", border_style="green"))


@app.command()
def query(
    query_text: str = typer.Argument(..., help="Search query"),
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="User ID"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max results"),
    category: Optional[str] = typer.Option(None, "--category", "-c", help="Filter by category"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Query memories by content."""
    clients = get_client()
    result = clients['query_memories'](
        query=query_text,
        user_id=user_id,
        limit=limit,
        category=category,
    )

    if json_output:
        console.print_json(json.dumps({"results": result}))
    else:
        lines = result.split("\n")
        for line in lines[:limit]:
            if line.strip():
                console.print(f"  [cyan]•[/cyan] {line}")


@app.command()
def list(
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="User ID"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
    offset: int = typer.Option(0, "--offset", "-o", help="Offset for pagination"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List recent memories."""
    clients = get_client()
    result = clients['list_memories'](
        user_id=user_id,
        limit=limit,
    )

    if json_output:
        console.print_json(json.dumps({"memories": result}))
    else:
        lines = result.split("\n")
        table = Table(title="Recent Memories")
        table.add_column("ID", style="cyan", width=12)
        table.add_column("Content", style="white")

        for line in lines[:limit]:
            if line.strip():
                parts = line.split(" | ", 1)
                mem_id = parts[0][:12] if len(parts) > 0 else ""
                content = parts[1] if len(parts) > 1 else line
                table.add_row(mem_id, content[:60] + "..." if len(content) > 60 else content)

        console.print(table)


@app.command()
def get(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="User ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Get a specific memory by ID."""
    clients = get_client()
    result = clients['get_memory'](
        memory_id=memory_id,
        user_id=user_id,
    )

    if json_output:
        console.print_json(json.dumps({"memory": result}))
    else:
        console.print(Panel(result, title=f"[cyan]{memory_id}[/cyan]"))


@app.command()
def update(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    content: Optional[str] = typer.Option(None, "--content", "-c", help="New content"),
    importance: Optional[float] = typer.Option(None, "--importance", "-i", help="Importance 0-1"),
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="User ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Update a memory."""
    clients = get_client()
    result = clients['update_memory'](
        memory_id=memory_id,
        content=content,
        importance=importance,
        user_id=user_id,
    )

    if json_output:
        console.print_json(json.dumps({"result": result}))
    else:
        console.print(Panel(result, title="[yellow]Memory Updated[/yellow]", border_style="yellow"))


@app.command()
def delete(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="User ID"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Delete a memory."""
    if not force:
        typer.confirm(f"Delete memory {memory_id}?", abort=True)

    clients = get_client()
    result = clients['delete_memory'](
        memory_id=memory_id,
        user_id=user_id,
    )

    console.print(Panel(result, title="[red]Memory Deleted[/red]", border_style="red"))


@app.command()
def synthesize(
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="User ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Synthesize memories into insights."""
    clients = get_client()
    result = clients['synthesize_memories'](
        user_id=user_id,
    )

    if json_output:
        console.print_json(json.dumps({"synthesis": result}))
    else:
        console.print(Panel(result, title="[magenta]Synthesis[/magenta]", border_style="magenta"))


@app.command()
def archive(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="User ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Archive a memory."""
    clients = get_client()
    result = clients['archive_memory'](
        memory_id=memory_id,
        user_id=user_id,
    )

    if json_output:
        console.print_json(json.dumps({"result": result}))
    else:
        console.print(Panel(result, title="[dim]Memory Archived[/dim]", border_style="dim"))


@app.command()
def rollback(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    version: int = typer.Argument(..., help="Target version"),
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="User ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Rollback a memory to a previous version."""
    clients = get_client()
    result = clients['rollback_memory'](
        memory_id=memory_id,
        to_version=version,
        user_id=user_id,
    )

    if json_output:
        console.print_json(json.dumps({"result": result}))
    else:
        console.print(Panel(result, title="[yellow]Rolled Back[/yellow]", border_style="yellow"))


@app.command()
def diff(
    memory_id: str = typer.Argument(..., help="Memory ID"),
    version1: int = typer.Argument(..., help="First version"),
    version2: int = typer.Argument(..., help="Second version"),
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="User ID"),
):
    """Show diff between two memory versions."""
    clients = get_client()
    result = clients['diff_memories'](
        memory_id=memory_id,
        version1=version1,
        version2=version2,
        user_id=user_id,
    )

    console.print(Syntax(result, "diff", theme="monokai"))


@app.command()
def status():
    """Show memory system status."""
    clients = get_client()
    result = clients['memory_status']()

    console.print(Panel(result, title="[blue]System Status[/blue]", border_style="blue"))


subconscious_app = typer.Typer()
app.add_typer(subconscious_app, name="subconscious", help="Subconscious block operations")


@subconscious_app.command()
def list_blocks(
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="User ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """List subconscious blocks."""
    clients = get_client()
    result = clients['get_subconscious_blocks'](
        user_id=user_id,
    )

    if json_output:
        console.print_json(json.dumps({"blocks": result}))
    else:
        console.print(Panel(result, title="[purple]Subconscious Blocks[/purple]", border_style="purple"))


@subconscious_app.command()
def reset(
    label: str = typer.Argument(..., help="Block label"),
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="User ID"),
):
    """Reset a subconscious block."""
    clients = get_client()
    result = clients['reset_subconscious_block'](
        label=label,
        user_id=user_id,
    )
    console.print(Panel(result, title="[yellow]Block Reset[/yellow]", border_style="yellow"))


@subconscious_app.command()
def clear(
    label: str = typer.Argument(..., help="Block label"),
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="User ID"),
):
    """Clear a subconscious block."""
    clients = get_client()
    result = clients['clear_subconscious_block'](
        label=label,
        user_id=user_id,
    )
    console.print(Panel(result, title="[red]Block Cleared[/red]", border_style="red"))


def main():
    """Entry point."""
    app()


if __name__ == "__main__":
    main()
