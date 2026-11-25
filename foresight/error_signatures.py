"""Error signature normalization for failure-signature recall (FOR-60 / FOR-64)."""

from __future__ import annotations

import re

_SIGNATURE_MAX = 160
_DETAIL_MAX = 80
_EXCEPTION_NAME = (
    r"(?:[a-z_][\w]*\.)*[A-Z][\w.]{0,80}?"
    r"(?:Error|Exception|ExceptionGroup|Fault|Panic|Timeout|Refused|Denied|Failure)"
    r"|panic|error|exception|timeout|denied|refused|failure"
)

_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:/[\w.\-]+){2,}|\b[\w.\-]+/[\w./\-]{4,}")
_HOST_RE = re.compile(r"\b(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}\b")
_HEX_RE = re.compile(r"\b(?:0x)?[0-9a-f]{8,}\b", re.IGNORECASE)
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_NUM_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")
_DETAIL_STOP_RE = re.compile(r"\s+\|\s+|;\s+|(?<=[.!?])\s+")
# Exception-shaped syntax only: a CamelCase exception name (optionally package
# qualified) or a bare keyword, always followed by a colon. A filename such as
# ``test_timeout.py`` or incidental prose therefore never becomes a signature.
_EXCEPTION_RE = re.compile(rf"(?<![\w./\-])({_EXCEPTION_NAME})\b\s*:\s*([^\n]{{0,160}})")
_TEST_FAILURE_RE = re.compile(r"\bFAILED\b[:\s]+([^\n]{0,140})", re.IGNORECASE)
_EXIT_CODE_RE = re.compile(r"exit(?:ed)?(?: with)?(?: code| status)[:=\s]+([1-9]\d*)\b", re.IGNORECASE)


def _normalize_message(msg: str) -> str:
    msg = msg.strip().lower()
    msg = _PATH_RE.sub("<path>", msg)
    msg = _HOST_RE.sub("<host>", msg)
    msg = _UUID_RE.sub("<uuid>", msg)
    msg = _HEX_RE.sub("<hex>", msg)
    msg = _NUM_RE.sub("#", msg)
    msg = _WS_RE.sub(" ", msg)
    return msg.strip(" .:-_")


def _bounded_detail(raw: str) -> str:
    msg = _DETAIL_STOP_RE.split(raw.strip(), maxsplit=1)[0]
    return _normalize_message(msg)[:_DETAIL_MAX].strip(" .:-_")


def _best_exception_match(text: str) -> re.Match[str] | None:
    """Prefer a CamelCase exception name over an incidental bare keyword mention."""
    matches = list(_EXCEPTION_RE.finditer(text))
    for match in matches:
        if match.group(1).rsplit(".", 1)[-1][:1].isupper():
            return match
    return matches[0] if matches else None


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

    match = _best_exception_match(tail)
    if match:
        exc = match.group(1).rsplit(".", 1)[-1].lower()
        detail = _bounded_detail(match.group(2))
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


def error_class_signature(signature: str | None) -> str | None:
    """Return the stable error-class key of a normalized signature (no detail)."""
    if not signature:
        return None
    head = signature.split(":", 1)[0].strip()
    return head or None


def error_signature_like_prefix(error_class: str) -> str:
    """Return the LIKE pattern matching every signature in ``error_class`` (``ESCAPE '!'``)."""
    escaped = error_class.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"{escaped}: %"
