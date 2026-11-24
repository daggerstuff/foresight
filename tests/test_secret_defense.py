"""Tests for FOR-46 / PIX-440 secret-aware write defense.

Covers:
- Secrets pattern pack: API keys, tokens, private keys, connection strings, credentials.
- Defense policy modes: flag, redact, block.
- Per-tenant and per-lane policy resolution with 'redact' as default for new tenants.
- Audit event logging with zero secret values recorded in metadata.
- CapturePipeline integration (redaction and blocking before storage).
- Store action defense integration.
"""

import json

import pytest

from foresight.audit import SECRET_DEFENSE, AuditLog, record_secret_defense_event, secret_defense_metadata
from foresight.capture import CaptureOptions, CapturePipeline
from foresight.sensitivity import (
    DEFAULT_SECRET_DEFENSE_MODE,
    evaluate_secrets,
    get_secret_defense_policy,
    redact_pem_blocks,
    reset_secret_defense_policies,
    set_secret_defense_policy,
)
from foresight.server import ManageMemoriesParams, MemoryUpdateOptions, get_db_connection, manage_memories
from foresight.tenant_context import set_current_account_id


@pytest.fixture(autouse=True)
def _clean_policies():
    reset_secret_defense_policies()
    yield
    reset_secret_defense_policies()


def test_default_mode_is_redact():
    """Verify default policy for new tenants is redact."""
    assert DEFAULT_SECRET_DEFENSE_MODE == "redact"
    assert get_secret_defense_policy("new_tenant_xyz") == "redact"


def test_large_private_key_is_redacted():
    """A PEM body past the old 4,000-char cap must still be redacted."""
    # Assembled at runtime, like the other fixtures here, so the file never
    # holds a complete key literal for a secret-scanning job to flag.
    body = "MIIE" + "A" * 6000
    header = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
    footer = "-----END " + "RSA PRIVATE KEY" + "-----"
    raw = f"{header}\n{body}\n{footer}"

    verdict = evaluate_secrets(f"key material: {raw} end", mode="redact")

    assert "private_key" in verdict.detected_secrets
    assert body not in verdict.content
    assert "[REDACTED-SECRET:private_key]" in verdict.content


