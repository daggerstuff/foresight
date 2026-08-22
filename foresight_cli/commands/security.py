"""Security and Encryption management commands for Foresight."""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from foresight.encryption import ForesightEncryptionEngine, get_encryption_engine
from foresight.server import _initialize_backend, get_db_connection, init_db
from foresight_cli.utils import config as cfg, output as out

console = Console()
app = typer.Typer(help="Manage security, sensitivity policies, and optional AES-256-GCM encryption.")


def _init_backend() -> None:
    init_db()
    _initialize_backend()


@app.command(name="status")
def status():
    """View current encryption status, active algorithm, and security mode."""
    engine = get_encryption_engine()
    st = engine.get_status()

    if out.get_settings().mode in ("agent", "json"):
        out.print_json(st.to_dict())
        return

    table = Table(title="🛡️ Foresight Security & Encryption Status", border_style="bright_blue")
    table.add_column("Property", style="bold cyan")
    table.add_column("Value", style="bold white")

    table.add_row("Encryption Enabled", "🟢 ACTIVE" if st.enabled else "⚪ DISABLED")
    table.add_row("Security Mode", st.mode.upper())
    table.add_row("Algorithm", st.algorithm)
    table.add_row("Key Configured", "✅ Yes (FORESIGHT_ENCRYPTION_KEY)" if st.key_configured else "❌ None")
    table.add_row("Cryptography Library", "✅ Available (AES-256-GCM / PBKDF2)" if st.library_available else "❌ Missing")
    table.add_row("Sensitivity Filter (PIX-3956)", "🟢 Active (PII/PHI Auto-Gating)")

    console.print(table)
    if not st.enabled:
        console.print("\n[dim]To enable encryption, set [bold]FORESIGHT_ENCRYPTION_KEY='your-secret-passphrase'[/bold] in your .env file.[/dim]")
    elif st.mode == "sensitive_only":
        console.print("\n[dim]Currently encrypting sensitive memories only. To encrypt the entire store, set [bold]FORESIGHT_ENCRYPT_ALL=true[/bold].[/dim]")


@app.command(name="encrypt-all")
def encrypt_all(
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
    tenant_id: str | None = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
):
    """Retroactively encrypt all plaintext memories for the active user/tenant."""
    _init_backend()
    resolved_uid = cfg.get_user_id(user_id)
    engine = get_encryption_engine()

    if not engine.enabled:
        out.error("Cannot encrypt: No encryption key configured. Set FORESIGHT_ENCRYPTION_KEY.")
        raise typer.Exit(1)

    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, content FROM memories WHERE user_id = ? AND tenant_id = ?",
            (resolved_uid, tenant_id),
        ).fetchall()

        encrypted_count = 0
        already_encrypted = 0

        for r in rows:
            content = r["content"] or ""
            if engine.is_encrypted(content):
                already_encrypted += 1
            else:
                ciphertext = engine.encrypt(content, tenant_id=tenant_id, user_id=resolved_uid, force=True)
                conn.execute(
                    "UPDATE memories SET content = ? WHERE id = ?",
                    (ciphertext, r["id"]),
                )
                encrypted_count += 1

        conn.commit()
        out.success(f"Encrypted {encrypted_count} memories at rest (AES-256-GCM). Already encrypted: {already_encrypted}.")
    finally:
        conn.close()


@app.command(name="rotate-key")
def rotate_key(
    old_key: str = typer.Option(..., "--old-key", prompt=True, hide_input=True, help="Existing master encryption key"),
    new_key: str = typer.Option(..., "--new-key", prompt=True, hide_input=True, help="New master encryption key to migrate to"),
    user_id: str | None = typer.Option(None, "--user-id", "-u", help="User ID override"),
    tenant_id: str | None = typer.Option("default", "--tenant", "-t", help="Tenant ID"),
):
    """Re-encrypt all stored memories under a new master encryption key."""
    _init_backend()
    resolved_uid = cfg.get_user_id(user_id)
    conn = get_db_connection()
    try:
        engine = get_encryption_engine()
        res = engine.rotate_key(
            old_master_key=old_key,
            new_master_key=new_key,
            conn=conn,
            tenant_id=tenant_id,
            user_id=resolved_uid,
        )
        out.success(f"Key rotation complete: {res['rotated_count']} memories re-encrypted under new key.")
        if res.get("failed_count"):
            out.warning(f"Failed to decrypt {res['failed_count']} memories with the provided old key.")
    finally:
        conn.close()
