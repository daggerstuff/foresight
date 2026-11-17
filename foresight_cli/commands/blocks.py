"""Context block management commands."""

from __future__ import annotations

import argparse
import json

from pydantic import BaseModel, ConfigDict, Field

from foresight.server import (
    ContextBlockAction,
    ManageContextBlocksParams,
    _initialize_backend,
    init_db,
    manage_context_blocks,
)
from foresight_cli.parser import CommandSpec
from foresight_cli.utils import config as cfg, output as out


class BlocksListParams(BaseModel):
    """Arguments for :func:`list_blocks`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    user_id: str | None = Field(default=None, description="User ID override")


class BlocksGetParams(BaseModel):
    """Arguments for :func:`get_block`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    label: str = Field(description="Block label (e.g. guidance, preferences, pending_items)")
    user_id: str | None = Field(default=None, description="User ID override")


class BlocksUpdateParams(BaseModel):
    """Arguments for :func:`update_block`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    label: str = Field(description="Block label")
    content: str = Field(description="New block content")
    user_id: str | None = Field(default=None, description="User ID override")


class BlocksResetParams(BaseModel):
    """Arguments for :func:`reset_block`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    label: str = Field(description="Block label to reset")
    user_id: str | None = Field(default=None, description="User ID override")


class BlocksClearParams(BaseModel):
    """Arguments for :func:`clear_block`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    label: str = Field(description="Block label to clear")
    user_id: str | None = Field(default=None, description="User ID override")


def _init_and_user(user_id_override: str | None = None):
    """Initialize DB backend and resolve user ID."""
    init_db()

    _initialize_backend()
    return cfg.get_user_id(user_id_override)


def _emit(result: str) -> None:
    """Render a tool result envelope."""
    try:
        payload = json.loads(result)
    except Exception:
        out.stderr(result)
        return

    if not payload.get("ok", False):
        message = payload.get("error", {}).get("message", result)
        out.error(message)
        raise SystemExit(1)

    if out.get_settings().mode == "agent":
        out.print_json(payload)
        return

    if "label" in payload and "content" in payload:
        out.stderr(f"\n[{payload['label']}]", style="bold underline")
        out.stdout(payload["content"])
        return
    if "blocks" in payload:
        blocks = payload["blocks"]
        if isinstance(blocks, list):
            out.bullet_list(
                [f"{b.get('label', '?')}: {str(b.get('content', ''))[:60]}" for b in blocks], title="Context Blocks"
            )
        else:
            out.print_json(blocks)
        return
    if "run" in payload:
        out.print_json(payload["run"])
        return

    out.print_json(payload)


def list_blocks(params: BlocksListParams) -> None:
    """List non-empty context blocks."""
    _init_and_user(params.user_id)
    result = manage_context_blocks(ManageContextBlocksParams(options=ContextBlockAction(action="list")))
    _emit(result)


def get_block(params: BlocksGetParams) -> None:
    """Get a specific context block."""
    _init_and_user(params.user_id)
    result = manage_context_blocks(
        ManageContextBlocksParams(options=ContextBlockAction(action="get", label=params.label))
    )
    _emit(result)


def update_block(params: BlocksUpdateParams) -> None:
    """Update a context block with new content."""
    _init_and_user(params.user_id)
    result = manage_context_blocks(
        ManageContextBlocksParams(
            options=ContextBlockAction(action="update", label=params.label, content=params.content)
        )
    )
    _emit(result)


def reset_block(params: BlocksResetParams) -> None:
    """Reset a context block to its default value."""
    _init_and_user(params.user_id)
    result = manage_context_blocks(
        ManageContextBlocksParams(options=ContextBlockAction(action="reset", label=params.label))
    )
    _emit(result)


def clear_block(params: BlocksClearParams) -> None:
    """Clear a context block's content."""
    _init_and_user(params.user_id)
    result = manage_context_blocks(
        ManageContextBlocksParams(options=ContextBlockAction(action="clear", label=params.label))
    )
    _emit(result)


def _list_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _get_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("label", help="Block label (e.g. guidance, preferences, pending_items)")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _update_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("label", help="Block label")
    parser.add_argument("content", help="New block content")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _reset_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("label", help="Block label to reset")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _clear_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("label", help="Block label to clear")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


COMMANDS = {
    "list": CommandSpec(list_blocks, BlocksListParams, _list_parser),
    "get": CommandSpec(get_block, BlocksGetParams, _get_parser),
    "update": CommandSpec(update_block, BlocksUpdateParams, _update_parser),
    "reset": CommandSpec(reset_block, BlocksResetParams, _reset_parser),
    "clear": CommandSpec(clear_block, BlocksClearParams, _clear_parser),
}
