"""Crystallized skill inspection, crystallization, export, verification, and review commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from foresight.profile_export import skill_signing_configured
from foresight.reflect import UserScope
from foresight.server import (
    InferredAction,
    ManageInferredParams,
    _initialize_backend,
    get_current_account_id,
    init_db,
    manage_inferred,
)
from foresight.skill_crystallization import (
    DEFAULT_MIN_SUCCESS_EPISODES,
    crystallize_skills,
    export_skills_to_dir,
    list_skills,
    load_skill,
    verify_skill_markdown,
)
from foresight_cli.parser import CommandSpec, UsageError
from foresight_cli.utils import config as cfg, output as out


class SkillsListParams(BaseModel):
    """Arguments for :func:`list_cmd`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: str = Field(default="approved", description="Filter: approved, pending, retired, declined, or all")
    limit: int = Field(default=50, description="Maximum skills to return")
    agent_id: str | None = Field(default=None, description="Optional agent namespace lane")
    project: str | None = Field(default=None, description="Optional project namespace lane")
    user_id: str | None = Field(default=None, description="User ID override")


class SkillsExportParams(BaseModel):
    """Arguments for :func:`export_cmd`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    skill_name: str | None = Field(default=None, description="Optional skill name or memory ID to export")
    output_dir: str = Field(default="skills", description="Destination directory for skills/<name>/SKILL.md")
    include_unapproved: bool = Field(default=False, description="Allow exporting pending/unapproved skills")
    agent_id: str | None = Field(default=None, description="Optional agent namespace lane")
    project: str | None = Field(default=None, description="Optional project namespace lane")
    user_id: str | None = Field(default=None, description="User ID override")


class SkillsCrystallizeParams(BaseModel):
    """Arguments for :func:`crystallize_cmd`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    min_successes: int = Field(
        default=DEFAULT_MIN_SUCCESS_EPISODES,
        description="Minimum successful episodes required to stage a skill",
    )
    limit: int = Field(default=50, description="Maximum observations to inspect")
    agent_id: str | None = Field(default=None, description="Optional agent namespace lane")
    project: str | None = Field(default=None, description="Optional project namespace lane")
    user_id: str | None = Field(default=None, description="User ID override")


class SkillsVerifyParams(BaseModel):
    """Arguments for :func:`verify_cmd`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    target: str = Field(description="Path to a SKILL.md file, or skill name / memory ID")
    agent_id: str | None = Field(default=None, description="Agent namespace lane")
    project: str | None = Field(default=None, description="Project namespace lane")
    user_id: str | None = Field(default=None, description="User ID override")


class SkillsApproveParams(BaseModel):
    """Arguments for :func:`approve_cmd`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Staged skill memory ID")
    agent_id: str | None = Field(default=None, description="Agent namespace lane")
    project: str | None = Field(default=None, description="Project namespace lane")
    user_id: str | None = Field(default=None, description="User ID override")


