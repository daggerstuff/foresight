"""User provisioning commands for Foresight (multi-user API keys)."""

from __future__ import annotations

import argparse
import secrets

from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.table import Table

from foresight.auth import Role, get_auth_manager
from foresight.server import _initialize_backend, init_db
from foresight.user_lanes import copy_user_lane, move_user_lane
from foresight_cli.parser import CommandSpec
from foresight_cli.utils import output as out

console = Console()


class UserCreateParams(BaseModel):
    """Arguments for :func:`create`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    username: str = Field(description="Login username (also the memory identity lane)")
    email: str = Field(description="Contact email")
    password: str | None = Field(default=None, description="Account password (generated if omitted)")
    admin: bool = Field(default=False, description="Grant admin role")


class UserListParams(BaseModel):
    """Arguments for :func:`list_users`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class UserRotateKeyParams(BaseModel):
    """Arguments for :func:`rotate_key`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    username: str = Field(description="Username to rotate")


class UserCopyLaneParams(BaseModel):
    """Arguments for :func:`copy_lane`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    src: str = Field(description="Source identity lane (user_id)")
    dst: str = Field(description="Destination identity lane (user_id)")
    dry_run: bool = Field(default=False, description="Report what would be copied without writing")


class UserMoveLaneParams(BaseModel):
    """Arguments for :func:`move_lane`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    src: str = Field(description="Source identity lane (user_id)")
    dst: str = Field(description="Destination identity lane (user_id)")
    dry_run: bool = Field(default=False, description="Report what would move without writing")


def _init_backend() -> None:
    init_db()
    _initialize_backend()


def create(params: UserCreateParams) -> None:
    """Create a user and print their API key (shown once — store it safely)."""
    _init_backend()
    manager = get_auth_manager()

    if manager.get_user_by_username(params.username):
        out.error(f"User '{params.username}' already exists")
        raise SystemExit(1)

    user = manager.create_user(
        username=params.username,
        email=params.email,
        password=params.password or secrets.token_urlsafe(24),
        role=Role.ADMIN if params.admin else Role.USER,
        tenant_access=[],
    )

    if out.get_settings().mode in ("agent", "json"):
        out.print_json(
            {
                "ok": True,
                "username": user.username,
                "user_id": user.user_id,
                "api_key": user.api_key,
                "role": user.role.value,
            }
        )
        return

    console.print(f"[bold green]✓[/] Created user [bold]{params.username}[/] ({user.role.value})")
    console.print(f"  user_id:  {user.user_id}")
    console.print(f"  api_key:  [bold yellow]{user.api_key}[/]")
    console.print(
        "\n[dim]Store the API key now — it is only shown once. Clients use it as\n"
        "Authorization: Bearer <key> plus X-User-Id: <username>.[/dim]"
    )


def list_users() -> None:
    """List provisioned users (never prints API keys)."""
    _init_backend()
    users = get_auth_manager().list_users()

    if out.get_settings().mode in ("agent", "json"):
        out.print_json(
            {
                "users": [
                    {
                        "username": u.username,
                        "user_id": u.user_id,
                        "email": u.email,
                        "role": u.role.value,
                        "is_active": u.is_active,
                        "tenant_access": u.tenant_access,
                    }
                    for u in users
                ]
            }
        )
        return

    table = Table(title="Foresight Users", border_style="bright_blue")
    for col in ("Username", "User ID", "Email", "Role", "Active", "Tenants"):
        table.add_column(col)
    for u in users:
        table.add_row(
            u.username,
            u.user_id[:12] + "…",
            u.email,
            u.role.value,
            "yes" if u.is_active else "no",
            ",".join(u.tenant_access) if u.tenant_access else "all",
        )
    console.print(table)


def rotate_key(params: UserRotateKeyParams) -> None:
    """Issue a new API key for a user (invalidates the old one)."""
    _init_backend()
    manager = get_auth_manager()

    if not manager.get_user_by_username(params.username):
        out.error(f"User '{params.username}' not found")
        raise SystemExit(1)

    new_key = manager.rotate_api_key(params.username)
    if not new_key:
        out.error(f"Failed to rotate key for '{params.username}'")
        raise SystemExit(1)

    if out.get_settings().mode in ("agent", "json"):
        out.print_json({"ok": True, "username": params.username, "api_key": new_key})
        return

    console.print(f"[bold green]✓[/] Rotated API key for [bold]{params.username}[/]")
    console.print(f"  api_key:  [bold yellow]{new_key}[/]")


def _print_lane_stats(stats: dict[str, int], verb: str, src: str, dst: str, dry_run: bool) -> None:
    if out.get_settings().mode in ("agent", "json"):
        out.print_json({"ok": True, "dry_run": dry_run, "from": src, "to": dst, "stats": stats})
        return
    label = "Would copy" if dry_run else "Copied"
    if verb == "move":
        label = "Would move" if dry_run else "Moved"
    console.print(f"[bold green]✓[/] {label} lane [bold]{src}[/] → [bold]{dst}[/]")
    for table, n in sorted(stats.items()):
        if table.endswith("_skipped"):
            continue
        skipped = stats.get(f"{table}_skipped", 0)
        suffix = f" (+{skipped} already present)" if skipped else ""
        console.print(f"  {table}: {n}{suffix}")


def copy_lane(params: UserCopyLaneParams) -> None:
    """Copy all memories from one identity lane to another (IDs remapped, idempotent)."""
    _init_backend()

    try:
        stats = copy_user_lane(params.src, params.dst, dry_run=params.dry_run)
    except ValueError as exc:
        out.error(str(exc))
        raise SystemExit(1) from None
    except Exception as exc:
        out.error(f"Copy failed (rolled back, nothing written): {exc}")
        raise SystemExit(1) from None
    _print_lane_stats(stats, "copy", params.src, params.dst, params.dry_run)


def move_lane(params: UserMoveLaneParams) -> None:
    """Move all memories from one identity lane to another (IDs unchanged)."""
    _init_backend()

    try:
        stats = move_user_lane(params.src, params.dst, dry_run=params.dry_run)
    except ValueError as exc:
        out.error(str(exc))
        raise SystemExit(1) from None
    except Exception as exc:
        out.error(f"Move failed (rolled back, nothing written): {exc}")
        raise SystemExit(1) from None
    _print_lane_stats(stats, "move", params.src, params.dst, params.dry_run)


def _create_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--username",
        "-n",
        required=True,
        help="Login username (also the memory identity lane)",
    )
    parser.add_argument("--email", "-e", required=True, help="Contact email")
    parser.add_argument("--password", "-p", default=None, help="Account password (generated if omitted)")
    parser.add_argument("--admin", action="store_true", help="Grant admin role")


def _rotate_key_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--username", "-n", required=True, help="Username to rotate")


def _copy_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("src", help="Source identity lane (user_id)")
    parser.add_argument("dst", help="Destination identity lane (user_id)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be copied without writing")


def _move_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("src", help="Source identity lane (user_id)")
    parser.add_argument("dst", help="Destination identity lane (user_id)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would move without writing")


COMMANDS = {
    "create": CommandSpec(create, UserCreateParams, _create_parser),
    "list": CommandSpec(list_users, UserListParams),
    "rotate-key": CommandSpec(rotate_key, UserRotateKeyParams, _rotate_key_parser),
    "copy": CommandSpec(copy_lane, UserCopyLaneParams, _copy_parser),
    "move": CommandSpec(move_lane, UserMoveLaneParams, _move_parser),
}
