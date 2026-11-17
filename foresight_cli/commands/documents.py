"""Document management commands: create, get, delete, list chunks."""

from __future__ import annotations

import argparse

from pydantic import BaseModel, ConfigDict, Field

from foresight.server import (
    CreateDocumentParams,
    _initialize_backend,
    create_document,
    delete_document,
    get_document,
    init_db,
    list_document_chunks,
)
from foresight_cli.parser import CommandSpec
from foresight_cli.utils import config as cfg, output as out


class DocumentsCreateParams(BaseModel):
    """Arguments for :func:`create`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    title: str = Field(description="Document title")
    content: str = Field(description="Document content (raw text)")
    source: str = Field(default="note", description="Source type (transcript/article/journal/note/email/other)")
    user_id: str | None = Field(default=None, description="User ID override")


class DocumentsGetParams(BaseModel):
    """Arguments for :func:`get`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    document_id: str = Field(description="Document ID")
    user_id: str | None = Field(default=None, description="User ID override")


class DocumentsDeleteParams(BaseModel):
    """Arguments for :func:`delete`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    document_id: str = Field(description="Document ID to delete")
    user_id: str | None = Field(default=None, description="User ID override")


class DocumentsChunksParams(BaseModel):
    """Arguments for :func:`list_chunks`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    document_id: str = Field(description="Document ID")
    user_id: str | None = Field(default=None, description="User ID override")


def _init_and_user(user_id_override: str | None = None):
    """Initialize DB backend and resolve user ID."""
    init_db()

    _initialize_backend()
    return cfg.get_user_id(user_id_override)


def create(params: DocumentsCreateParams) -> None:
    """Create a new document with auto-chunking."""
    _init_and_user(user_id_override=None)
    result = create_document(
        CreateDocumentParams(
            title=params.title,
            content=params.content,
            source=params.source,
            user_id=params.user_id,
        )
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.done(f"Document created: {result}")


def get(params: DocumentsGetParams) -> None:
    """Retrieve a document by ID."""
    _init_and_user(params.user_id)
    result = get_document(document_id=params.document_id, user_id=params.user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Document {params.document_id}")


def delete(params: DocumentsDeleteParams) -> None:
    """Delete a document and all its chunks."""
    _init_and_user(params.user_id)
    result = delete_document(document_id=params.document_id, user_id=params.user_id)
    out.warn(f"Deleted document {params.document_id}: {result}")


def list_chunks(params: DocumentsChunksParams) -> None:
    """List all chunks from a document, in order."""
    _init_and_user(params.user_id)
    result = list_document_chunks(document_id=params.document_id, user_id=params.user_id)
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Chunks for {params.document_id}")


def _create_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("title", help="Document title")
    parser.add_argument("content", help="Document content (raw text)")
    parser.add_argument(
        "--source",
        "-s",
        default="note",
        help="Source type (transcript/article/journal/note/email/other)",
    )
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _document_id_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("document_id", help="Document ID")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _delete_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("document_id", help="Document ID to delete")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


COMMANDS = {
    "create": CommandSpec(create, DocumentsCreateParams, _create_parser),
    "get": CommandSpec(get, DocumentsGetParams, _document_id_parser),
    "delete": CommandSpec(delete, DocumentsDeleteParams, _delete_parser),
    "chunks": CommandSpec(list_chunks, DocumentsChunksParams, _document_id_parser),
}