class SkillsDeclineParams(BaseModel):
    """Arguments for :func:`decline_cmd`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    memory_id: str = Field(description="Staged or retired skill memory ID")
    reason: str | None = Field(default=None, description="Reason recorded when declining")
    agent_id: str | None = Field(default=None, description="Agent namespace lane")
    project: str | None = Field(default=None, description="Project namespace lane")
    user_id: str | None = Field(default=None, description="User ID override")


def _init_scope(
    user_id_override: str | None = None,
    *,
    agent_id: str | None = None,
    project: str | None = None,
) -> UserScope:
    init_db()
    _initialize_backend()
    uid = cfg.get_user_id(user_id_override)
    tenant_id = get_current_account_id()
    return UserScope(user_id=uid, tenant_id=tenant_id, agent_id=agent_id, project=project)


def _emit(payload: dict[str, Any], human_fn) -> None:
    mode = out.get_settings().mode
    if mode == "agent":
        sys.stdout.write(f"[JSON] {json.dumps(payload)}\n")
        sys.stdout.flush()
        return
    if mode == "json":
        out.print_json(payload)
        return
    human_fn()


def list_cmd(params: SkillsListParams) -> None:
    """List crystallized skills."""
    allowed = {"approved", "pending", "retired", "declined", "all"}
    if params.status not in allowed:
        raise UsageError(f"status must be one of: {', '.join(sorted(allowed))}")
    scope = _init_scope(params.user_id, agent_id=params.agent_id, project=params.project)
    skills = list_skills(scope, status=params.status, limit=params.limit)
    payload = {
        "ok": True,
        "status": params.status,
        "count": len(skills),
        "skills": [{k: v for k, v in s.items() if k != "content"} for s in skills],
    }

    def _human() -> None:
        if not skills:
            out.info(f"No {params.status} skills found.")
            return
        rows = [
            [
                s["skill_name"],
                s["memory_id"],
                s["status"],
                f"v{s['version']}",
                str(s["success_count"]),
                "valid" if s["signature_valid"] else "INVALID",
            ]
            for s in skills
        ]
        out.print_table(["Skill", "Memory ID", "Status", "Ver", "Proof", "Signature"], rows, title="Foresight Skills")

    _emit(payload, _human)


def export_cmd(params: SkillsExportParams) -> None:
    """Export crystallized skills into a skills/-compatible directory layout."""
    scope = _init_scope(params.user_id, agent_id=params.agent_id, project=params.project)
    result = export_skills_to_dir(
        scope,
        Path(params.output_dir),
        skill_name=params.skill_name,
        include_unapproved=params.include_unapproved,
    )

    def _human() -> None:
        if not result["exported"]:
            out.info(f"No matching skills exported to {result['output_dir']}.")
            return
        for item in result["exported"]:
            out.done(f"Exported {item['skill_name']} (v{item['version']}) -> {item['path']}")

    _emit(result, _human)


def crystallize_cmd(params: SkillsCrystallizeParams) -> None:
    """Evaluate procedural observations and stage qualifying SKILL.md artifacts."""
    scope = _init_scope(params.user_id, agent_id=params.agent_id, project=params.project)
    result = crystallize_skills(scope, min_successes=params.min_successes, limit=params.limit)
    payload = {"ok": True, **result}

    def _human() -> None:
        out.done(
            f"Candidates: {len(result['candidates'])}, staged: {result['staged_count']}, "
            f"updated: {result['updated_count']}, retired: {result['retired_count']}"
        )
        for s in result["skills"]:
            out.info(f"  - {s['skill_name']} ({s['memory_id']}) v{s['version']} [{s['status']}]")

    _emit(payload, _human)


def verify_cmd(params: SkillsVerifyParams) -> None:
    """Verify the HMAC-SHA256 signature of a SKILL.md file or stored skill."""
    candidate_path = Path(params.target)
    if candidate_path.is_file():
        content = candidate_path.read_text(encoding="utf-8")
        ok, parsed = verify_skill_markdown(content)
        payload: dict[str, Any] = {
            "ok": ok,
            "source": str(candidate_path),
            "skill_name": parsed.get("name"),
            "signature_valid": ok,
        }
    else:
        scope = _init_scope(params.user_id, agent_id=params.agent_id, project=params.project)
        skill = load_skill(params.target, scope)
        if skill is None:
            out.error(f"Approved skill '{params.target}' not found or failed signature verification.")
            raise SystemExit(1)
        payload = {
            "ok": skill["signature_valid"],
            "source": skill["memory_id"],
            "skill_name": skill["skill_name"],
            "signature_valid": skill["signature_valid"],
        }

    def _human() -> None:
        if payload["signature_valid"]:
            out.done(f"Signature VALID for {payload.get('skill_name') or payload['source']}")
        elif not skill_signing_configured():
            out.error(
                f"Signature INVALID for {payload.get('skill_name') or payload['source']}: "
                "no FORESIGHT_PROFILE_SIGNING_KEY or FORESIGHT_ENCRYPTION_KEY is configured, "
                "so skill signatures cannot be authenticated."
            )
        else:
            out.error(f"Signature INVALID for {payload.get('skill_name') or payload['source']}")

    _emit(payload, _human)
    if not payload["signature_valid"]:
        raise SystemExit(1)


def approve_cmd(params: SkillsApproveParams) -> None:
    """Approve a staged skill in the review queue so agents can load it."""
    scope = _init_scope(params.user_id, agent_id=params.agent_id, project=params.project)
    raw = manage_inferred(
        ManageInferredParams(
            options=InferredAction(action="approve", memory_id=params.memory_id),
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            project=scope.project,
        )
    )
    payload = json.loads(raw)
    if not payload.get("ok"):
        out.error(payload.get("error", {}).get("message", raw))
        raise SystemExit(1)
    _emit(payload, lambda: out.done(f"Approved skill {params.memory_id}"))


def decline_cmd(params: SkillsDeclineParams) -> None:
    """Decline a staged or retired skill in the review queue."""
    scope = _init_scope(params.user_id, agent_id=params.agent_id, project=params.project)
    raw = manage_inferred(
        ManageInferredParams(
            options=InferredAction(action="decline", memory_id=params.memory_id, reason=params.reason),
            user_id=scope.user_id,
            agent_id=scope.agent_id,
            project=scope.project,
        )
    )
    payload = json.loads(raw)
    if not payload.get("ok"):
        out.error(payload.get("error", {}).get("message", raw))
        raise SystemExit(1)
    _emit(payload, lambda: out.warn(f"Declined skill {params.memory_id}"))


def _list_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--status",
        default="approved",
        help="Filter: approved, pending, retired, declined, or all",
    )
    parser.add_argument("--limit", "-l", type=int, default=50, help="Maximum skills to return")
    parser.add_argument("--agent-id", default=None, help="Optional agent namespace lane")
    parser.add_argument("--project", default=None, help="Optional project namespace lane")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _export_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("skill_name", nargs="?", default=None, help="Optional skill name or memory ID")
    parser.add_argument(
        "--output-dir",
        "-d",
        default="skills",
        help="Destination directory for skills/<name>/SKILL.md",
    )
    parser.add_argument("--include-unapproved", action="store_true", help="Include pending/unapproved skills")
    parser.add_argument("--agent-id", default=None, help="Optional agent namespace lane")
    parser.add_argument("--project", default=None, help="Optional project namespace lane")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _crystallize_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--min-successes",
        "-n",
        type=int,
        default=DEFAULT_MIN_SUCCESS_EPISODES,
        help="Minimum successful episodes required to stage a skill",
    )
    parser.add_argument("--limit", "-l", type=int, default=50, help="Maximum observations to inspect")
    parser.add_argument("--agent-id", default=None, help="Optional agent namespace lane")
    parser.add_argument("--project", default=None, help="Optional project namespace lane")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _verify_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", help="Path to a SKILL.md file, or skill name / memory ID")
    parser.add_argument("--agent-id", default=None, help="Optional agent namespace lane")
    parser.add_argument("--project", default=None, help="Optional project namespace lane")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _approve_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Staged skill memory ID")
    parser.add_argument("--agent-id", default=None, help="Optional agent namespace lane")
    parser.add_argument("--project", default=None, help="Optional project namespace lane")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _decline_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("memory_id", help="Staged or retired skill memory ID")
    parser.add_argument("--reason", "-r", default=None, help="Reason recorded when declining")
    parser.add_argument("--agent-id", default=None, help="Optional agent namespace lane")
    parser.add_argument("--project", default=None, help="Optional project namespace lane")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


COMMANDS = {
    "list": CommandSpec(list_cmd, SkillsListParams, _list_parser),
    "export": CommandSpec(export_cmd, SkillsExportParams, _export_parser),
    "crystallize": CommandSpec(crystallize_cmd, SkillsCrystallizeParams, _crystallize_parser),
    "verify": CommandSpec(verify_cmd, SkillsVerifyParams, _verify_parser),
    "approve": CommandSpec(approve_cmd, SkillsApproveParams, _approve_parser),
    "decline": CommandSpec(decline_cmd, SkillsDeclineParams, _decline_parser),
}
