"""Error signature normalization for failure-signature recall (FOR-60 / FOR-64)."""

from __future__ import annotations

import re

_SIGNATURE_MAX = 160

_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:/[\w.\-]+){2,}|\b[\w.\-]+/[\w./\-]{4,}")
_HEX_RE = re.compile(r"\b(?:0x)?[0-9a-f]{8,}\b", re.IGNORECASE)
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_NUM_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")
_EXCEPTION_RE = re.compile(
    r"([A-Za-z_][\w.]{0,80}?(?:Error|Exception|ExceptionGroup|Fault|Panic|Timeout|Refused|Denied|Failure))"
    r"\b\s*[:\-]?\s*([^\n]{0,160})",
    re.IGNORECASE,
)
_TEST_FAILURE_RE = re.compile(r"\bFAILED\b[:\s]+([^\n]{0,140})", re.IGNORECASE)
_EXIT_CODE_RE = re.compile(r"exit(?:ed)?(?: with)?(?: code| status)[:=\s]+([1-9]\d*)\b", re.IGNORECASE)


def _normalize_message(msg: str) -> str:
    msg = msg.strip().lower()
    msg = _PATH_RE.sub("<path>", msg)
    msg = _UUID_RE.sub("<uuid>", msg)
    msg = _HEX_RE.sub("<hex>", msg)
    msg = _NUM_RE.sub("#", msg)
    msg = _WS_RE.sub(" ", msg)
    return msg.strip(" .:-_")


def normalize_error_signature(text: str | None) -> str | None:
    """Return a stable, normalized signature for the dominant error in ``text``.

    Normalization strips volatile tokens (paths, hex ids, uuids, numbers) so
    the same failure class yields the same signature across runs. Returns
    ``None`` when the text carries no recognizable failure signal.
    """
    if not text:
        return None
    # Scan the whole text: a long session can carry its only failure early,
    # and a tail window would then record no signature at all.
    tail = text

    match = _EXCEPTION_RE.search(tail)
    if match:
        exc = match.group(1).split(".")[-1].lower()
        detail = _normalize_message(match.group(2))
        sig = f"{exc}: {detail}" if detail else exc
        return sig[:_SIGNATURE_MAX]

    match = _TEST_FAILURE_RE.search(tail)
    if match:
        detail = _normalize_message(match.group(1))
        return f"test-failure: {detail}"[:_SIGNATURE_MAX] if detail else None

    match = _EXIT_CODE_RE.search(tail)
    if match:
        return f"exit-code-{match.group(1)}"

    return None
