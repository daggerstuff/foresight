"""Foresight plugin SDK for Python hook/provider targets (FOR-69).

Single-file, stdlib-only module so agent plugins can vendor or import it
without third-party dependencies. Distributed copies live next to the hook
that uses them (see ``plugins/claude/install.sh``); in this repo the module
is importable from ``plugins/sdk/python``.

Provides the pieces every Python plugin was hand-rolling:

- server discovery (``FORESIGHT_MCP_URL`` / ``FORESIGHT_URL`` / default
  ``http://127.0.0.1:8764``)
- credential loading (environment first, then
  ``~/.config/foresight/credentials.env``, KEY=VALUE, chmod 600)
- auth headers (``Authorization: Bearer`` + ``X-User-Id`` lane)
- fail-safe HTTP POST (``post_json``) and hook payload parsing
- a small ``ForesightClient`` covering the REST surfaces hooks actually use
  (``/ui/api/inject``, ``/ui/api/capture``, ``/session/transcript``,
  ``/ui/api/telemetry/gating``) plus MCP ``tools/call``

Fail-safe contract: ``post_json`` and ``ForesightClient.post`` raise
``ForesightError``/``ForesightHTTPError`` on transport or HTTP failure and never
return partial data. The high-level ``ForesightClient`` helpers are fail-safe:
they swallow ``ForesightError`` and return their documented ``None`` so a down
server can never break the host agent session.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import sys
import urllib.parse
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

__version__ = "0.1.0"

DEFAULT_BASE_URL = "http://127.0.0.1:8764"
DEFAULT_TIMEOUT = 12.0
MCP_PROTOCOL_VERSION = "2026-07-28"


class ForesightError(RuntimeError):
    """Base error for SDK transport/protocol failures."""


class ForesightHTTPError(ForesightError):
    """Raised by ``post_json`` when the server answers >= 400."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"HTTP {status}: {reason}")
        self.status = status
        self.reason = reason


# -- Credentials -------------------------------------------------------------


def default_credentials_path(env: Mapping[str, str] | None = None) -> str:
    """Return the conventional credentials.env path (XDG-aware)."""
    environ: Mapping[str, str] = os.environ if env is None else env
    config_home = environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config_home, "foresight", "credentials.env")


