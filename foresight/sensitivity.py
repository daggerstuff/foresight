"""Clinical/safety/privacy sensitivity tagging for memory capture.

PIX-3956: per-tenant, opt-in flag raised on memory insert when content
matches a clinical/PHI detector. The detector never inspects content
already in storage; it only inspects incoming text at the capture
boundary. Maintenance flows downstream read the resulting
``is_sensitive`` column to decide what they are allowed to touch.

Detector layers (in priority order):

1. **PII regex** — SSN, MRN, credit-card, phone, email patterns.
2. **Clinical keyword set** — small starter list, overridable via the
   ``FORESIGHT_SENSITIVITY_KEYWORDS`` env var (comma-separated, lower-case).
3. **Explicit caller flag** — the store action accepts an
   ``opts.is_sensitive`` boolean to force-on or force-off the bit.

The keyword set is intentionally conservative. False positives are
preferred to false negatives: low-risk automatic maintenance skips
sensitive rows either way, so a too-broad detector only widens that
skip set.
"""

from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass
from typing import Literal

# Shortest override keyword accepted from FORESIGHT_SENSITIVITY_KEYWORDS.
MIN_KEYWORD_CHARS = 3

# Content shorter than this is too thin to classify as sensitive.
MIN_CONTENT_CHARS_FOR_DETECTION = 20

DEFAULT_CLINICAL_KEYWORDS: tuple[str, ...] = (
    "patient",
    "diagnosis",
    "diagnostic",
    "prescription",
    "prescribed",
    "medication",
    "dosage",
    "phi",
    "protected health information",
    "medical record",
    "medical history",
    "ssn",
    "social security",
    "mrn",
    "medical record number",
    "insurance id",
    "credit card",
    "date of birth",
    "dob",
    "psychiatric",
    "therapy session",
    "therapist",
    "counselor notes",
    "substance use",
    "suicide",
    "suicidal ideation",
    "self harm",
    "abuse",
)


@dataclass(frozen=True)
class SensitivityVerdict:
    """Outcome of evaluating ``is_sensitive`` for a single content string."""

    is_sensitive: bool
    reason: str | None
    matched_pattern: str | None


SecretDefenseMode = Literal["flag", "redact", "block"]
DEFAULT_SECRET_DEFENSE_MODE: SecretDefenseMode = "redact"

# FOR-46 / PIX-440 Secrets pattern pack
_SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    "openai_key": re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "github_token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "aws_secret_key": re.compile(r"(?i)\baws_secret_access_key\s*=\s*[A-Za-z0-9/+=]{40}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[0-9a-zA-Z-]{10,}\b"),
    "jwt_token": re.compile(r"\beyJ[A-Za-z0-9-_=]+\.eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_.+/=]+\b"),
    "bearer_token": re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{20,}\b"),
    "private_key": re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        r"[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    ),
    "connection_string": re.compile(
        r"\b(?:postgres|postgresql|mysql|mongodb|redis|amqp|mssql):\/\/[^\s:@]+:[^\s:@]+@[^\s\/]+"
    ),
    "assignment_secret": re.compile(
        r"(?i)\b(?:api_key|apikey|client_secret|auth_token)\s*[:=]\s*[\"']?([A-Za-z0-9_\-\.]{16,})[\"']?"
    ),
    "assignment_password": re.compile(
        r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*[\"']?([^\s\"']{8,})[\"']?"
    ),
}

_TENANT_DEFENSE_POLICIES: dict[str, SecretDefenseMode] = {}
_LANE_DEFENSE_POLICIES: dict[str, SecretDefenseMode] = {}


def set_secret_defense_policy(
    mode: SecretDefenseMode,
    *,
    tenant_id: str | None = None,
    lane: str | None = None,
) -> None:
    """Set secret defense policy for a specific tenant or lane."""
    if mode not in ("flag", "redact", "block"):
        raise ValueError(f"Invalid secret defense mode: {mode}. Allowed: flag, redact, block")
    if lane:
        _LANE_DEFENSE_POLICIES[lane] = mode
    elif tenant_id:
        _TENANT_DEFENSE_POLICIES[tenant_id] = mode
    else:
        _TENANT_DEFENSE_POLICIES["default"] = mode


def get_secret_defense_policy(
    tenant_id: str | None = None,
    lane: str | None = None,
) -> SecretDefenseMode:
    """Resolve the active secret defense policy for a tenant/lane."""
    if lane and lane in _LANE_DEFENSE_POLICIES:
        return _LANE_DEFENSE_POLICIES[lane]
    tid = tenant_id or "default"
    if tid in _TENANT_DEFENSE_POLICIES:
        return _TENANT_DEFENSE_POLICIES[tid]
    env_mode = os.environ.get("FORESIGHT_SECRET_DEFENSE_MODE", "").strip().lower()
    if env_mode in ("flag", "redact", "block"):
        return env_mode  # type: ignore[return-value]
    return DEFAULT_SECRET_DEFENSE_MODE


def reset_secret_defense_policies() -> None:
    """Reset dynamic tenant/lane policy overrides (useful in tests)."""
    _TENANT_DEFENSE_POLICIES.clear()
    _LANE_DEFENSE_POLICIES.clear()


def _redact_secrets(text: str) -> tuple[str, list[str]]:
    """Scan and redact known secrets, returning the sanitized string and detected types."""
    detected: list[str] = []
    redacted = text
    for stype, pattern in _SECRET_PATTERNS.items():
        if pattern.search(redacted):
            detected.append(stype)
            if pattern.groups > 0:

                def _make_repl(secret_type: str):
                    def _repl(m: re.Match[str]) -> str:
                        val = m.group(1)
                        full = m.group(0)
                        return full.replace(val, f"[REDACTED-SECRET:{secret_type}]")

                    return _repl

                redacted = pattern.sub(_make_repl(stype), redacted)
            else:
                redacted = pattern.sub(f"[REDACTED-SECRET:{stype}]", redacted)
    return redacted, detected


