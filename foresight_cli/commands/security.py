"""Security and Encryption management commands for Foresight."""

from __future__ import annotations

import argparse
import getpass
import sys

from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.table import Table

from foresight.encryption import get_encryption_engine
from foresight.server import _initialize_backend, get_db_connection, init_db
from foresight_cli.parser import CommandSpec
from foresight_cli.utils import config as cfg, output as out

console = Console()


class SecurityStatusParams(BaseModel):
    """Arguments for :func:`status`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class SecurityEncryptAllParams(BaseModel):
    """Arguments for :func:`encrypt_all`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    user_id: str | None = Field(default=None, description="User ID override")
    tenant_id: str = Field(default="default", description="Tenant ID")


class SecurityRotateKeyParams(BaseModel):
    """Arguments for :func:`rotate_key`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    old_key: str | None = Field(default=None, description="Existing master encryption key")
    new_key: str | None = Field(default=None, description="New master encryption key to migrate to")
    user_id: str | None = Field(default=None, description="User ID override")
    tenant_id: str = Field(default="default", description="Tenant ID")


def _init_backend() -> None:
    init_db()
    _initialize_backend()


def _prompt_secret(label: str) -> str:
    try:
        return getpass.getpass(f"{label}: ")
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("Aborted.\n")
        raise SystemExit(1) from None


def status() -> None:
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
    table.add_row(
        "Cryptography Library", "✅ Available (AES-256-GCM / PBKDF2)" if st.library_available else "❌ Missing"
    )
    table.add_row("Sensitivity Filter (PIX-3956)", "🟢 Active (PII/PHI Auto-Gating)")

    console.print(table)
    if not st.enabled:
        console.print(
            "\n[dim]To enable encryption, set "
            "[bold]FORESIGHT_ENCRYPTION_KEY='your-secret-passphrase'[/bold] in your .env file.[/dim]"
        )
    elif st.mode == "sensitive_only":
        console.print(
            "\n[dim]Currently encrypting sensitive memories only. To encrypt "
            "the entire store, set [bold]FORESIGHT_ENCRYPT_ALL=true[/bold].[/dim]"
        )


def encrypt_all(params: SecurityEncryptAllParams) -> None:
    """Retroactively encrypt all plaintext memories for the active user/tenant."""
    _init_backend()
    resolved_uid = cfg.get_user_id(params.user_id)
    engine = get_encryption_engine()

    if not engine.enabled:
        out.error("Cannot encrypt: No encryption key configured. Set FORESIGHT_ENCRYPTION_KEY.")
        raise SystemExit(1)

    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, content FROM memories WHERE user_id = ? AND tenant_id = ?",
            (resolved_uid, params.tenant_id),
        ).fetchall()

        encrypted_count = 0
        already_encrypted = 0

        for r in rows:
            content = r["content"] or ""
            if engine.is_encrypted(content):
                already_encrypted += 1
            else:
                ciphertext = engine.encrypt(
                    content,
                    tenant_id=params.tenant_id,
                    user_id=resolved_uid,
                    force=True,
                )
                conn.execute(
                    "UPDATE memories SET content = ? WHERE id = ?",
                    (ciphertext, r["id"]),
                )
                encrypted_count += 1

        conn.commit()
        out.done(f"Encrypted {encrypted_count} memories at rest (AES-256-GCM). Already encrypted: {already_encrypted}.")
    finally:
        conn.close()


def rotate_key(params: SecurityRotateKeyParams) -> None:
    """Re-encrypt all stored memories under a new master encryption key."""
    old_key = params.old_key if params.old_key is not None else _prompt_secret("Old key")
    new_key = params.new_key if params.new_key is not None else _prompt_secret("New key")

    _init_backend()
    resolved_uid = cfg.get_user_id(params.user_id)
    conn = get_db_connection()
    try:
        engine = get_encryption_engine()
        res = engine.rotate_key(
            old_master_key=old_key,
            new_master_key=new_key,
            conn=conn,
            tenant_id=params.tenant_id,
            user_id=resolved_uid,
        )
        out.done(f"Key rotation complete: {res['rotated_count']} memories re-encrypted under new key.")
        if res.get("failed_count"):
            out.warn(f"Failed to decrypt {res['failed_count']} memories with the provided old key.")
    finally:
        conn.close()


def _encrypt_all_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")
    parser.add_argument("--tenant", "-t", dest="tenant_id", default="default", help="Tenant ID")


def _rotate_key_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--old-key", default=None, help="Existing master encryption key")
    parser.add_argument("--new-key", default=None, help="New master encryption key to migrate to")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")
    parser.add_argument("--tenant", "-t", dest="tenant_id", default="default", help="Tenant ID")


COMMANDS = {
    "status": CommandSpec(status, SecurityStatusParams),
    "encrypt-all": CommandSpec(encrypt_all, SecurityEncryptAllParams, _encrypt_all_parser),
    "rotate-key": CommandSpec(rotate_key, SecurityRotateKeyParams, _rotate_key_parser),
}