def load_env_file(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE env file. Missing/unreadable files yield ``{}``."""
    values: dict[str, str] = {}
    with suppress(Exception), open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Credentials:
    """Resolved connection settings. Secrets are never logged or repr'd."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = field(default="", repr=False)
    identity: str = ""
    user_id: str = ""
    extra: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def lane(self) -> str:
        """Memory lane derived from identity (``alice@acme`` -> ``alice``)."""
        return self.identity.split("@", 1)[0] if self.identity else ""

    @property
    def mcp_url(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/mcp") else f"{base}/mcp"

    @property
    def rest_url(self) -> str:
        """REST base: ``base_url`` without a terminal ``/mcp``.

        The REST endpoints (``/ui/api/inject``, ``/ui/api/capture``,
        ``/session/transcript``, ``/ui/api/telemetry/gating``) are registered at
        the app root, so posting to ``<base>/mcp/ui/api/...`` 404s when the
        discovered URL is the MCP endpoint.
        """
        base = self.base_url.rstrip("/")
        return base[: -len("/mcp")] if base.endswith("/mcp") else base


def resolve_base_url(env: Mapping[str, str] | None = None, creds: Mapping[str, str] | None = None) -> str:
    """Server discovery: env override -> credentials file -> local default."""
    environ = os.environ if env is None else env
    file_values = creds or {}
    return (
        environ.get("FORESIGHT_MCP_URL")
        or environ.get("FORESIGHT_URL")
        or file_values.get("FORESIGHT_MCP_URL")
        or file_values.get("FORESIGHT_URL")
        or DEFAULT_BASE_URL
    )


def resolve_credentials(
    env: Mapping[str, str] | None = None,
    creds_path: str | None = None,
) -> Credentials:
    """Resolve credentials with precedence: environment > credentials.env."""
    environ = os.environ if env is None else env
    path = creds_path or default_credentials_path(environ)
    file_values = load_env_file(path)

    def pick(*names: str) -> str:
        for name in names:
            value = environ.get(name) or file_values.get(name)
            if value:
                return value
        return ""

    return Credentials(
        base_url=resolve_base_url(environ, file_values),
        api_key=pick("FORESIGHT_API_KEY"),
        identity=pick("FORESIGHT_IDENTITY"),
        user_id=pick("FORESIGHT_USER_ID"),
        extra=file_values,
    )


def auth_headers(credentials: Credentials | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Standard Foresight request headers: JSON + Bearer + X-User-Id lane."""
    creds = credentials or resolve_credentials()
    headers = {"Content-Type": "application/json"}
    if creds.api_key:
        headers["Authorization"] = f"Bearer {creds.api_key}"
    lane = creds.lane or creds.user_id
    if lane:
        headers["X-User-Id"] = lane
    if extra:
        headers.update(extra)
    return headers


# -- Transport ----------------------------------------------------------------


def _is_loopback_host(hostname: str) -> bool:
    """True for ``localhost`` and loopback IP literals; false for anything else."""
    host = hostname.strip("[]").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_secure_transport(parts: urllib.parse.SplitResult, headers: dict[str, str] | None) -> None:
    """Refuse to send an ``Authorization`` header in cleartext off loopback.

    A configured ``http://`` base URL would otherwise put the API key on the
    wire for any network observer; loopback development stays supported.
    """
    if parts.scheme == "https" or not headers:
        return
    if not any(name.lower() == "authorization" for name in headers):
        return
    hostname = parts.hostname or ""
    if _is_loopback_host(hostname):
        return
    raise ForesightError(
        f"refusing to send credentials over cleartext HTTP to {hostname or parts.netloc}; "
        "use https:// or a loopback address"
    )


def post_json(
    url: str,
    payload: Any,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, bytes]:
    """POST a JSON body; return ``(status, raw_body)``.

    Raises ``ForesightHTTPError`` on status >= 400, and ``ForesightError`` on
    transport failures, invalid URLs, and cleartext credential transport.
    Callers that must never break the host agent should wrap in
    ``contextlib.suppress(Exception)``.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ForesightError(f"invalid URL {url}: {exc}") from exc
    _require_secure_transport(parts, headers)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    connection_type = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    try:
        connection = connection_type(parts.hostname or "", port, timeout=timeout)
    except Exception as exc:
        raise ForesightError(f"cannot connect to {parts.hostname or url}: {exc}") from exc
    try:
        body = json.dumps(payload).encode("utf-8")
        connection.request(
            "POST",
            target,
            body=body,
            headers=headers or {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        status, reason = response.status, response.reason
        raw = response.read()
    except ForesightError:
        raise
    except Exception as exc:
        raise ForesightError(f"request to {url} failed: {exc}") from exc
    finally:
        connection.close()
    if status >= http.HTTPStatus.BAD_REQUEST:
        raise ForesightHTTPError(status, reason or "")
    return status, raw


def parse_json(raw: str | bytes) -> Any:
    """Parse JSON, returning ``None`` on any failure (hook-friendly)."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except Exception:
        return None


def read_hook_input(stdin_text: str | None = None) -> dict[str, Any]:
    """Read a hook payload from stdin (or ``stdin_text``); ``{}`` on failure."""
    text_in = stdin_text or ""
    if stdin_text is None:
        with suppress(Exception):
            text_in = sys.stdin.read()
    if not text_in.strip():
        return {}
    parsed = parse_json(text_in)
    return parsed if isinstance(parsed, dict) else {}


# -- MCP -----------------------------------------------------------------------


def _is_rpc_response(frame: dict[str, Any]) -> bool:
    """True for a JSON-RPC response frame (``result`` or ``error`` present)."""
    return "result" in frame or "error" in frame


def _is_rpc_notification(frame: dict[str, Any]) -> bool:
    """True for a JSON-RPC notification frame (``method`` and no ``id``)."""
    return "method" in frame and "id" not in frame


def parse_sse_result(body: str) -> dict[str, Any] | None:
    """Extract the JSON-RPC response from a plain or SSE-framed body.

    MCP allows notifications (``notifications/message``,
    ``notifications/progress``) to arrive before the response on the same
    stream, so a notification frame is skipped instead of being mistaken for
    the result. A plain JSON body is returned as-is; a stream with no response
    frame falls back to its first non-notification frame so plain-data servers
    keep working.
    """
    if not body:
        return None
    parsed = parse_json(body)
    if isinstance(parsed, dict):
        return parsed
    fallback: dict[str, Any] | None = None
    for line in body.split("\n"):
        if line.startswith("data: "):
            candidate = parse_json(line[6:])
            if not isinstance(candidate, dict):
                continue
            if _is_rpc_response(candidate):
                return candidate
            if fallback is None and not _is_rpc_notification(candidate):
                fallback = candidate
    return fallback


def mcp_result_text(result: Any) -> str | None:
    """Flatten an MCP ``tools/call`` result to text (content[].text joined)."""
    if result is None:
        return None
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        texts.append(text)
            return "\n".join(texts)
        return json.dumps(result)
    return json.dumps(result)


# -- Client --------------------------------------------------------------------


@dataclass
class ForesightClient:
    """Fail-safe REST + MCP client for agent plugins.

    ``post`` raises ``ForesightError``/``ForesightHTTPError``; every other
    helper swallows transport/HTTP failures and returns ``None`` so a hook can
    never break the host agent session. Use ``from_env`` for the standard
    env -> credentials.env resolution.
    """

    credentials: Credentials = field(default_factory=resolve_credentials)
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, creds_path: str | None = None) -> ForesightClient:
        return cls(credentials=resolve_credentials(env, creds_path))

    @property
    def base_url(self) -> str:
        return self.credentials.base_url.rstrip("/")

    def post(self, path: str, payload: Any, timeout: float | None = None) -> tuple[int, Any]:
        """POST ``path`` (e.g. ``/ui/api/inject``); return ``(status, parsed_json)``."""
        status, raw = post_json(
            f"{self.credentials.rest_url}{path}",
            payload,
            headers=auth_headers(self.credentials),
            timeout=timeout or self.timeout,
        )
        return status, parse_json(raw)

    def inject_context(self, text: str, timeout: float | None = None) -> str | None:
        """Single-roundtrip context injection; returns the formatted block.

        Falls back to the MCP ``inject_context`` tool when the REST endpoint is
        unavailable, so plugins keep recall on a server that only exposes MCP.
        """
        try:
            _, data = self.post("/ui/api/inject", {"text": text}, timeout=timeout)
        except ForesightError:
            lane = self.credentials.lane or self.credentials.user_id or "default"
            return self.call_tool(
                "inject_context",
                {"conversation_text": text, "user_id": lane},
                timeout=timeout,
            )
        if isinstance(data, dict):
            formatted = data.get("formatted")
            return formatted.strip() if isinstance(formatted, str) and formatted.strip() else None
        return None

    def capture_text(self, text: str, session_id: str, messages: list[Any] | None = None) -> None:
        """In-flight capture via ``/ui/api/capture``."""
        payload: dict[str, Any] = {"text": text, "session_id": session_id}
        if messages:
            payload["messages"] = messages
        with suppress(ForesightError):
            self.post("/ui/api/capture", payload, timeout=5.0)

    def capture_transcript(self, session_id: str, messages: list[Any]) -> None:
        """Distillation transcript via ``/session/transcript``."""
        with suppress(ForesightError):
            self.post("/session/transcript", {"session_id": session_id, "messages": messages}, timeout=12.0)

    def post_telemetry(self, payload: dict[str, Any], timeout: float = 2.0) -> None:
        """Fire-and-forget telemetry via ``/ui/api/telemetry/gating``."""
        with suppress(ForesightError):
            self.post("/ui/api/telemetry/gating", payload, timeout=timeout)

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float | None = None) -> str | None:
        """MCP ``tools/call`` against ``<base>/mcp``; returns joined text or None."""
        meta: dict[str, Any] = {
            "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        # AuthMiddleware.on_call_tool reads the key from the message metadata and
        # never from the Bearer header, so an unauthenticated call is rejected.
        if self.credentials.api_key:
            meta["api_key"] = self.credentials.api_key
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                # Every MCP tool takes one pydantic model named ``params``.
                "arguments": {"params": arguments},
                "_meta": meta,
            },
        }
        try:
            status, raw = post_json(
                self.credentials.mcp_url,
                payload,
                headers=auth_headers(
                    self.credentials,
                    {
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                        "Mcp-Method": "tools/call",
                        "Mcp-Name": name,
                    },
                ),
                timeout=timeout or self.timeout,
            )
        except ForesightError:
            return None
        if status != http.HTTPStatus.OK:
            return None
        response = parse_sse_result(raw.decode("utf-8", errors="replace"))
        if not response or response.get("error"):
            return None
        result = response.get("result")
        if isinstance(result, dict) and result.get("isError"):
            return None
        return mcp_result_text(result)