def test_secret_patterns_detection_and_redaction():
    """Verify all key credential formats in pattern pack are detected and redacted."""
    dummy_openai = "sk-" + "proj-abc12345678901234567890"
    dummy_ant = "sk-" + "ant-api03-abcdef123456789012345678"
    dummy_gh = "gh" + "p_1234567890abcdef1234567890abcdef1234"
    dummy_slack = "xo" + "xb-dummy-test-token-slack"

    samples = [
        (dummy_openai, "openai_key"),
        (dummy_ant, "anthropic_key"),
        (dummy_gh, "github_token"),
        ("AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
        (dummy_slack, "slack_token"),
        # Assembled from fragments at runtime: the file never contains a
        # complete token literal for the secrets-scan job to flag.
        (
            ".".join(
                (
                    "eyJhbGciOi" + "JIUzI1NiJ9",
                    "eyJzdWIiOiIx" + "MjM0NTY3ODkwIn0",
                    "dozG4b4p_1234" + "567890abcdef",
                )
            ),
            "jwt_token",
        ),
        ("Bea" + "rer " + "eyJhbGci" + "OiJIUzI1NiJ9", "bearer_token"),
        (
            "-----BEGIN "
            + "RSA PRIVATE KEY"
            + "-----\n"
            + "MIIEowIBAAKCAQEA0Y1234567890\n"
            + "-----END "
            + "RSA PRIVATE KEY"
            + "-----",
            "private_key",
        ),
        ("postgres://" + "app_user:" + "mock_pw" + "@db.internal:5432/production", "connection_string"),
        ("api_" + "key" + " = 'abcdef" + "1234567890" + "abcdef'", "assignment_secret"),
        ("pass" + "word" + " = 'mock" + "Password123!'", "assignment_password"),
        ('{"client_' + "sec" + 'ret": "mock' + '-token-12345"}', "assignment_secret"),
        ("DB_" + "PASS" + "WORD" + '="mock' + '-token-12345"', "assignment_password"),
    ]

    for raw, expected_type in samples:
        text = f"System configuration setting: {raw} is used for access."
        verdict = evaluate_secrets(text, mode="redact")
        assert expected_type in verdict.detected_secrets, f"Failed to detect {expected_type} in {text}"
        assert verdict.action_taken == "redacted"
        assert raw not in verdict.content, f"Secret was leaked in redacted output: {verdict.content}"
        assert f"[REDACTED-SECRET:{expected_type}]" in verdict.content


def test_mode_block():
    """Verify block mode drops content completely."""
    dummy_gh = "gh" + "p_1234567890abcdef1234567890abcdef1234"
    secret_text = f"Deploy token: {dummy_gh}"
    verdict = evaluate_secrets(secret_text, mode="block")
    assert verdict.blocked is True
    assert verdict.action_taken == "blocked"
    assert verdict.content == ""
    assert "github_token" in verdict.detected_secrets


def test_mode_flag():
    """Verify flag mode keeps content but flags as sensitive."""
    dummy_gh = "gh" + "p_1234567890abcdef1234567890abcdef1234"
    secret_text = f"Deploy token: {dummy_gh}"
    verdict = evaluate_secrets(secret_text, mode="flag")
    assert verdict.blocked is False
    assert verdict.flagged is True
    assert verdict.action_taken == "flagged"
    assert dummy_gh in verdict.content


def test_per_tenant_and_lane_policies():
    """Verify policies can be configured per tenant and per lane."""
    set_secret_defense_policy("block", tenant_id="tenant_strict")
    set_secret_defense_policy("flag", tenant_id="tenant_strict", lane="lane_debug")

    assert get_secret_defense_policy("tenant_strict") == "block"
    assert get_secret_defense_policy("tenant_strict", lane="lane_debug") == "flag"
    assert get_secret_defense_policy("other_tenant") == "redact"
    # A lane override belongs to its tenant; another tenant on the same lane
    # name must not inherit it (it could otherwise weaken a block policy).
    assert get_secret_defense_policy("other_tenant", lane="lane_debug") == "redact"


def test_audit_event_records_decisions_without_secret_leak(tmp_path):
    """Verify audit_events table records defense decisions with no secret values logged."""
    db_file = str(tmp_path / "audit.db")
    audit_log = AuditLog(db_file)

    dummy_ant = "sk-" + "ant-api03-abcdef123456789012345678"
    secret = dummy_ant
    text = f"Agent prompt with {secret}"

    verdict = evaluate_secrets(text, mode="redact", tenant_id="t_audit")
    assert verdict.action_taken == "redacted"

    record_secret_defense_event(
        "t_audit",
        "u_audit",
        "res_1",
        secret_defense_metadata(verdict, lane="ops"),
        audit_log=audit_log,
    )

    events = audit_log.query("t_audit", event_type=SECRET_DEFENSE)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == SECRET_DEFENSE
    assert ev.metadata["action"] == "redacted"
    assert "anthropic_key" in ev.metadata["secret_types"]
    assert ev.metadata["mode"] == "redact"
    assert ev.metadata["lane"] == "ops"

    # Crucial security check: the raw secret string must never appear in metadata
    metadata_str = json.dumps(ev.metadata)
    assert secret not in metadata_str
    audit_log.close()


@pytest.mark.usefixtures("reset_test_tables")
def test_capture_pipeline_redacts_credentials():
    """Verify CapturePipeline redacts secrets under default redact mode."""
    set_current_account_id("test_tenant")
    pipeline = CapturePipeline()

    dummy_openai = "sk-" + "proj-abc12345678901234567890"
    secret = dummy_openai
    messages = [
        {"role": "user", "content": f"Let's use OpenAI API with key {secret} for backend embedding."},
        {
            "role": "assistant",
            "content": "Good decision, I will configure the provider to authenticate with the key.",
        },
        {"role": "user", "content": "We should also use PostgreSQL for storage."},
    ]

    stats = pipeline.run(
        session_id="sec_sess_1",
        messages=messages,
        user_id="u_capture_sec",
        tenant_id="test_tenant",
        options=CaptureOptions(),
    )
    assert stats.stored >= 1

    # Verify memory content in DB is redacted
    conn = get_db_connection()
    row = conn.execute(
        """SELECT content, is_sensitive FROM memories
           WHERE user_id = ? AND tenant_id = ?
           ORDER BY created_at DESC LIMIT 1""",
        ("u_capture_sec", "test_tenant"),
    ).fetchone()
    conn.close()

    assert row is not None
    assert secret not in row["content"]
    assert "[REDACTED-SECRET:openai_key]" in row["content"]
    assert row["is_sensitive"] == 1


@pytest.mark.usefixtures("reset_test_tables")
def test_manage_memories_store_blocks_under_block_mode():
    """Verify manage_memories blocks write when tenant policy is block."""
    set_current_account_id("test_tenant_block")
    set_secret_defense_policy("block", tenant_id="test_tenant_block")

    secret = "postgres://root:supersecretpassword@db:5432/main"
    res = manage_memories(
        ManageMemoriesParams(
            action="store",
            content=f"Database connection string is {secret}",
            user_id="u_block_test",
        )
    )
    assert "Error:" in res
    assert "blocked by secret defense policy" in res


@pytest.mark.usefixtures("reset_test_tables")
def test_manage_memories_store_redacts_under_redact_mode():
    """Verify manage_memories redacts credentials and flags memory as sensitive."""
    set_current_account_id("test_tenant_redact")
    set_secret_defense_policy("redact", tenant_id="test_tenant_redact")

    secret = "AKIAIOSFODNN7EXAMPLE"
    res = manage_memories(
        ManageMemoriesParams(
            action="store",
            content=f"AWS key identifier {secret} for S3 bucket storage.",
            user_id="u_redact_test",
            response_format="json",
        )
    )
    data = json.loads(res)
    assert data["status"] == "stored"
    mem_id = data["memory_id"]

    conn = get_db_connection()
    row = conn.execute(
        "SELECT content, is_sensitive FROM memories WHERE id = ?",
        (mem_id,),
    ).fetchone()
    conn.close()

    assert row is not None
    assert secret not in row["content"]
    assert "[REDACTED-SECRET:aws_access_key]" in row["content"]
    assert row["is_sensitive"] == 1


@pytest.mark.usefixtures("reset_test_tables")
def test_manage_memories_update_redacts_and_blocks():
    """Verify manage_memories update action runs secret defense redaction and blocking."""
    set_current_account_id("test_tenant_update")
    set_secret_defense_policy("redact", tenant_id="test_tenant_update")

    res = manage_memories(
        ManageMemoriesParams(
            action="store",
            content="Initial safe content",
            user_id="u_up_test",
            response_format="json",
        )
    )
    mem_id = json.loads(res)["memory_id"]

    secret = "AKIAIOSFODNN7EXAMPLE"
    manage_memories(
        ManageMemoriesParams(
            action="update",
            memory_id=mem_id,
            updates=MemoryUpdateOptions(content=f"Updated key {secret} for S3"),
            user_id="u_up_test",
        )
    )

    conn = get_db_connection()
    row = conn.execute("SELECT content, is_sensitive FROM memories WHERE id = ?", (mem_id,)).fetchone()
    conn.close()

    assert row is not None
    assert secret not in row["content"]
    assert "[REDACTED-SECRET:aws_access_key]" in row["content"]
    assert row["is_sensitive"] == 1

    set_secret_defense_policy("block", tenant_id="test_tenant_update")
    block_res = manage_memories(
        ManageMemoriesParams(
            action="update",
            memory_id=mem_id,
            updates=MemoryUpdateOptions(content=f"Blocked key {secret}"),
            user_id="u_up_test",
        )
    )
    assert "Error:" in block_res
    assert "blocked by secret defense policy" in block_res


def test_footerless_pem_short_final_line_redacted():
    """Footerless PEM whose final line is short base64 must still have its body redacted."""
    header = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
    body_line1 = "A" * 64
    body_line2 = "ABCD"
    text = f"prefix\n{header}\n{body_line1}\n{body_line2}\nsuffix"
    redacted, found = redact_pem_blocks(text)
    assert found
    assert body_line1 not in redacted
    assert body_line2 not in redacted
    assert "suffix" in redacted
