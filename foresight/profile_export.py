"""Portable, HMAC-signed profile exports (Phase 5).

Renders the synthesized user profile — including the derived layer
(mental models and active observations) — as a versioned, signed
document that can be exported via the ``export_profile`` MCP tool or
``GET /profile/export`` and verified offline.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from .observations import list_observations
from .profile_synthesizer import ProfileConfig, synthesize_profile
from .reflect import UserScope, list_mental_models

logger = logging.getLogger("foresight_profile_export")

PROFILE_VERSION = 1
SKILL_SCHEMA_VERSION = 1
_SIGNING_CONTEXT = b"foresight-profile-export-v1"


class SkillSignatureUnavailableError(RuntimeError):
    """Raised when no configured secret can authenticate a skill signature."""


def _configured_signing_key() -> bytes | None:
    """Return the key derived from a configured secret, or ``None``."""
    explicit = os.environ.get("FORESIGHT_PROFILE_SIGNING_KEY")
    if explicit:
        return hashlib.sha256(_SIGNING_CONTEXT + explicit.encode()).digest()
    enc = os.environ.get("FORESIGHT_ENCRYPTION_KEY")
    if enc:
        return hashlib.sha256(_SIGNING_CONTEXT + enc.encode()).digest()
    return None


def skill_signing_configured() -> bool:
    """True when a configured secret can authenticate skill signatures."""
    return _configured_signing_key() is not None


def _signing_key() -> bytes:
    """Derive the profile signing key.

    Prefers ``FORESIGHT_PROFILE_SIGNING_KEY``; falls back to a key
    derived from ``FORESIGHT_ENCRYPTION_KEY`` (domain-separated). When
    neither is configured the exported document is still signed with a
    public development key — that proves integrity/format, not
    authenticity. Skill signatures never accept it: they require a
    configured secret and fail closed otherwise.
    """
    key = _configured_signing_key()
    if key is not None:
        return key
    return hashlib.sha256(_SIGNING_CONTEXT + b"unsigned-dev-key").digest()


def _canonical(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_document(document: dict[str, Any]) -> str:
    """Return the hex HMAC-SHA256 signature for a profile document."""
    return hmac.new(_signing_key(), _canonical(document), hashlib.sha256).hexdigest()


def verify_document(document: dict[str, Any], signature: str) -> bool:
    """Verify a signature against a document (constant-time)."""
    expected = sign_document(document)
    return hmac.compare_digest(expected, str(signature))


def sign_skill_document(skill_document: dict[str, Any]) -> str:
    """Return the hex HMAC-SHA256 signature for a crystallized skill package.

    Raises:
        SkillSignatureUnavailableError: when no signing secret is configured, so an
            untrusted artifact can never carry a verifying signature.
    """
    key = _configured_signing_key()
    if key is None:
        raise SkillSignatureUnavailableError(
            "skill signing requires FORESIGHT_PROFILE_SIGNING_KEY or FORESIGHT_ENCRYPTION_KEY"
        )
    return hmac.new(key, _canonical(skill_document), hashlib.sha256).hexdigest()


def verify_skill_document(skill_document: dict[str, Any], signature: str) -> bool:
    """Verify a crystallized skill signature; fails closed without a configured secret."""
    key = _configured_signing_key()
    if key is None:
        return False
    expected = hmac.new(key, _canonical(skill_document), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature))


def build_profile_document(
    scope: UserScope,
    *,
    max_static_memories: int = 20,
    max_dynamic_memories: int = 10,
) -> dict[str, Any]:
    """Assemble the versioned export document (profile + derived layer)."""
    user_id = scope.user_id
    tenant_id = scope.tenant_id
    agent_id = scope.agent_id
    project = scope.project

    profile = synthesize_profile(
        user_id,
        tenant_id,
        ProfileConfig(max_static_memories=max_static_memories, max_dynamic_memories=max_dynamic_memories),
    )

    document: dict[str, Any] = {
        "version": PROFILE_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "project": project,
        "profile": profile,
        "mental_models": [],
        "observations": [],
    }
    try:
        document["mental_models"] = list_mental_models(
            user_id=user_id, tenant_id=tenant_id, agent_id=agent_id, limit=100
        )
    except Exception:
        logger.debug("mental model export failed (non-fatal)", exc_info=True)
    try:
        document["observations"] = list_observations(
            UserScope(user_id=user_id, tenant_id=tenant_id, agent_id=agent_id, project=project),
            limit=100,
        )
    except Exception:
        logger.debug("observation export failed (non-fatal)", exc_info=True)
    return document


def _render_markdown(document: dict[str, Any]) -> str:
    lines = [
        f"# Foresight Profile Export (v{document['version']})",
        "",
        f"- Exported: {document['exported_at']}",
        f"- Tenant: {document['tenant_id']}",
        f"- User: {document['user_id']}",
    ]
    if document.get("agent_id") or document.get("project"):
        lines.append(
            f"- Namespace: agent={document.get('agent_id') or 'default'} project={document.get('project') or 'default'}"
        )
    profile = document.get("profile") or {}
    for section in ("static", "dynamic"):
        items = profile.get(section) or []
        if not items:
            continue
        lines += ["", f"## {section.capitalize()}", ""]
        lines += [f"- {item}" for item in items]
    models = document.get("mental_models") or []
    if models:
        lines += ["", "## Mental Models", ""]
        for m in models:
            lines.append(f"- **Q:** {m.get('question_text', '')}")
            lines.append(f"  **A:** {str(m.get('answer', ''))[:500]}")
    observations = document.get("observations") or []
    if observations:
        lines += ["", "## Observations", ""]
        for o in observations:
            lines.append(
                f"- {o.get('observation_text', '')} (confidence: {o.get('confidence', 0):.2f}, "
                f"evidence: {o.get('evidence_count', 0)}, version: {o.get('version', 1)})"
            )
    lines += ["", "---", f"signature: {sign_document(document)}"]
    return "\n".join(lines)


def export_profile(
    scope: UserScope,
    *,
    output_format: str = "json",
    include_signature: bool = True,
) -> str:
    """Export the profile as a signed JSON envelope or Markdown document."""
    document = build_profile_document(scope)
    signature = sign_document(document) if include_signature else None

    if output_format == "markdown":
        text = _render_markdown(document)
        if not include_signature:
            text = text.rsplit("signature:", 1)[0].rstrip()
        return text
    envelope: dict[str, Any] = {"document": document}
    if signature is not None:
        envelope["signature"] = signature
        envelope["signature_alg"] = "HMAC-SHA256"
    return json.dumps(envelope, indent=2, ensure_ascii=False)
