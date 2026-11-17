"""Analysis commands: synthesize, reflect, profile, diff, rollback."""

from __future__ import annotations

import argparse
import json

from pydantic import BaseModel, ConfigDict, Field

from foresight.server import (
    AnalysisAction,
    AnalyzeMemoriesParams,
    ManageMemoryVersionsParams,
    SynthesizeProfileParams,
    VersionAction,
    _initialize_backend,
    analyze_memories,
    init_db,
    manage_memory_versions,
    synthesize_profile,
)
from foresight_cli.parser import CommandSpec
from foresight_cli.utils import config as cfg, output as out


class AnalysisSynthesizeParams(BaseModel):
    """Arguments for :func:`synthesize`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    limit: int = Field(default=50, description="Memory limit for synthesis")
    enhanced: bool = Field(default=False, description="Use enhanced synthesis with contradiction detection")
    user_id: str | None = Field(default=None, description="User ID override")


class AnalysisReflectParams(BaseModel):
    """Arguments for :func:`reflect`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    period: str = Field(default="weekly", description="Reflection period (daily/weekly/monthly)")
    user_id: str | None = Field(default=None, description="User ID override")


class AnalysisProfileParams(BaseModel):
    """Arguments for :func:`profile`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    max_static: int = Field(default=20, description="Max stable-fact memories")
    max_dynamic: int = Field(default=10, description="Max recent-context memories")
    no_synthesis: bool = Field(default=False, description="Disable contradiction detection")
    prompt_format: bool = Field(default=False, description="Output as formatted prompt block")
    user_id: str | None = Field(default=None, description="User ID override")


class AnalysisDiffParams(BaseModel):
    """Arguments for :func:`diff`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Memory ID")
    v1: int = Field(description="Version 1")
    v2: int = Field(description="Version 2")
    user_id: str | None = Field(default=None, description="User ID override")


class AnalysisRollbackParams(BaseModel):
    """Arguments for :func:`rollback`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Memory ID")
    version: int = Field(description="Version to rollback to")
    user_id: str | None = Field(default=None, description="User ID override")


class AnalysisVersionsParams(BaseModel):
    """Arguments for :func:`versions`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Memory ID")
    user_id: str | None = Field(default=None, description="User ID override")


def _init_and_user(user_id_override: str | None = None):
    """Initialize DB backend and resolve user ID."""
    init_db()

    _initialize_backend()
    return cfg.get_user_id(user_id_override)


def synthesize(params: AnalysisSynthesizeParams) -> None:
    """Run synthesis to find patterns, contradictions, and insights."""
    _init_and_user(params.user_id)
    result = analyze_memories(
        AnalyzeMemoriesParams(options=AnalysisAction(action="synthesize", limit=params.limit, enhanced=params.enhanced))
    )

    if out.get_settings().mode == "agent":
        out.print_json({"synthesis": result})
    else:
        out.result_block(result, title="Memory Synthesis")


def reflect(params: AnalysisReflectParams) -> None:
    """Run reflection analysis over a time period."""
    _init_and_user(params.user_id)
    result = analyze_memories(AnalyzeMemoriesParams(options=AnalysisAction(action="reflect", period=params.period)))

    if out.get_settings().mode == "agent":
        out.print_json({"reflection": result})
    else:
        out.result_block(result, title=f"Reflection ({params.period})")


def profile(params: AnalysisProfileParams) -> None:
    """Build a user profile (static facts + dynamic context)."""
    uid = _init_and_user(params.user_id)
    result = synthesize_profile(
        SynthesizeProfileParams(
            user_id=uid,
            max_static_memories=params.max_static,
            max_dynamic_memories=params.max_dynamic,
            include_synthesis=not params.no_synthesis,
            format_prompt=params.prompt_format,
        )
    )

    if out.get_settings().mode == "agent" or params.prompt_format:
        out.stdout(result)
        return

    try:
        parsed = result if isinstance(result, dict) else json.loads(result)
        static_items = parsed.get("static", [])
        dynamic_items = parsed.get("dynamic", [])
        out.stderr(
            f"User Profile ({len(static_items)} static, {len(dynamic_items)} dynamic items)", style="bold underline"
        )
        if static_items:
            out.stderr("\nStatic (stable facts):", style="underline")
            for s in static_items:
                out.stderr(f"  • {s}")
        if dynamic_items:
            out.stderr("\nDynamic (recent context):", style="underline")
            for d in dynamic_items:
                out.stderr(f"  • {d}")
    except (json.JSONDecodeError, TypeError):
        out.stdout(result)


def diff(params: AnalysisDiffParams) -> None:
    """Compare two versions of a memory."""
    _init_and_user(params.user_id)
    result = manage_memory_versions(
        ManageMemoryVersionsParams(
            options=VersionAction(
                action="diff",
                memory_id=params.memory_id,
                version1=params.v1,
                version2=params.v2,
            ),
        )
    )
    out.result_block(result, title=f"Diff {params.memory_id} (v{params.v1} → v{params.v2})")


def rollback(params: AnalysisRollbackParams) -> None:
    """Rollback a memory to a specific version."""
    _init_and_user(params.user_id)
    result = manage_memory_versions(
        ManageMemoryVersionsParams(
            options=VersionAction(action="rollback", memory_id=params.memory_id, to_version=params.version),
        )
    )
    out.warn(f"Rolled back {params.memory_id} to version {params.version}: {result}")


def versions(params: AnalysisVersionsParams) -> None:
    """List all versions of a memory."""
    _init_and_user(params.user_id)
    result = manage_memory_versions(
        ManageMemoryVersionsParams(
            options=VersionAction(action="list", memory_id=params.memory_id),
        )
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title=f"Versions for {params.memory_id}")


def _synthesize_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", "-l", type=int, default=50, help="Memory limit for synthesis")
    parser.add_argument("--enhanced", action="store_true", help="Use enhanced synthesis with contradiction detection")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _reflect_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--period", "-p", default="weekly", help="Reflection period (daily/weekly/monthly)")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _profile_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-static", type=int, default=20, help="Max stable-fact memories")
    parser.add_argument("--max-dynamic", type=int, default=10, help="Max recent-context memories")
    parser.add_argument("--no-synthesis", action="store_true", help="Disable contradiction detection")
    parser.add_argument(
        "--prompt",
        "-p",
        dest="prompt_format",
        action="store_true",
        help="Output as formatted prompt block",
    )
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _diff_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Memory ID")
    parser.add_argument("v1", type=int, help="Version 1")
    parser.add_argument("v2", type=int, help="Version 2")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _rollback_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Memory ID")
    parser.add_argument("version", type=int, help="Version to rollback to")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _versions_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Memory ID")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


COMMANDS = {
    "synthesize": CommandSpec(synthesize, AnalysisSynthesizeParams, _synthesize_parser),
    "reflect": CommandSpec(reflect, AnalysisReflectParams, _reflect_parser),
    "profile": CommandSpec(profile, AnalysisProfileParams, _profile_parser),
    "diff": CommandSpec(diff, AnalysisDiffParams, _diff_parser),
    "rollback": CommandSpec(rollback, AnalysisRollbackParams, _rollback_parser),
    "versions": CommandSpec(versions, AnalysisVersionsParams, _versions_parser),
}
