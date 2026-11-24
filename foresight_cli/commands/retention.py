"""Retention policy commands: show, set, presets.

The declarative retention policy is what ``MemoryGC`` reads to expire and
purge memories. Until now only tests wrote it, so these commands are the
production write path for ``memory_policies``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, NoReturn

from pydantic import BaseModel, ConfigDict, Field

from foresight.config import DEFAULT_TENANT_ID
from foresight.retention_policy import (
    POLICY_PRESETS,
    PolicyError,
    load_policy_status,
    preset_policy,
    save_policy,
)
from foresight.server import _initialize_backend, get_current_account_id, get_db_connection, init_db
from foresight_cli.parser import CommandSpec
from foresight_cli.utils import config as cfg, output as out


class RetentionShowParams(BaseModel):
    """Arguments for :func:`show`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    tenant_id: str | None = Field(default=None, description="Tenant ID (default: active tenant)")
    user_id: str | None = Field(default=None, description="User ID override")


class RetentionSetParams(BaseModel):
    """Arguments for :func:`set_policy`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    preset: str | None = Field(default=None, description="Preset name (conservative/balanced/aggressive)")
    policy: str | None = Field(default=None, description="Policy JSON, or @path/to/policy.json")
    purge_expired_after: str | None = Field(
        default=None,
        description="Destructive purge window for expired rows (30m/12h/7d/2w/3mo/1y/never)",
    )
    yes: bool = Field(default=False, description="Confirm a policy whose purge window enables hard deletion")
    tenant_id: str | None = Field(default=None, description="Tenant ID (default: active tenant)")
    user_id: str | None = Field(default=None, description="User ID override")


class RetentionPresetsParams(BaseModel):
    """Arguments for :func:`presets`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


_TENANT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _init_backend() -> None:
    """Bootstrap the backend from the CLI config, matching `foresight system`."""
    config = cfg.ensure_config()
    if not os.environ.get("FORESIGHT_DB_URL") and config.db_url:
        os.environ["FORESIGHT_DB_URL"] = config.db_url
    if not os.environ.get("FORESIGHT_USER_ID") and config.user_id:
        os.environ["FORESIGHT_USER_ID"] = config.user_id
    if not os.environ.get("FORESIGHT_BANK_ID") and config.bank_id:
        os.environ["FORESIGHT_BANK_ID"] = config.bank_id
    init_db()
    _initialize_backend()


def _tenant(override: str | None = None, user_id: str | None = None) -> str:
    """Resolve the tenant, rejecting an override that names no real tenant."""
    if override is None:
        return get_current_account_id()
    if not _TENANT_ID_RE.fullmatch(override):
        _fail(f"Tenant '{override}' is not a valid tenant ID (letters, digits, '-' and '_' only).")
    if override != DEFAULT_TENANT_ID:
        conn = get_db_connection()
        try:
            row = conn.execute("SELECT 1 FROM tenants WHERE id = ?", (override,)).fetchone()
            if not row:
                # Tenants are created lazily by the first write, so a lane with
                # stored memories is real even without a tenants row.
                row = conn.execute(
                    "SELECT 1 FROM memories WHERE tenant_id = ? AND user_id = ? LIMIT 1",
                    (override, user_id or cfg.get_user_id(None)),
                ).fetchone()
        finally:
            conn.close()
        if not row:
            _fail(f"Tenant '{override}' not found; check the ID before writing a policy for it.")
    return override


def _fail(message: str) -> NoReturn:
    """Report a usage error in every output mode, then exit 2."""
    if out.get_settings().mode == "agent":
        sys.stderr.write(f"[ERROR] {message}\n")
        sys.stderr.flush()
    out.error(message)
    raise SystemExit(2)


def _emit(payload: dict[str, Any], human: list[str]) -> None:
    mode = out.get_settings().mode
    if mode == "agent":
        sys.stdout.write(f"[JSON] {json.dumps(payload)}\n")
        sys.stdout.flush()
        return
    if mode == "json":
        out.print_json(payload)
        return
    for line in human:
        out.info(line)


