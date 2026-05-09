#!/usr/bin/env python3
"""Foresight CLI for memory, context-block, and curation workflows."""

import json
from pathlib import Path

import click
import typer
from foresight_mcp import (
    AnalysisAction,
    ContextBlockAction,
    CurationRunAction,
    MemoryAction,
    MemoryOptions,
    MemoryUpdateOptions,
    SearchOptions,
    VersionAction,
    analyze_memories,
    get_system_status,
    manage_context_blocks,
    manage_curation_runs,
    manage_memories,
    manage_memory_versions,
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
blocks_app = typer.Typer(help="Manage Foresight context blocks.")
curate_app = typer.Typer(help="Manage async Foresight curation runs.")
console = Console()

app.add_typer(blocks_app, name="blocks")
app.add_typer(curate_app, name="curate")


def output_json(data: dict) -> None:
    """Output data as formatted JSON."""
    console.print(JSON(json.dumps(data)))


def _ctx() -> click.Context:
    return click.get_current_context()


def _ctx_user_id() -> str | None:
    return _ctx().obj["user_id"]


def _ctx_json() -> bool:
    return _ctx().obj["json"]


def _load_transcript_bundle(path: Path | None) -> list[dict] | None:
    """Load a transcript bundle JSON file if provided."""
    if path is None:
        return None
    return json.loads(path.read_text())


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
    user_id = _ctx_user_id()
    _json = _ctx_json()
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
    user_id = _ctx_user_id()
    _json = _ctx_json()
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
    user_id = _ctx_user_id()
    _json = _ctx_json()
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
    user_id = _ctx_user_id()
    _json = _ctx_json()
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
    user_id = _ctx_user_id()
    _json = _ctx_json()
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
    user_id = _ctx_user_id()
    _json = _ctx_json()
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
    user_id = _ctx_user_id()
    _json = _ctx_json()
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
    user_id = _ctx_user_id()
    _json = _ctx_json()
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
    user_id = _ctx_user_id()
    _json = _ctx_json()
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
    user_id = _ctx_user_id()
    _json = _ctx_json()
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
    user_id = _ctx_user_id()
    _json = _ctx_json()
    result = get_system_status(user_id=user_id)

    if _json:
        output_json(json.loads(result))
    else:
        console.print(result)


@blocks_app.command("list")
def cmd_blocks_list():
    """List non-empty context blocks."""
    user_id = _ctx_user_id()
    _json = _ctx_json()
    result = manage_context_blocks(options=ContextBlockAction(action="list"), user_id=user_id)
    if _json:
        output_json({"blocks": json.loads(result)})
    else:
        console.print(result)


@blocks_app.command("get")
def cmd_blocks_get(
    label: str = typer.Argument(..., help="Block label"),
):
    """Get a specific context block."""
    user_id = _ctx_user_id()
    _json = _ctx_json()
    result = manage_context_blocks(
        options=ContextBlockAction(action="get", label=label),
        user_id=user_id,
    )

    if _json:
        output_json({"label": label, "result": result})
    else:
        console.print(result)


@blocks_app.command("update")
def cmd_blocks_update(
    label: str = typer.Argument(..., help="Block label"),
    content: str = typer.Argument(..., help="New block content"),
):
    """Update a context block."""
    user_id = _ctx_user_id()
    _json = _ctx_json()
    result = manage_context_blocks(
        options=ContextBlockAction(action="update", label=label, content=content),
        user_id=user_id,
    )
    if _json:
        output_json({"label": label, "result": result})
    else:
        console.print(Text(result, style="yellow"))


@blocks_app.command("reset")
def cmd_blocks_reset(
    label: str = typer.Argument(..., help="Block label"),
):
    """Reset a context block to its default value."""
    user_id = _ctx_user_id()
    _json = _ctx_json()
    result = manage_context_blocks(
        options=ContextBlockAction(action="reset", label=label),
        user_id=user_id,
    )
    if _json:
        output_json({"label": label, "result": result})
    else:
        console.print(Text(result, style="green"))


@blocks_app.command("clear")
def cmd_blocks_clear(
    label: str = typer.Argument(..., help="Block label"),
):
    """Clear a context block."""
    user_id = _ctx_user_id()
    _json = _ctx_json()
    result = manage_context_blocks(
        options=ContextBlockAction(action="clear", label=label),
        user_id=user_id,
    )
    if _json:
        output_json({"label": label, "result": result})
    else:
        console.print(Text(result, style="red"))


@curate_app.command("create")
def cmd_curate_create(
    source_bank_id: str = typer.Option(..., "--source-bank-id", help="Source bank to curate"),
    output_bank_id: str | None = typer.Option(None, "--output-bank-id", help="Optional reviewable output bank"),
    policy_mode: str = typer.Option("rebalance", "--policy-mode", help="preserve, rebalance, or rebuild"),
    tool_access: str = typer.Option("observe", "--tool-access", help="disabled, observe, or operate"),
    output_mode: str = typer.Option("reviewable_output", "--output-mode", help="reviewable_output or in_place"),
    instructions: str | None = typer.Option(None, "--instructions", help="Optional curator instructions"),
    transcript_bundle_file: Path | None = typer.Option(
        None,
        "--transcript-bundle-file",
        help="Optional JSON file containing transcript messages",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    session_id: str | None = typer.Option(None, "--session-id", help="Optional transcript session ID"),
    project_path: str | None = typer.Option(None, "--project-path", help="Optional transcript project path"),
):
    """Create a new curation run."""
    user_id = _ctx_user_id()
    _json = _ctx_json()
    result = manage_curation_runs(
        options=CurationRunAction(
            action="create",
            source_bank_id=source_bank_id,
            output_bank_id=output_bank_id,
            policy_mode=policy_mode,
            tool_access=tool_access,
            output_mode=output_mode,
            instructions=instructions,
            transcript_bundle=_load_transcript_bundle(transcript_bundle_file),
            session_id=session_id,
            project_path=project_path,
        ),
        user_id=user_id,
    )
    if _json:
        output_json({"run": json.loads(result)})
    else:
        console.print(result)


@curate_app.command("get")
def cmd_curate_get(
    run_id: str = typer.Argument(..., help="Curation run ID"),
):
    """Get a curation run."""
    user_id = _ctx_user_id()
    _json = _ctx_json()
    result = manage_curation_runs(options=CurationRunAction(action="get", run_id=run_id), user_id=user_id)
    if _json:
        output_json({"run": json.loads(result)})
    else:
        console.print(result)


@curate_app.command("list")
def cmd_curate_list(
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum number of runs"),
):
    """List recent curation runs."""
    user_id = _ctx_user_id()
    _json = _ctx_json()
    result = manage_curation_runs(options=CurationRunAction(action="list", limit=limit), user_id=user_id)
    if _json:
        output_json({"runs": json.loads(result)})
    else:
        console.print(result)


@curate_app.command("cancel")
def cmd_curate_cancel(
    run_id: str = typer.Argument(..., help="Curation run ID"),
):
    """Cancel a pending or running curation run."""
    user_id = _ctx_user_id()
    _json = _ctx_json()
    result = manage_curation_runs(options=CurationRunAction(action="cancel", run_id=run_id), user_id=user_id)
    if _json:
        output_json({"run": json.loads(result)})
    else:
        console.print(result)


@curate_app.command("archive")
def cmd_curate_archive(
    run_id: str = typer.Argument(..., help="Curation run ID"),
):
    """Archive a completed, failed, or canceled curation run."""
    user_id = _ctx_user_id()
    _json = _ctx_json()
    result = manage_curation_runs(options=CurationRunAction(action="archive", run_id=run_id), user_id=user_id)
    if _json:
        output_json({"run": json.loads(result)})
    else:
        console.print(result)


@app.command("block-get", hidden=True)
def cmd_block_get_legacy(
    label: str = typer.Argument(..., help="Block label"),
):
    """Legacy alias for `foresight blocks get`."""
    cmd_blocks_get(label)


def main():
    """CLI entry point."""
    app()


if __name__ == "__main__":
    app()
