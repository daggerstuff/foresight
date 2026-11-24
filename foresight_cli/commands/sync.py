"""Opt-in instruction-file sync: write top memories into agent instruction files.

``foresight sync`` recalls the highest-importance instruction/preference/goal
memories and writes them as ``- [TYPE] content`` lines inside the
``<!-- FORESIGHT-DYNAMIC-MEMORIES -->`` sentinel block of detected agent
instruction files (``CLAUDE.md``, ``AGENTS.md``, ``GEMINI.md``).

Strictly opt-in: the command never runs automatically, only rewrites the
sentinel block, and skips (reporting) files that lack the sentinels. To opt a
file in, add the block manually::

    <!-- FORESIGHT-DYNAMIC-MEMORIES -->
    <!-- /FORESIGHT-DYNAMIC-MEMORIES -->
"""

from __future__ import annotations

import argparse
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from foresight.instruction_sync import run_sync
from foresight.server import _initialize_backend, get_current_account_id, init_db
from foresight_cli.parser import CommandSpec
from foresight_cli.utils import config as cfg, output as out


class SyncParams(BaseModel):
    """Arguments for :func:`sync`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    project_dir: str = Field(
        default=".",
        description="Project directory to scan for CLAUDE.md / AGENTS.md / GEMINI.md",
    )
    limit: int = Field(default=10, description="Maximum memories to sync (1-50)")
    project: str | None = Field(
        default=None,
        description="Project lane filter (default: the project directory name)",
    )
    agent_id: str | None = Field(default=None, description="Agent lane filter")
    user_id: str | None = Field(default=None, description="User ID override")


def sync(params: SyncParams) -> None:
    """Sync top instruction memories into agent instruction files (opt-in)."""
    init_db()
    _initialize_backend()
    uid = cfg.get_user_id(params.user_id)
    tenant_id = get_current_account_id()

    project_dir = Path(params.project_dir)
    # Scope to this project's lane by default so another project's memories
    # never land in files that are usually committed.
    project = params.project or project_dir.resolve().name or "default"
    report = run_sync(
        project_dir,
        uid,
        tenant_id,
        limit=params.limit,
        project=project,
        agent_id=params.agent_id,
    )

    if out.get_settings().mode == "json":
        out.print_json(report)
        return

    out.done(f"Synced {report['memories']} memories")
    for entry in report["files"]:
        out.info(f"  {entry['file']}: {entry['status']}")
    for entry in report["skipped"]:
        out.warn(f"  {entry['file']}: skipped ({entry['reason']})")
        if entry.get("hint"):
            out.info(entry["hint"])
    if not report["files"]:
        out.warn("No instruction files with FORESIGHT-DYNAMIC-MEMORIES sentinels found.")


def _sync_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project-dir",
        "-d",
        default=".",
        help="Project directory to scan for agent instruction files (default: .)",
    )
    parser.add_argument("--limit", "-l", type=int, default=10, help="Maximum memories to sync (default: 10)")
    parser.add_argument(
        "--project",
        "-p",
        default=None,
        help="Project lane filter (default: the project directory name)",
    )
    parser.add_argument("--agent-id", default=None, help="Agent lane filter")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


COMMANDS = {"sync": CommandSpec(sync, SyncParams, _sync_parser)}
