"""Document management commands: create, get, delete, list chunks."""

from __future__ import annotations

import typer

from foresight.server import create_document, delete_document, get_document, list_document_chunks, init_db
from foresight_cli.utils import config as cfg, output as out

app = typer.Typer(help="Manage source documents and their chunks.")


def _init_and_user(user_id_override: str | None = None):
    """Initialize DB backend and resolve user ID."""
    init_db()
    from foresight.server import _initialize_backend

    _initialize_backend()
    return cfg.get_user_id(user_id_override)


@app.command()
def create(
    title: str = typer.Argument(..., help="Document title"),
    content: str = typer.Argument(..., help="Document content (raw text)"),
    source: str = typer.Option("note", "--source", "-s", help="Source type (transcript/article/journal/note/email/other)"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Create a new document with auto-chunking."""
    _init_and_user(user_id_override=None)
    result = create_document(title=title, content=content, source=source, user_id=user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.done(f"Document created: {result}")


@app.command()
def get(
    document_id: str = typer.Argument(..., help="Document ID"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Retrieve a document by ID."""
    _init_and_user(user_id)
    result = get_document(document_id=document_id, user_id=user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Document {document_id}")


@app.command()
def delete(
    document_id: str = typer.Argument(..., help="Document ID to delete"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """Delete a document and all its chunks."""
    _init_and_user(user_id)
    result = delete_document(document_id=document_id, user_id=user_id)
    out.warn(f"Deleted document {document_id}: {result}")


@app.command("chunks")
def list_chunks(
    document_id: str = typer.Argument(..., help="Document ID"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
):
    """List all chunks from a document, in order."""
    _init_and_user(user_id)
    result = list_document_chunks(document_id=document_id, user_id=user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Chunks for {document_id}")
