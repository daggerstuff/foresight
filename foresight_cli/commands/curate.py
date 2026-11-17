"""Curation run management commands."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from foresight.server import (
    CurationRunAction,
    ManageCurationRunsParams,
    _initialize_backend,
    init_db,
    manage_curation_runs,
)
from foresight_cli.parser import CommandSpec, UsageError
from foresight_cli.utils import config as cfg, output as out


class CurateCreateParams(BaseModel):
    """Arguments for :func:`create`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    source_bank_id: str = Field(default="default", description="Source bank to curate")
    output_bank_id: str | None = Field(default=None, description="Optional reviewable output bank")
    policy_mode: str = Field(default="rebalance", description="preserve, rebalance, or rebuild")
    tool_access: str = Field(default="observe", description="disabled, observe, or operate")
    output_mode: str = Field(default="reviewable_output", description="reviewable_output or in_place")
    instructions: str | None = Field(default=None, description="Optional curator instructions")
    transcript_bundle_file: Path | None = Field(default=None, description="JSON file with transcript messages")
    session_id: str | None = Field(default=None, description="Transcript session ID")
    run_clustering: bool = Field(default=False, description="Run clustering after curation")
    user_id: str | None = Field(default=None, description="User ID override")


class CurateGetParams(BaseModel):
    """Arguments for :func:`get_run`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    run_id: str = Field(description="Curation run ID")
    user_id: str | None = Field(default=None, description="User ID override")


class CurateListParams(BaseModel):
    """Arguments for :func:`list_runs`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    limit: int = Field(default=20, description="Maximum number of runs")
    user_id: str | None = Field(default=None, description="User ID override")


class CurateCancelParams(BaseModel):
    """Arguments for :func:`cancel_cmd`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    run_id: str = Field(description="Curation run ID")
    user_id: str | None = Field(default=None, description="User ID override")


class CurateArchiveParams(BaseModel):
    """Arguments for :func:`archive_cmd`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    run_id: str = Field(description="Curation run ID")
    user_id: str | None = Field(default=None, description="User ID override")


def _existing_file(value: str) -> Path:
    path = Path(value)
    if not path.exists():
        raise argparse.ArgumentTypeError(f"file {value!r} does not exist.")
    if path.is_dir():
        raise argparse.ArgumentTypeError(f"file {value!r} is a directory.")
    if not os.access(path, os.R_OK):
        raise argparse.ArgumentTypeError(f"file {value!r} is not readable.")
    return path


def _init_and_user(user_id_override: str | None = None):
    """Initialize DB backend and resolve user ID."""
    init_db()

    _initialize_backend()
    return cfg.get_user_id(user_id_override)


def _coerce(value: str, allowed: set[str], label: str) -> Any:
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise UsageError(f"{label} must be one of: {choices}")
    return value


def _emit(result: str) -> None:
    payload = json.loads(result) if isinstance(result, str) else result
    if isinstance(payload, dict) and not payload.get("ok", True):
        out.error(payload.get("error", {}).get("message", str(result)))
        raise SystemExit(1)

    if out.get_settings().mode == "agent":
        out.print_json(payload)
        return

    if isinstance(payload, dict):
        if "run" in payload:
            out.print_json(payload["run"])
        elif "runs" in payload:
            runs = payload["runs"]
            if isinstance(runs, list) and runs:
                rows = []
                for r in runs:
                    rows.append(
                        [
                            r.get("id", "?")[:12],
                            r.get("status", "?"),
                            r.get("policy_mode", "?"),
                            r.get("source_bank_id", "?"),
                        ]
                    )
                out.print_table(["ID", "Status", "Policy", "Source Bank"], rows, title="Curation Runs")
            else:
                out.info("No curation runs found.")
        else:
            out.print_json(payload)
    else:
        out.stdout(str(payload))


def create(params: CurateCreateParams) -> None:
    """Create a new curation run."""
    _init_and_user(params.user_id)
    policy = _coerce(params.policy_mode, {"preserve", "rebalance", "rebuild"}, "policy-mode")
    access = _coerce(params.tool_access, {"disabled", "observe", "operate"}, "tool-access")
    mode = _coerce(params.output_mode, {"reviewable_output", "in_place"}, "output-mode")

    bundle = None
    if params.transcript_bundle_file:
        bundle = json.loads(params.transcript_bundle_file.read_text())

    result = manage_curation_runs(
        ManageCurationRunsParams(
            options=CurationRunAction(
                action="create",
                source_bank_id=params.source_bank_id,
                output_bank_id=params.output_bank_id,
                policy_mode=policy,
                tool_access=access,
                output_mode=mode,
                instructions=params.instructions,
                transcript_bundle=bundle,
                session_id=params.session_id,
                project_path=None,
            ),
        )
    )
    _emit(result)


def get_run(params: CurateGetParams) -> None:
    """Get details of a curation run."""
    _init_and_user(params.user_id)
    result = manage_curation_runs(
        ManageCurationRunsParams(options=CurationRunAction(action="get", run_id=params.run_id))
    )
    _emit(result)


def list_runs(params: CurateListParams) -> None:
    """List recent curation runs."""
    _init_and_user(params.user_id)
    result = manage_curation_runs(
        ManageCurationRunsParams(options=CurationRunAction(action="list", limit=params.limit))
    )
    _emit(result)


def cancel_cmd(params: CurateCancelParams) -> None:
    """Cancel a pending or running curation run."""
    _init_and_user(params.user_id)
    result = manage_curation_runs(
        ManageCurationRunsParams(options=CurationRunAction(action="cancel", run_id=params.run_id))
    )
    _emit(result)


def archive_cmd(params: CurateArchiveParams) -> None:
    """Archive a completed, failed, or canceled curation run."""
    _init_and_user(params.user_id)
    result = manage_curation_runs(
        ManageCurationRunsParams(options=CurationRunAction(action="archive", run_id=params.run_id))
    )
    _emit(result)


def _create_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-bank-id", default="default", help="Source bank to curate")
    parser.add_argument("--output-bank-id", default=None, help="Optional reviewable output bank")
    parser.add_argument("--policy-mode", default="rebalance", help="preserve, rebalance, or rebuild")
    parser.add_argument("--tool-access", default="observe", help="disabled, observe, or operate")
    parser.add_argument("--output-mode", default="reviewable_output", help="reviewable_output or in_place")
    parser.add_argument("--instructions", default=None, help="Optional curator instructions")
    parser.add_argument(
        "--transcript-bundle",
        dest="transcript_bundle_file",
        type=_existing_file,
        default=None,
        help="JSON file with transcript messages",
    )
    parser.add_argument("--session-id", default=None, help="Transcript session ID")
    parser.add_argument("--run-clustering", action="store_true", help="Run clustering after curation")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _run_id_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run_id", help="Curation run ID")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _list_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", "-l", type=int, default=20, help="Maximum number of runs")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


COMMANDS = {
    "create": CommandSpec(create, CurateCreateParams, _create_parser),
    "get": CommandSpec(get_run, CurateGetParams, _run_id_parser),
    "list": CommandSpec(list_runs, CurateListParams, _list_parser),
    "cancel": CommandSpec(cancel_cmd, CurateCancelParams, _run_id_parser),
    "archive": CommandSpec(archive_cmd, CurateArchiveParams, _run_id_parser),
}
