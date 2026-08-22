"""Tests for the Foresight Optional Encryption Layer (AES-256-GCM / PBKDF2)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from foresight.encryption import (
    ENC_PREFIX_V1,
    ForesightEncryptionEngine,
    decrypt_if_encrypted,
    encrypt_if_enabled,
    get_encryption_engine,
)


def test_encryption_disabled_by_default():
    with patch.dict(os.environ, {}, clear=True):
        engine = ForesightEncryptionEngine(master_key=None)
        assert engine.enabled is False
        assert engine.mode == "disabled"
        assert engine.is_encrypted("hello world") is False

        # When disabled, encrypt returns original plaintext
        result = engine.encrypt("unencrypted secret", tenant_id="t1", user_id="u1")
        assert result == "unencrypted secret"
        assert engine.decrypt(result, tenant_id="t1", user_id="u1") == "unencrypted secret"


def test_encryption_roundtrip_aes_gcm():
    master_key = "super-secret-production-master-key-2026"
    engine = ForesightEncryptionEngine(master_key=master_key)
    assert engine.enabled is True
    assert engine.mode == "sensitive_only"

    secret_text = "Database password: postgres://admin:hunter2@db.internal:5432/prod"
    encrypted = engine.encrypt(secret_text, tenant_id="tenant-alpha", user_id="user-123", force=True)

    assert encrypted.startswith(ENC_PREFIX_V1)
    assert encrypted != secret_text
    assert "postgres" not in encrypted
    assert "hunter2" not in encrypted
    assert engine.is_encrypted(encrypted) is True

    # Decrypt with matching tenant & user
    decrypted = engine.decrypt(encrypted, tenant_id="tenant-alpha", user_id="user-123")
    assert decrypted == secret_text


def test_multi_tenant_cryptographic_isolation():
    master_key = "shared-cluster-master-key-999"
    engine = ForesightEncryptionEngine(master_key=master_key)

    secret_text = "Confidential Patient Record #88192"
    encrypted_tenant_a = engine.encrypt(secret_text, tenant_id="tenant-a", user_id="doctor-1", force=True)

    # Attempting to decrypt with tenant-b must fail due to AEAD tag mismatch
    failed_decrypt = engine.decrypt(encrypted_tenant_a, tenant_id="tenant-b", user_id="doctor-1")
    assert "[DECRYPTION FAILED" in failed_decrypt
    assert secret_text not in failed_decrypt

    # Matching tenant decrypts cleanly
    ok_decrypt = engine.decrypt(encrypted_tenant_a, tenant_id="tenant-a", user_id="doctor-1")
    assert ok_decrypt == secret_text


def test_blind_search_tokens():
    master_key = "blind-search-key"
    engine = ForesightEncryptionEngine(master_key=master_key)

    token1 = engine.generate_blind_token("pnpm", tenant_id="t1")
    token2 = engine.generate_blind_token("pnpm", tenant_id="t1")
    token3 = engine.generate_blind_token("yarn", tenant_id="t1")
    token4 = engine.generate_blind_token("pnpm", tenant_id="t2")

    assert token1.startswith("btoken:")
    assert token1 == token2  # Deterministic for same term & tenant
    assert token1 != token3  # Different terms produce different tokens
    assert token1 != token4  # Different tenants produce different tokens


def test_encrypt_if_enabled_helper():
    with patch.dict(os.environ, {"FORESIGHT_ENCRYPTION_KEY": "test-key-123"}, clear=True):
        engine = ForesightEncryptionEngine()
        with patch("foresight.encryption.get_encryption_engine", return_value=engine):
            # Non-sensitive memory remains plaintext in sensitive_only mode
            res_plain = encrypt_if_enabled("public fact", is_sensitive=False)
            assert res_plain == "public fact"

            # Sensitive memory gets encrypted
            res_enc = encrypt_if_enabled("sensitive ssn 123-45-6789", is_sensitive=True)
            assert res_enc.startswith(ENC_PREFIX_V1)

            # Decrypt helper works
            decrypted = decrypt_if_encrypted(res_enc)
            assert decrypted == "sensitive ssn 123-45-6789"
