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
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("foresight_profile_export")

PROFILE_VERSION = 1
_SIGNING_CONTEXT = b"foresight-profile-export-v1"


def _signing_key() -> bytes:
    """Derive the profile signing key.

    Prefers ``FORESIGHT_PROFILE_SIGNING_KEY``; falls back to a key
    derived from ``FORESIGHT_ENCRYPTION_KEY`` (domain-separated). When
    neither is configured the signature is still produced but is not
    secret — it proves document integrity/format, not authenticity.
    """
    explicit = os.environ.get("FORESIGHT_PROFILE_SIGNING_KEY")
    if explicit:
        return hashlib.sha256(_SIGNING_CONTEXT + explicit.encode()).digest()
    enc = os.environ.get("FORESIGHT_ENCRYPTION_KEY")
    if enc:
        return hashlib.sha256(_SIGNING_CONTEXT + enc.encode()).digest()
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


def build_profile_document(
    user_id: str,
    tenant_id: str,
    *,
    agent_id: str | None = None,
    project: str | None = None,
    max_static_memories: int = 20,
    max_dynamic_memories: int = 10,
) -> dict[str, Any]:
    """Assemble the versioned export document (profile + derived layer)."""
    from .observations import list_observations
    from .profile_synthesizer import ProfileConfig, synthesize_profile
    from .reflect import list_mental_models

    profile = synthesize_profile(
        user_id,
        tenant_id,
        ProfileConfig(max_static_memories=max_static_memories, max_dynamic_memories=max_dynamic_memories),
    )

    document: dict[str, Any] = {
        "version": PROFILE_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
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
            user_id=user_id, tenant_id=tenant_id, limit=100, agent_id=agent_id, project=project
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
    user_id: str,
    tenant_id: str,
    *,
    format: str = "json",
    agent_id: str | None = None,
    project: str | None = None,
    include_signature: bool = True,
) -> str:
    """Export the profile as a signed JSON envelope or Markdown document."""
    document = build_profile_document(user_id, tenant_id, agent_id=agent_id, project=project)
    signature = sign_document(document) if include_signature else None

    if format == "markdown":
        text = _render_markdown(document)
        if not include_signature:
            text = text.rsplit("signature:", 1)[0].rstrip()
        return text
    envelope: dict[str, Any] = {"document": document}
    if signature is not None:
        envelope["signature"] = signature
        envelope["signature_alg"] = "HMAC-SHA256"
    return json.dumps(envelope, indent=2, ensure_ascii=False)
