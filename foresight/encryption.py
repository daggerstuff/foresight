"""Optional Encryption Layer for Foresight.

Provides enterprise-grade, field-level Authenticated Encryption at Rest (AEAD)
using AES-256-GCM with PBKDF2 key derivation and multi-tenant salt isolation.

Modes:
- **DISABLED** (Default): Operates with standard plaintext storage.
- **OPT-IN SENSITIVE**: Automatically encrypts memories tagged `is_sensitive=1`
  or stored with `encrypt=True`.
- **ENCRYPT-ALL (Full Store)**: When `FORESIGHT_ENCRYPT_ALL=true`, encrypts all
  memory contents and context blocks at rest.

Payload Format:
  ``enc:v1:<base64(salt[16] + nonce[12] + ciphertext_with_tag)>``
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("foresight_encryption")

# Check cryptography library availability
try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False
    AESGCM = None  # type: ignore
    PBKDF2HMAC = None  # type: ignore
    hashes = None  # type: ignore

ENC_PREFIX_V1 = "enc:v1:"
PBKDF2_ITERATIONS = 100_000
SALT_SIZE = 16
NONCE_SIZE = 12  # 96-bit standard for AES-GCM


@dataclass
class EncryptionStatus:
    """Status summary of the encryption engine."""

    enabled: bool
    mode: str  # "disabled" | "sensitive_only" | "encrypt_all"
    algorithm: str
    key_configured: bool
    library_available: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "algorithm": self.algorithm,
            "key_configured": self.key_configured,
            "library_available": self.library_available,
        }


class ForesightEncryptionEngine:
    """AES-256-GCM field-level encryption engine with per-tenant key derivation."""

    def __init__(self, master_key: str | bytes | None = None) -> None:
        env_key = os.environ.get("FORESIGHT_ENCRYPTION_KEY") or os.environ.get("FORESIGHT_MASTER_KEY")
        raw_key = master_key if master_key is not None else env_key
        if isinstance(raw_key, str):
            self._master_key: bytes | None = raw_key.encode("utf-8") if raw_key else None
        else:
            self._master_key = raw_key

        self.encrypt_all = os.environ.get("FORESIGHT_ENCRYPT_ALL", "false").lower() in ("true", "1", "yes")
        self.enabled = bool(self._master_key) and HAS_CRYPTOGRAPHY

    @property
    def mode(self) -> str:
        if not self.enabled:
            return "disabled"
        return "encrypt_all" if self.encrypt_all else "sensitive_only"

    def get_status(self) -> EncryptionStatus:
        return EncryptionStatus(
            enabled=self.enabled,
            mode=self.mode,
            algorithm="AES-256-GCM" if self.enabled else "None",
            key_configured=bool(self._master_key),
            library_available=HAS_CRYPTOGRAPHY,
        )

    def is_encrypted(self, text: str | None) -> bool:
        """Check if a string is encrypted with Foresight envelope format."""
        if not text or not isinstance(text, str):
            return False
        return text.startswith(ENC_PREFIX_V1)

    def _derive_key(self, salt: bytes, tenant_id: str = "default", user_id: str = "default") -> bytes:
        """Derive 256-bit key from master key + tenant/user context + salt."""
        if not self._master_key:
            raise ValueError("No master encryption key configured in FORESIGHT_ENCRYPTION_KEY")

        tenant_context = f"{tenant_id}:{user_id}".encode()
        combined_salt = hashlib.sha256(salt + tenant_context).digest()

        if HAS_CRYPTOGRAPHY:
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=combined_salt,
                iterations=PBKDF2_ITERATIONS,
            )
            return kdf.derive(self._master_key)

        # Fallback pure Python PBKDF2
        return hashlib.pbkdf2_hmac(
            "sha256",
            self._master_key,
            combined_salt,
            PBKDF2_ITERATIONS,
            dklen=32,
        )

    def encrypt(
        self,
        plaintext: str | None,
        tenant_id: str = "default",
        user_id: str = "default",
        force: bool = False,
    ) -> str:
        """Encrypt plaintext into `enc:v1:<base64>` payload using AES-256-GCM.

        If encryption is not enabled and `force` is False, returns plaintext as-is.
        If plaintext is already encrypted, returns it unchanged.
        """
        if plaintext is None:
            return ""
        if not plaintext:
            return ""
        if self.is_encrypted(plaintext):
            return plaintext
        if not self.enabled and not force:
            return plaintext

        if not HAS_CRYPTOGRAPHY:
            raise RuntimeError("cryptography library required for AES-256-GCM encryption")

        salt = secrets.token_bytes(SALT_SIZE)
        key = self._derive_key(salt, tenant_id=tenant_id, user_id=user_id)
        nonce = secrets.token_bytes(NONCE_SIZE)

        aesgcm = AESGCM(key)
        # Associated Authenticated Data binds ciphertext to tenant & user
        aad = f"{tenant_id}:{user_id}".encode()
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad)

        envelope = salt + nonce + ciphertext
        b64_payload = base64.b64encode(envelope).decode("utf-8")
        return f"{ENC_PREFIX_V1}{b64_payload}"

    def decrypt(
        self,
        payload: str | None,
        tenant_id: str = "default",
        user_id: str = "default",
    ) -> str:
        """Decrypt `enc:v1:<base64>` payload into plaintext.

        If payload is not encrypted (plain text), returns it as-is.
        """
        if payload is None:
            return ""
        if not self.is_encrypted(payload):
            return payload

        if not self._master_key:
            logger.warning("Attempted to decrypt encrypted memory but no FORESIGHT_ENCRYPTION_KEY configured")
            return "[ENCRYPTED - Key Required]"

        if not HAS_CRYPTOGRAPHY:
            raise RuntimeError("cryptography library required for AES-256-GCM decryption")

        try:
            raw_b64 = payload[len(ENC_PREFIX_V1) :]
            envelope = base64.b64decode(raw_b64)

            salt = envelope[:SALT_SIZE]
            nonce = envelope[SALT_SIZE : SALT_SIZE + NONCE_SIZE]
            ciphertext = envelope[SALT_SIZE + NONCE_SIZE :]

            key = self._derive_key(salt, tenant_id=tenant_id, user_id=user_id)
            aesgcm = AESGCM(key)
            aad = f"{tenant_id}:{user_id}".encode()
            decrypted_bytes = aesgcm.decrypt(nonce, ciphertext, aad)
            return decrypted_bytes.decode("utf-8")
        except Exception as exc:
            logger.error("Decryption failed for tenant %s, user %s: %s", tenant_id, user_id, exc)
            return "[DECRYPTION FAILED - Invalid Key or Corrupted Ciphertext]"

    def generate_blind_token(self, term: str, tenant_id: str = "default") -> str:
        """Generate deterministic HMAC-SHA256 token for encrypted blind search."""
        if not self._master_key:
            return ""
        key = hashlib.sha256(self._master_key + f":blind:{tenant_id}".encode()).digest()
        token = hmac.new(key, term.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()
        return f"btoken:{token[:16]}"

    def rotate_key(
        self,
        old_master_key: str,
        new_master_key: str,
        conn: Any,
        tenant_id: str = "default",
        user_id: str = "default",
    ) -> dict[str, Any]:
        """Re-encrypt all stored memories for a tenant/user from old key to new key."""
        old_engine = ForesightEncryptionEngine(master_key=old_master_key)
        new_engine = ForesightEncryptionEngine(master_key=new_master_key)

        rows = conn.execute(
            "SELECT id, content FROM memories WHERE user_id = ? AND tenant_id = ?",
            (user_id, tenant_id),
        ).fetchall()

        rotated_count = 0
        failed_count = 0

        for r in rows:
            content = r["content"] or ""
            if old_engine.is_encrypted(content):
                plain = old_engine.decrypt(content, tenant_id=tenant_id, user_id=user_id)
                if plain.startswith("[DECRYPTION FAILED"):
                    failed_count += 1
                    continue
                new_ciphertext = new_engine.encrypt(plain, tenant_id=tenant_id, user_id=user_id, force=True)
                conn.execute(
                    "UPDATE memories SET content = ? WHERE id = ?",
                    (new_ciphertext, r["id"]),
                )
                rotated_count += 1

        conn.commit()
        return {
            "ok": True,
            "total_rows": len(rows),
            "rotated_count": rotated_count,
            "failed_count": failed_count,
        }


# Module-level singleton
_DEFAULT_ENCRYPTION_ENGINE: ForesightEncryptionEngine | None = None


def get_encryption_engine() -> ForesightEncryptionEngine:
    """Retrieve the global ForesightEncryptionEngine instance."""
    global _DEFAULT_ENCRYPTION_ENGINE
    if _DEFAULT_ENCRYPTION_ENGINE is None:
        _DEFAULT_ENCRYPTION_ENGINE = ForesightEncryptionEngine()
    return _DEFAULT_ENCRYPTION_ENGINE


def encrypt_if_enabled(
    text: str | None,
    is_sensitive: bool = False,
    tenant_id: str = "default",
    user_id: str = "default",
) -> str:
    """Convenience helper to encrypt content if policy matches (sensitive or encrypt_all)."""
    engine = get_encryption_engine()
    if not engine.enabled:
        return text or ""
    if engine.encrypt_all or is_sensitive:
        return engine.encrypt(text, tenant_id=tenant_id, user_id=user_id)
    return text or ""


def decrypt_if_encrypted(
    text: str | None,
    tenant_id: str = "default",
    user_id: str = "default",
) -> str:
    """Convenience helper to decrypt content if encrypted."""
    engine = get_encryption_engine()
    if not text or not engine.is_encrypted(text):
        return text or ""
    return engine.decrypt(text, tenant_id=tenant_id, user_id=user_id)