@dataclass(frozen=True)
class SecretDefenseVerdict:
    """Outcome of evaluating credentials/secrets defense policy on text."""

    content: str
    blocked: bool
    flagged: bool
    detected_secrets: tuple[str, ...]
    mode: SecretDefenseMode
    action_taken: str  # "clean", "flagged", "redacted", "blocked"


def evaluate_secrets(
    content: str | None,
    mode: SecretDefenseMode | None = None,
    *,
    tenant_id: str | None = None,
    lane: str | None = None,
) -> SecretDefenseVerdict:
    """
    Scan content for credentials and enforce the per-tenant/lane defense policy.
    FOR-46 / PIX-440: mode=flag|redact|block before encryption and storage.
    Default mode is 'redact'.
    """
    effective_mode = mode or get_secret_defense_policy(tenant_id, lane)
    if not content:
        return SecretDefenseVerdict(
            content="",
            blocked=False,
            flagged=False,
            detected_secrets=(),
            mode=effective_mode,
            action_taken="clean",
        )

    redacted_text, detected = _redact_secrets(content)
    if not detected:
        return SecretDefenseVerdict(
            content=content,
            blocked=False,
            flagged=False,
            detected_secrets=(),
            mode=effective_mode,
            action_taken="clean",
        )

    deduped = tuple(sorted(set(detected)))
    if effective_mode == "block":
        return SecretDefenseVerdict(
            content="",
            blocked=True,
            flagged=True,
            detected_secrets=deduped,
            mode=effective_mode,
            action_taken="blocked",
        )
    if effective_mode == "flag":
        return SecretDefenseVerdict(
            content=content,
            blocked=False,
            flagged=True,
            detected_secrets=deduped,
            mode=effective_mode,
            action_taken="flagged",
        )
    return SecretDefenseVerdict(
        content=redacted_text,
        blocked=False,
        flagged=True,
        detected_secrets=deduped,
        mode=effective_mode,
        action_taken="redacted",
    )


def _compile_default_patterns() -> list[re.Pattern[str]]:
    return [
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # US SSN NNN-NN-NNNN
        re.compile(r"\b\d{9}\b"),  # bare 9-digit SSN
        re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),  # credit card
        re.compile(r"\b\d{3}-\d{3}-\d{4}\b"),  # US phone
        re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),  # email
        re.compile(r"\bMRN[:\s#-]*\d{4,}\b", re.IGNORECASE),
    ]


@functools.lru_cache(maxsize=1)
def _load_keywords() -> tuple[str, ...]:
    override = os.environ.get("FORESIGHT_SENSITIVITY_KEYWORDS", "").strip()
    if not override:
        return DEFAULT_CLINICAL_KEYWORDS
    extras = tuple(k.strip().lower() for k in override.split(",") if k.strip() and len(k.strip()) >= MIN_KEYWORD_CHARS)
    if not extras:
        return DEFAULT_CLINICAL_KEYWORDS
    return tuple(sorted(set(DEFAULT_CLINICAL_KEYWORDS) | set(extras)))


class SensitivityDetector:
    """Evaluate whether content should be flagged ``is_sensitive`` at capture.

    Thread-safe — pattern list and keyword set are captured at construction
    time and never mutated. Instances are cheap to keep on a module-level
    singleton.
    """

    def __init__(
        self,
        extra_keywords: tuple[str, ...] = (),
        extra_patterns: tuple[re.Pattern[str], ...] = (),
    ) -> None:
        keywords = set(_load_keywords())
        keywords.update(k.lower() for k in extra_keywords)
        self._keywords: tuple[str, ...] = tuple(sorted(keywords))
        self._patterns: tuple[re.Pattern[str], ...] = (
            *_compile_default_patterns(),
            *extra_patterns,
        )
        self._keyword_set = frozenset(self._keywords)

    def evaluate(self, content: str | None) -> SensitivityVerdict:
        if not content:
            return SensitivityVerdict(False, None, None)
        lowered = content.lower()
        for pattern in self._patterns:
            match = pattern.search(content)
            if match is not None:
                return SensitivityVerdict(True, "pii_pattern", match.group(0))
        sec_verdict = evaluate_secrets(content)
        if sec_verdict.detected_secrets:
            return SensitivityVerdict(True, "secret_pattern", sec_verdict.detected_secrets[0])
        for word in self._keywords:
            if word and word in lowered:
                return SensitivityVerdict(True, "clinical_keyword", word)
        return SensitivityVerdict(False, None, None)


_DEFAULT_DETECTOR = SensitivityDetector()


def detect_sensitivity(content: str | None) -> SensitivityVerdict:
    """Module-level helper that reuses a single :class:`SensitivityDetector`."""
    return _DEFAULT_DETECTOR.evaluate(content)


def resolve_is_sensitive(
    opts_is_sensitive: bool | None,
    content: str | None,
) -> tuple[bool, str | None]:
    """Combine caller override with detector verdict.

    - ``opts_is_sensitive is True`` wins (forced-on). Reason recorded as
      ``"caller_override_true"``.
    - ``opts_is_sensitive is False`` wins (forced-off). Returned reason is
      ``"caller_override_false"`` so there's an audit trail.
    - ``opts_is_sensitive is None`` defers to the detector verdict.
    """
    if opts_is_sensitive is True:
        return True, "caller_override"
    if opts_is_sensitive is False:
        return False, None

    if isinstance(content, str) and len(content) < MIN_CONTENT_CHARS_FOR_DETECTION:
        return False, None

    verdict = detect_sensitivity(content)
    return verdict.is_sensitive, verdict.reason
