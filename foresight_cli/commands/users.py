"""User provisioning commands for Foresight (multi-user API keys)."""

from __future__ import annotations

import secrets

import typer
from rich.console import Console
from rich.table import Table

from foresight.auth import Role, get_auth_manager
from foresight.server import _initialize_backend, init_db
from foresight_cli.utils import output as out

console = Console()
app = typer.Typer(help="Provision users and API keys for remote/multi-user access.")


def _init_backend() -> None:
    init_db()
    _initialize_backend()


@app.command(name="create")
def create(
    username: str = typer.Option(..., "--username", "-n", help="Login username (also the memory identity lane)"),
    email: str = typer.Option(..., "--email", "-e", help="Contact email"),
    password: str | None = typer.Option(None, "--password", "-p", help="Account password (generated if omitted)"),
    admin: bool = typer.Option(False, "--admin", help="Grant admin role"),
):
    """Create a user and print their API key (shown once — store it safely)."""
    _init_backend()
    manager = get_auth_manager()

    if manager.get_user_by_username(username):
        out.error(f"User '{username}' already exists")
        raise typer.Exit(1)

    user = manager.create_user(
        username=username,
        email=email,
        password=password or secrets.token_urlsafe(24),
        role=Role.ADMIN if admin else Role.USER,
        tenant_access=[],  # all tenants
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

    console.print(f"[bold green]✓[/] Created user [bold]{username}[/] ({user.role.value})")
    console.print(f"  user_id:  {user.user_id}")
    console.print(f"  api_key:  [bold yellow]{user.api_key}[/]")
    console.print(
        "\n[dim]Store the API key now — it is only shown once. Clients use it as\n"
        "Authorization: Bearer <key> plus X-User-Id: <username>.[/dim]"
    )


@app.command(name="list")
def list_users():
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


@app.command(name="rotate-key")
def rotate_key(
    username: str = typer.Option(..., "--username", "-n", help="Username to rotate"),
):
    """Issue a new API key for a user (invalidates the old one)."""
    _init_backend()
    manager = get_auth_manager()

    if not manager.get_user_by_username(username):
        out.error(f"User '{username}' not found")
        raise typer.Exit(1)

    new_key = manager.rotate_api_key(username)
    if not new_key:
        out.error(f"Failed to rotate key for '{username}'")
        raise typer.Exit(1)

    if out.get_settings().mode in ("agent", "json"):
        out.print_json({"ok": True, "username": username, "api_key": new_key})
        return

    console.print(f"[bold green]✓[/] Rotated API key for [bold]{username}[/]")
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


@app.command(name="copy")
def copy_lane(
    src: str = typer.Argument(..., help="Source identity lane (user_id)"),
    dst: str = typer.Argument(..., help="Destination identity lane (user_id)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be copied without writing"),
):
    """Copy all memories from one identity lane to another (IDs remapped, idempotent)."""
    _init_backend()
    from foresight.user_lanes import copy_user_lane

    try:
        stats = copy_user_lane(src, dst, dry_run=dry_run)
    except ValueError as exc:
        out.error(str(exc))
        raise typer.Exit(1) from None
    except Exception as exc:
        out.error(f"Copy failed (rolled back, nothing written): {exc}")
        raise typer.Exit(1) from None
    _print_lane_stats(stats, "copy", src, dst, dry_run)


@app.command(name="move")
def move_lane(
    src: str = typer.Argument(..., help="Source identity lane (user_id)"),
    dst: str = typer.Argument(..., help="Destination identity lane (user_id)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would move without writing"),
):
    """Move all memories from one identity lane to another (IDs unchanged)."""
    _init_backend()
    from foresight.user_lanes import move_user_lane

    try:
        stats = move_user_lane(src, dst, dry_run=dry_run)
    except ValueError as exc:
        out.error(str(exc))
        raise typer.Exit(1) from None
    except Exception as exc:
        out.error(f"Move failed (rolled back, nothing written): {exc}")
        raise typer.Exit(1) from None
    _print_lane_stats(stats, "move", src, dst, dry_run)
