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
``ForesightError``/``ForesightHTTPError`` on transport, redirect, or HTTP
failure and never return partial data. The high-level ``ForesightClient``
helpers never raise: ``inject_context`` and ``call_tool`` return ``None`` when
no text comes back, and ``capture_text``, ``capture_transcript`` and
``post_telemetry`` return ``True`` when the server accepted the write or
``False`` when it did not. A down server can therefore never break the host
agent session, and the return value — not an exception handler — is how a
caller learns whether a call landed.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import sys
import urllib.parse
from collections.abc import Iterator, Mapping
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


def _posix_modes_are_enforced() -> bool:
    """Whether ``st_mode`` carries access bits.

    Windows synthesizes them from the read-only flag, so every file there reads
    as ``0o666`` whatever its ACL; the ACL is the whole gate and this check must
    not reject the file.
    """
    return os.name != "nt"


def load_env_file(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE env file. Missing, unreadable or shared files yield ``{}``.

    The file holds an API key, so a mode other users can read means the key is
    already exposed; such a file is ignored instead of trusted.
    """
    values: dict[str, str] = {}
    with suppress(Exception):
        if _posix_modes_are_enforced() and os.stat(path).st_mode & 0o077:
            return values
    with suppress(Exception), open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _split_base_url(url: str) -> urllib.parse.SplitResult:
    """Split a Foresight URL, rejecting anything that is not http(s).

    ``http.client`` picks its connection class from the scheme and falls back to
    a cleartext ``HTTPConnection`` for everything it does not recognise, so an
    ``ftp://`` URL would silently put the request (and the API key) on an
    unencrypted HTTP connection. Callers rebuild the URL from these parts
    instead of concatenating strings, because appending a path to a URL that
    carries a query lands inside the query (``?route=blue/mcp``).
    """
    try:
        parts = urllib.parse.urlsplit(url.strip())
        _ = parts.port  # a malformed port raises here, not at connection time
    except ValueError as exc:
        raise ForesightError(f"invalid URL {url}: {exc}") from exc
    if parts.scheme not in ("http", "https"):
        raise ForesightError(f"invalid URL {url}: expected an http:// or https:// scheme")
    if not parts.hostname:
        raise ForesightError(f"invalid URL {url}: no host")
    return parts


def _with_path(parts: urllib.parse.SplitResult, path: str) -> str:
    """Rebuild ``parts`` with ``path``, keeping scheme, host, port and query."""
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def rest_base(base_url: str) -> str:
    """REST root: ``base_url`` with a terminal ``/mcp`` removed."""
    parts = _split_base_url(base_url)
    path = parts.path.rstrip("/")
    if path.endswith("/mcp"):
        path = path[: -len("/mcp")]
    return _with_path(parts, path)


def rest_path(base_url: str, path: str) -> str:
    """URL for the REST ``path`` under ``base_url``, keeping the base query."""
    parts = _split_base_url(rest_base(base_url))
    base = parts.path.rstrip("/")
    return _with_path(parts, f"{base}/{path.lstrip('/')}")


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
        """MCP endpoint: the base URL with ``/mcp`` appended to its path."""
        parts = _split_base_url(self.base_url)
        path = parts.path.rstrip("/")
        return _with_path(parts, path if path.endswith("/mcp") else f"{path}/mcp")

    @property
    def rest_url(self) -> str:
        """REST base: ``base_url`` without a terminal ``/mcp``.

        The REST endpoints (``/ui/api/inject``, ``/ui/api/capture``,
        ``/session/transcript``, ``/ui/api/telemetry/gating``) are registered at
        the app root, so posting to ``<base>/mcp/ui/api/...`` 404s when the
        discovered URL is the MCP endpoint.
        """
        return rest_base(self.base_url)


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
    lane = creds.user_id or creds.lane
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

    Raises ``ForesightHTTPError`` on status >= 400, and ``ForesightError`` on a
    redirect status, transport failure, invalid or non-HTTP URL, or cleartext
    credential transport. Callers that must never break the host agent should
    wrap in ``contextlib.suppress(Exception)`` or use a client helper.
    """
    parts = _split_base_url(url)
    port = parts.port
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
    if http.HTTPStatus.MULTIPLE_CHOICES <= status < http.HTTPStatus.BAD_REQUEST:
        # http.client never follows a redirect, so a 3xx means the endpoint
        # moved. Reporting it as success would hand the caller an empty body
        # from the redirect target it never fetched.
        raise ForesightError(f"refusing redirect {status} from {url}: the endpoint moved")
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


def _iter_sse_events(body: str) -> Iterator[str]:
    """Yield each SSE event's data payload, joined across that event's lines.

    SSE ends an event on a blank line and allows one event's payload to span
    several ``data:`` lines. A parser that decodes each line on its own loses a
    payload split that way, and one that requires the space in ``data: `` misses
    the equally legal ``data:{"result": 1}``. A single leading space is part of
    the framing, not the payload; ``:`` comment lines carry no data. Only a real
    newline ends a line: ``splitlines`` also splits on U+2028, which is legal
    inside a JSON string and would cut a payload in half.
    """
    data: list[str] = []
    for raw_line in body.split("\n"):
        line = raw_line.rstrip("\r")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            value = line[len("data:") :]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        yield "\n".join(data)


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
    for event in _iter_sse_events(body):
        candidate = parse_json(event)
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
            rest_path(self.credentials.base_url, path),
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
            arguments: dict[str, Any] = {"conversation_text": text}
            lane = self.credentials.user_id or self.credentials.lane
            if lane:
                # The server resolves the lane from this argument first and from
                # the API key second, so an invented placeholder would read a
                # lane the key cannot see. Omitting it lets the key's own lane
                # apply.
                arguments["user_id"] = lane
            return self.call_tool("inject_context", arguments, timeout=timeout)
        if isinstance(data, dict):
            formatted = data.get("formatted")
            return formatted.strip() if isinstance(formatted, str) and formatted.strip() else None
        return None

    def _post_ok(self, path: str, payload: Any, timeout: float) -> bool:
        """POST and report delivery; ``False`` on any failure, never raises."""
        try:
            status, _ = self.post(path, payload, timeout=timeout)
        except ForesightError:
            return False
        return http.HTTPStatus.OK <= status < http.HTTPStatus.MULTIPLE_CHOICES

    def capture_text(self, text: str, session_id: str, messages: list[Any] | None = None) -> bool:
        """In-flight capture via ``/ui/api/capture``; ``True`` when delivered."""
        payload: dict[str, Any] = {"text": text, "session_id": session_id}
        if messages:
            payload["messages"] = messages
        return self._post_ok("/ui/api/capture", payload, 5.0)

    def capture_transcript(self, session_id: str, messages: list[Any]) -> bool:
        """Distillation transcript via ``/session/transcript``; ``True`` when delivered."""
        return self._post_ok("/session/transcript", {"session_id": session_id, "messages": messages}, 12.0)

    def post_telemetry(self, payload: dict[str, Any], timeout: float = 2.0) -> bool:
        """Telemetry via ``/ui/api/telemetry/gating``; ``True`` when delivered."""
        return self._post_ok("/ui/api/telemetry/gating", payload, timeout)

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