def show(params: RetentionShowParams) -> None:
    """Show the stored retention policy for the active user/tenant."""
    _init_backend()
    uid = cfg.get_user_id(params.user_id)
    tenant_id = _tenant(params.tenant_id, user_id=uid)
    conn = get_db_connection()
    try:
        config, status = load_policy_status(conn, tenant_id, uid)
    finally:
        conn.close()

    if config is None:
        empty_payload: dict[str, Any] = {"tenant_id": tenant_id, "user_id": uid, "policy": None, "status": status}
        human = [
            f"Stored retention policy is {status} for {uid} in tenant {tenant_id}."
            if status and status.startswith("invalid")
            else f"No retention policy stored for {uid} in tenant {tenant_id}."
        ]
        if status and status.startswith("invalid"):
            human.append("Fix it with `foresight retention set --preset ...`.")
        _emit(empty_payload, human)
        return

    policy = config.to_dict()
    payload: dict[str, Any] = {"tenant_id": tenant_id, "user_id": uid, "policy": policy}
    human = [
        f"Retention policy for {uid} (tenant {tenant_id})",
        f"  purge_expired_after: {policy['purge_expired_after']}",
        f"  retention: {json.dumps(policy['retention'])}",
        f"  rules: {len(policy['rules'])}",
    ]
    human.extend(f"    - {rule['name']}: expire_after={rule['expire_after']}" for rule in policy["rules"])
    _emit(payload, human)


def set_policy(params: RetentionSetParams) -> None:
    """Save a retention policy from a preset or a JSON document."""
    _init_backend()
    uid = cfg.get_user_id(params.user_id)
    tenant_id = _tenant(params.tenant_id, user_id=uid)

    if params.preset:
        try:
            policy = preset_policy(params.preset)
        except PolicyError as exc:
            _fail(str(exc))
    elif params.policy:
        raw = params.policy
        try:
            if raw.startswith("@"):
                raw = Path(raw[1:]).read_text(encoding="utf-8")
            policy = json.loads(raw)
        except (OSError, ValueError) as exc:
            _fail(f"Invalid policy document: {exc}")
    else:
        _fail("Provide --preset or --policy (JSON, or @path/to/policy.json).")

    if not isinstance(policy, dict):
        _fail("Policy document must be a JSON object.")

    if params.purge_expired_after:
        policy = {**policy, "purge_expired_after": params.purge_expired_after}
    if policy.get("purge_expired_after", "never") != "never" and not params.yes:
        _fail("purge_expired_after enables hard deletion of expired memories; re-run with --yes to confirm.")

    conn = get_db_connection()
    try:
        config = save_policy(conn, tenant_id, uid, policy, preset=params.preset)
    except PolicyError as exc:
        _fail(f"Invalid policy: {exc}")
    finally:
        conn.close()

    payload = {"ok": True, "tenant_id": tenant_id, "user_id": uid, "policy": config.to_dict()}
    _emit(payload, [f"Saved retention policy for {uid} (tenant {tenant_id}); preset={params.preset or 'custom'}."])


def presets(_params: RetentionPresetsParams) -> None:
    """List the built-in retention presets."""
    payload = {"presets": {name: preset_policy(name) for name in sorted(POLICY_PRESETS)}}
    human = [
        f"{name}: purge_expired_after={policy['purge_expired_after']}, rules={len(policy['rules'])}"
        for name, policy in payload["presets"].items()
    ]
    _emit(payload, human)


def _set_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--preset", "-p", default=None, help="Preset name (conservative/balanced/aggressive)")
    parser.add_argument("--policy", default=None, help="Policy JSON, or @path/to/policy.json")
    parser.add_argument(
        "--purge-expired-after",
        default=None,
        help="Destructive purge window for expired rows (30m/12h/7d/2w/3mo/1y/never)",
    )
    parser.add_argument("--yes", "-y", action="store_true", help="Confirm a policy that enables hard deletion")
    parser.add_argument("--tenant", "-t", dest="tenant_id", default=None, help="Tenant ID (default: active tenant)")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _show_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tenant", "-t", dest="tenant_id", default=None, help="Tenant ID (default: active tenant)")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


COMMANDS = {
    "show": CommandSpec(show, RetentionShowParams, _show_parser),
    "set": CommandSpec(set_policy, RetentionSetParams, _set_parser),
    "presets": CommandSpec(presets, RetentionPresetsParams),
}
