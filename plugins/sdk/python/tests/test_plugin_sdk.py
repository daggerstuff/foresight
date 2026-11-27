"""Tests for the shared plugin SDK (FOR-69) and its Claude hook consumer."""

from __future__ import annotations

import http
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SDK_DIR = REPO_ROOT / "plugins" / "sdk" / "python"
HOOK_PATH = REPO_ROOT / "plugins" / "claude" / "hooks" / "foresight_hook.py"


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


_spec = importlib.util.spec_from_file_location("foresight_plugin_sdk", SDK_DIR / "foresight_plugin_sdk.py")
assert _spec is not None
assert _spec.loader is not None
sdk = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sdk
_spec.loader.exec_module(sdk)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / "credentials.env"
    path.write_text(
        "# comment\nFORESIGHT_API_KEY=file-key\nFORESIGHT_IDENTITY=carol@acme\n"
        'FORESIGHT_URL=http://file-host:9999\nQUOTED=" spaced "\n'
    )
    path.chmod(0o600)
    return path


class TestCredentials:
    def test_load_env_file_parses_and_strips(self, env_file: Path):
        values = sdk.load_env_file(str(env_file))
        assert values["FORESIGHT_API_KEY"] == "file-key"
        assert values["QUOTED"] == " spaced "
        assert "# comment" not in values

    def test_load_env_file_missing_returns_empty(self, tmp_path: Path):
        assert sdk.load_env_file(str(tmp_path / "nope.env")) == {}

    def test_default_credentials_path_xdg(self, tmp_path: Path):
        env = {"XDG_CONFIG_HOME": str(tmp_path)}
        assert sdk.default_credentials_path(env) == str(tmp_path / "foresight" / "credentials.env")

    def test_resolve_credentials_env_beats_file(self, env_file: Path):
        env = {"FORESIGHT_API_KEY": "env-key", "FORESIGHT_MCP_URL": "http://env:1"}
        creds = sdk.resolve_credentials(env, str(env_file))
        assert creds.api_key == "env-key"
        assert creds.base_url == "http://env:1"
        # identity still comes from the file
        assert creds.identity == "carol@acme"

    def test_resolve_credentials_file_fallback(self, env_file: Path):
        creds = sdk.resolve_credentials({}, str(env_file))
        assert creds.api_key == "file-key"
        assert creds.base_url == "http://file-host:9999"

    def test_lane_derives_from_identity(self, env_file: Path):
        creds = sdk.resolve_credentials({}, str(env_file))
        assert creds.lane == "carol"

    def test_mcp_url_appends_to_the_path_and_keeps_the_query(self):
        creds = sdk.Credentials(base_url="https://foresight.example.com/prefix?route=blue")
        assert creds.mcp_url == "https://foresight.example.com/prefix/mcp?route=blue"
        assert creds.rest_url == "https://foresight.example.com/prefix?route=blue"

    def test_mcp_url_does_not_double_a_terminal_mcp(self):
        assert sdk.Credentials(base_url="https://h/mcp").mcp_url == "https://h/mcp"
        assert sdk.Credentials(base_url="https://h/mcp/").mcp_url == "https://h/mcp"

    def test_rest_url_strips_only_a_terminal_mcp(self):
        assert sdk.Credentials(base_url="https://h/mcp").rest_url == "https://h"
        assert sdk.Credentials(base_url="https://h/mcpx").rest_url == "https://h/mcpx"

    def test_url_properties_reject_a_non_http_scheme(self):
        # http.client falls back to a cleartext HTTPConnection for a scheme it
        # does not recognise, which would put the API key on the wire.
        with pytest.raises(sdk.ForesightError, match="http://"):
            _ = sdk.Credentials(base_url="ftp://foresight.example.com/mcp").mcp_url

    def test_url_properties_reject_a_missing_host(self):
        with pytest.raises(sdk.ForesightError, match="no host"):
            _ = sdk.Credentials(base_url="http://").rest_url

    def test_auth_headers_include_bearer_and_lane(self, env_file: Path):
        creds = sdk.resolve_credentials({}, str(env_file))
        headers = sdk.auth_headers(creds)
        assert headers["Authorization"] == "Bearer file-key"
        assert headers["X-User-Id"] == "carol"
        assert headers["Content-Type"] == "application/json"

    def test_auth_headers_empty_without_credentials(self):
        headers = sdk.auth_headers(sdk.Credentials())
        assert "Authorization" not in headers
        assert "X-User-Id" not in headers


class _StubHandler(BaseHTTPRequestHandler):
    responses: ClassVar[dict[str, tuple[int, dict]]] = {}
    requests: ClassVar[list[tuple[str, dict]]] = []
    headers: ClassVar[list[dict[str, str]]] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append((self.path, body))
        type(self).headers.append(dict(self.headers))
        status, payload = type(self).responses.get(self.path, (404, {"error": "nf"}))
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_server():
    handler = type("Handler", (_StubHandler,), {"responses": {}, "requests": [], "headers": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", handler
    server.shutdown()
    thread.join(timeout=5)


class TestTransport:
    def test_post_json_success(self, stub_server):
        base, handler = stub_server
        handler.responses["/echo"] = (http.HTTPStatus.OK, {"ok": True})
        status, raw = sdk.post_json(f"{base}/echo", {"a": 1})
        assert status == http.HTTPStatus.OK
        assert json.loads(raw) == {"ok": True}
        assert handler.requests == [("/echo", {"a": 1})]

    def test_post_json_http_error_raises(self, stub_server):
        base, handler = stub_server
        handler.responses["/bad"] = (http.HTTPStatus.INTERNAL_SERVER_ERROR, {"err": "boom"})
        with pytest.raises(sdk.ForesightHTTPError) as excinfo:
            sdk.post_json(f"{base}/bad", {})
        assert excinfo.value.status == http.HTTPStatus.INTERNAL_SERVER_ERROR

    def test_post_json_refuses_redirect(self, stub_server):
        base, handler = stub_server
        handler.responses["/moved"] = (http.HTTPStatus.FOUND, {"ok": True})
        with pytest.raises(sdk.ForesightError, match="redirect"):
            sdk.post_json(f"{base}/moved", {})
        # The body was posted once; the redirect target was never followed.
        assert handler.requests == [("/moved", {})]

    def test_post_json_unreachable_raises_foresight_error(self):
        url = f"http://127.0.0.1:{_closed_port()}/unreachable"
        with pytest.raises(sdk.ForesightError):
            sdk.post_json(url, {}, timeout=1.0)

    def test_post_json_invalid_url_raises_foresight_error(self):
        with pytest.raises(sdk.ForesightError):
            sdk.post_json("http://127.0.0.1:99999/x", {})

    def test_post_json_refuses_cleartext_credentials_off_loopback(self, monkeypatch):
        def no_connect(*_args, **_kwargs):
            raise AssertionError("a refused cleartext request must not open a connection")

        monkeypatch.setattr(sdk.http.client, "HTTPConnection", no_connect)
        monkeypatch.setattr(sdk.http.client, "HTTPSConnection", no_connect)
        with pytest.raises(sdk.ForesightError) as excinfo:
            sdk.post_json(
                "http://foresight.example.com/mcp",
                {"a": 1},
                headers={"Authorization": "Bearer k"},
            )
        assert "cleartext" in str(excinfo.value)

    def test_post_json_refuses_cleartext_credentials_case_insensitively(self):
        with pytest.raises(sdk.ForesightError) as excinfo:
            sdk.post_json(
                "http://foresight.example.com/mcp",
                {"a": 1},
                headers={"authorization": "Bearer k"},
            )
        assert "cleartext" in str(excinfo.value)

    def test_post_json_allows_cleartext_credentials_on_loopback(self, stub_server):
        base, handler = stub_server
        handler.responses["/auth"] = (http.HTTPStatus.OK, {"ok": True})
        status, raw = sdk.post_json(f"{base}/auth", {}, headers={"Authorization": "Bearer k"})
        assert status == http.HTTPStatus.OK
        assert json.loads(raw) == {"ok": True}

    def test_post_json_allows_cleartext_credentials_to_ipv6_loopback(self, monkeypatch):
        opened: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def record(*args, **kwargs):
            opened.append((args, kwargs))
            raise ConnectionRefusedError("test refuses to open a socket")

        monkeypatch.setattr(sdk.http.client, "HTTPConnection", record)
        with pytest.raises(sdk.ForesightError) as excinfo:
            sdk.post_json(
                "http://[::1]:1/mcp",
                {},
                headers={"Authorization": "Bearer k"},
                timeout=1.0,
            )
        assert opened, "an allowed loopback request must reach the transport"
        assert "cleartext" not in str(excinfo.value)

    def test_post_json_unauthenticated_cleartext_off_loopback_is_allowed(self, monkeypatch):
        attempted: list[tuple] = []

        def record(*args, **kwargs):
            attempted.append((args, kwargs))
            raise sdk.ForesightError("stub transport")

        monkeypatch.setattr(sdk.http.client, "HTTPConnection", record)
        with pytest.raises(sdk.ForesightError) as excinfo:
            sdk.post_json("http://foresight.example.com/mcp", {"a": 1})
        assert "cleartext" not in str(excinfo.value)
        # An unauthenticated request must reach the transport, not be refused.
        assert attempted
        assert attempted[0][0][0] == "foresight.example.com"

    def test_parse_json(self):
        assert sdk.parse_json(b'{"a": 1}') == {"a": 1}
        assert sdk.parse_json("not json") is None
        assert sdk.parse_json(b"\xff\xfe") is None


class _FakeStdin:
    def __init__(self, text: str):
        self._text = text

    def read(self):
        return self._text


class TestHookInputAndMcp:
    def test_read_hook_input_from_string(self):
        assert sdk.read_hook_input('{"prompt": "hi"}') == {"prompt": "hi"}
        assert sdk.read_hook_input("not json") == {}
        assert sdk.read_hook_input("") == {}

    def test_read_hook_input_none_reads_stdin(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _FakeStdin('{"x": 1}'))
        assert sdk.read_hook_input(None) == {"x": 1}

    def test_parse_sse_result_plain_and_framed(self):
        assert sdk.parse_sse_result('{"result": 1}') == {"result": 1}
        assert sdk.parse_sse_result('data: {"result": 2}\n\n') == {"result": 2}
        assert sdk.parse_sse_result("garbage") is None

    def test_parse_sse_result_skips_notification_frames(self):
        body = (
            'data: {"jsonrpc": "2.0", "method": "notifications/message", '
            '"params": {"level": "info"}}\n\n'
            'data: {"jsonrpc": "2.0", "id": 4, "result": {"content": []}}\n\n'
        )
        assert sdk.parse_sse_result(body) == {
            "jsonrpc": "2.0",
            "id": 4,
            "result": {"content": []},
        }

    def test_parse_sse_result_falls_back_to_first_plain_frame(self):
        body = 'data: {"jsonrpc": "2.0", "id": 1, "content": []}\n\n'
        assert sdk.parse_sse_result(body) == {"jsonrpc": "2.0", "id": 1, "content": []}

    def test_parse_sse_result_skips_comments_and_joins_multiline_data(self):
        body = ': keep-alive\n\nevent: message\ndata: {"jsonrpc": "2.0",\ndata:  "result": {"content": []}}\n\n'
        assert sdk.parse_sse_result(body) == {
            "jsonrpc": "2.0",
            "result": {"content": []},
        }

    def test_parse_sse_result_accepts_data_without_a_space(self):
        assert sdk.parse_sse_result('data:{"result": 7}\n\n') == {"result": 7}

    def test_parse_sse_result_ignores_an_unparsable_event(self):
        body = 'data: {not json}\n\ndata: {"result": 8}\n\n'
        assert sdk.parse_sse_result(body) == {"result": 8}

    def test_mcp_result_text(self):
        result = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        assert sdk.mcp_result_text(result) == "a\nb"
        assert sdk.mcp_result_text("raw") == "raw"
        assert sdk.mcp_result_text(None) is None
        assert sdk.mcp_result_text({"content": []}) == ""


class TestForesightClient:
    def test_inject_context_returns_formatted(self, stub_server):
        base, handler = stub_server
        handler.responses["/ui/api/inject"] = (http.HTTPStatus.OK, {"formatted": "mem block"})
        client = sdk.ForesightClient(
            credentials=sdk.Credentials(base_url=base, api_key="k", identity="d@e"),
        )
        assert client.inject_context("hello") == "mem block"
        path, body = handler.requests[-1]
        assert path == "/ui/api/inject"
        assert body == {"text": "hello"}

    def test_inject_context_falls_back_to_mcp_without_inventing_a_lane(self, stub_server):
        base, handler = stub_server
        handler.responses["/ui/api/inject"] = (http.HTTPStatus.INTERNAL_SERVER_ERROR, {"err": "no rest"})
        handler.responses["/mcp"] = (
            http.HTTPStatus.OK,
            {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "mcp block"}]}},
        )
        client = sdk.ForesightClient(credentials=sdk.Credentials(base_url=base))
        assert client.inject_context("hello") == "mcp block"
        path, body = handler.requests[-1]
        assert path == "/mcp"
        arguments = body["params"]["arguments"]["params"]
        assert arguments["conversation_text"] == "hello"
        assert "user_id" not in arguments

    def test_inject_context_fallback_sends_the_configured_lane(self, stub_server):
        base, handler = stub_server
        handler.responses["/ui/api/inject"] = (http.HTTPStatus.INTERNAL_SERVER_ERROR, {"err": "no rest"})
        handler.responses["/mcp"] = (
            http.HTTPStatus.OK,
            {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "mcp block"}]}},
        )
        client = sdk.ForesightClient(
            credentials=sdk.Credentials(base_url=base, identity="dana@acme"),
        )
        assert client.inject_context("hello") == "mcp block"
        arguments = handler.requests[-1][1]["params"]["arguments"]["params"]
        assert arguments["user_id"] == "dana"

    def test_capture_text_and_transcript(self, stub_server):
        base, handler = stub_server
        handler.responses["/ui/api/capture"] = (http.HTTPStatus.OK, {})
        handler.responses["/session/transcript"] = (http.HTTPStatus.OK, {})
        client = sdk.ForesightClient(credentials=sdk.Credentials(base_url=base))
        assert client.capture_text("note", "sess1") is True
        assert client.capture_transcript("sess1", [{"role": "user", "content": "hi"}]) is True
        paths = [p for p, _ in handler.requests]
        assert paths == ["/ui/api/capture", "/session/transcript"]
        assert handler.requests[0][1] == {"text": "note", "session_id": "sess1"}
        assert handler.requests[1][1] == {
            "session_id": "sess1",
            "messages": [{"role": "user", "content": "hi"}],
        }

    def test_capture_reports_a_rejected_post_as_false(self, stub_server):
        base, handler = stub_server
        handler.responses["/ui/api/capture"] = (http.HTTPStatus.INTERNAL_SERVER_ERROR, {"err": "boom"})
        client = sdk.ForesightClient(credentials=sdk.Credentials(base_url=base))
        assert client.capture_text("note", "sess1") is False

    def test_call_tool_parses_sse(self):
        class SSEHandler(_StubHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                raw = b'data: {"jsonrpc":"2.0","result":{"content":[{"type":"text","text":"tool says hi"}]}}\n\n'
                self.send_response(http.HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), SSEHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = sdk.ForesightClient(
                credentials=sdk.Credentials(base_url=f"http://127.0.0.1:{server.server_port}"),
            )
            assert client.call_tool("search_memories", {"query": "x"}) == "tool says hi"
        finally:
            server.shutdown()
            thread.join(timeout=5)

    def test_call_tool_plain_json_response(self, stub_server):
        base, handler = stub_server
        handler.responses["/mcp"] = (
            http.HTTPStatus.OK,
            {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "plain hi"}]}},
        )
        client = sdk.ForesightClient(credentials=sdk.Credentials(base_url=base, api_key="k"))
        assert client.call_tool("search_memories", {"query": "x"}) == "plain hi"
        path, body = handler.requests[-1]
        assert path == "/mcp"
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "search_memories"
        assert body["params"]["arguments"] == {"params": {"query": "x"}}
        assert body["params"]["_meta"]["api_key"] == "k"

    def test_call_tool_returns_none_on_transport_failure(self):
        client = sdk.ForesightClient(
            credentials=sdk.Credentials(base_url=f"http://127.0.0.1:{_closed_port()}"),
            timeout=1.0,
        )
        assert client.call_tool("search_memories", {"query": "x"}) is None

    def test_call_tool_returns_none_on_tool_error(self, stub_server):
        base, handler = stub_server
        handler.responses["/mcp"] = (
            http.HTTPStatus.OK,
            {
                "jsonrpc": "2.0",
                "result": {"isError": True, "content": [{"type": "text", "text": "boom"}]},
            },
        )
        client = sdk.ForesightClient(credentials=sdk.Credentials(base_url=base))
        assert client.call_tool("search_memories", {"query": "x"}) is None


class TestClaudeHookIntegration:
    """The refactored hook must keep working end-to-end via the SDK."""

    # Tests that need a relocated copy of the hook overwrite this per instance.
    hook_path: Path = HOOK_PATH

    def _copy_hook(self, tmp_path: Path) -> None:
        """Copy the hook somewhere its repo sibling SDK path does not resolve."""
        hook = tmp_path / "hooks" / "foresight_hook.py"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_bytes(HOOK_PATH.read_bytes())
        self.hook_path = hook

    @staticmethod
    def _write_sdk_shim(hook: Path, marker: Path) -> None:
        """Point the hook at the real SDK, recording that the import happened."""
        real_sdk = SDK_DIR / "foresight_plugin_sdk.py"
        lines = [
            "import importlib.util as _iu",
            "import pathlib as _pl",
            "import sys as _sys",
            f"_marker = _pl.Path({str(marker)!r})",
            f"_spec = _iu.spec_from_file_location('_real_foresight_plugin_sdk', {str(real_sdk)!r})",
            "_real = _iu.module_from_spec(_spec)",
            "_sys.modules[_spec.name] = _real",
            "_spec.loader.exec_module(_real)",
            "_real_post_json = _real.post_json",
            "def _shimmed_post_json(*args, **kwargs):",
            "    _marker.write_text('posted', encoding='utf-8')",
            "    return _real_post_json(*args, **kwargs)",
            "_real.post_json = _shimmed_post_json",
            "globals().update(",
            "    {name: getattr(_real, name) for name in dir(_real) if not name.startswith('__')}",
            ")",
        ]
        (hook.parent / "foresight_plugin_sdk.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _run_hook(
        self,
        action: str,
        payload: dict,
        base_url: str,
        tmp_path: Path,
        extra_env: dict[str, str] | None = None,
    ):
        env = dict(os.environ)
        env.update(
            {
                "FORESIGHT_MCP_URL": base_url,
                "XDG_CACHE_HOME": str(tmp_path / "cache"),
                "FORESIGHT_HOOK_MODE": "cache",
            }
        )
        env.pop("FORESIGHT_API_KEY", None)
        env.pop("FORESIGHT_IDENTITY", None)
        env.pop("FORESIGHT_USER_ID", None)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(self.hook_path), action],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )

    def test_prompt_injects_context_block_through_the_sdk(self, stub_server, tmp_path: Path):
        base, handler = stub_server
        handler.responses["/ui/api/inject"] = (
            http.HTTPStatus.OK,
            {"formatted": "recalled memory", "top_score": 0.5},
        )
        handler.responses["/ui/api/telemetry/gating"] = (http.HTTPStatus.OK, {})
        self._copy_hook(tmp_path)
        marker = tmp_path / "sdk-imported"
        self._write_sdk_shim(self.hook_path, marker)
        result = self._run_hook(
            "prompt",
            {"prompt": "continue the migration", "session_id": "s1"},
            base,
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        # The shim writes the marker from the SDK's own post_json, so it proves
        # this request went through the SDK rather than merely importing it.
        assert marker.exists()
        assert marker.read_text(encoding="utf-8") == "posted"
        assert "FORESIGHT CONTINUITY CONTEXT" in result.stdout
        assert "recalled memory" in result.stdout
        inject_calls = [b for p, b in handler.requests if p == "/ui/api/inject"]
        assert inject_calls
        assert inject_calls[0]["text"] == "continue the migration"

    def test_prompt_still_injects_without_the_sdk(self, stub_server, tmp_path: Path):
        """A missing SDK must not break the host session: the inline path stays."""
        base, handler = stub_server
        handler.responses["/ui/api/inject"] = (
            http.HTTPStatus.OK,
            {"formatted": "inline memory", "top_score": 0.5},
        )
        handler.responses["/ui/api/telemetry/gating"] = (http.HTTPStatus.OK, {})
        self._copy_hook(tmp_path)
        result = self._run_hook(
            "prompt",
            {"prompt": "continue the migration", "session_id": "s1"},
            base,
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert "inline memory" in result.stdout
        inject_calls = [b for p, b in handler.requests if p == "/ui/api/inject"]
        assert inject_calls
        assert inject_calls[0]["text"] == "continue the migration"

    def test_hook_survives_server_down(self, tmp_path: Path):
        result = self._run_hook(
            "prompt",
            {"prompt": "hello there", "session_id": "s2"},
            f"http://127.0.0.1:{_closed_port()}",
            tmp_path,
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("base_suffix", ["/?route=blue", "/mcp?route=blue", "/mcp/?route=blue"])
    def test_prompt_reaches_the_endpoint_when_the_base_url_has_a_query(
        self, stub_server, tmp_path: Path, base_suffix: str
    ):
        base, handler = stub_server
        handler.responses["/ui/api/inject?route=blue"] = (
            http.HTTPStatus.OK,
            {"formatted": "routed memory"},
        )
        handler.responses["/ui/api/telemetry/gating?route=blue"] = (http.HTTPStatus.OK, {})
        self._copy_hook(tmp_path)
        result = self._run_hook(
            "prompt",
            {"prompt": "route me", "session_id": "s5"},
            f"{base}{base_suffix}",
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert "routed memory" in result.stdout

    def test_the_sdk_path_sends_the_user_id_lane(self, stub_server, tmp_path: Path):
        base, handler = stub_server
        handler.responses["/ui/api/inject"] = (http.HTTPStatus.OK, {"formatted": "ok"})
        handler.responses["/ui/api/telemetry/gating"] = (http.HTTPStatus.OK, {})
        self._copy_hook(tmp_path)
        self._write_sdk_shim(self.hook_path, tmp_path / "sdk-lane")
        result = self._run_hook(
            "prompt",
            {"prompt": "lane check", "session_id": "s6"},
            base,
            tmp_path,
            extra_env={"FORESIGHT_IDENTITY": "alice@acme", "FORESIGHT_USER_ID": "bob"},
        )
        assert result.returncode == 0, result.stderr
        lanes = {h.get("X-User-Id") for h in handler.headers if h.get("X-User-Id")}
        assert lanes == {"bob"}

    def test_the_inline_path_sends_the_user_id_lane(self, stub_server, tmp_path: Path):
        base, handler = stub_server
        handler.responses["/ui/api/inject"] = (http.HTTPStatus.OK, {"formatted": "ok"})
        handler.responses["/ui/api/telemetry/gating"] = (http.HTTPStatus.OK, {})
        self._copy_hook(tmp_path)
        result = self._run_hook(
            "prompt",
            {"prompt": "lane check", "session_id": "s7"},
            base,
            tmp_path,
            extra_env={"FORESIGHT_IDENTITY": "alice@acme", "FORESIGHT_USER_ID": "bob"},
        )
        assert result.returncode == 0, result.stderr
        lanes = {h.get("X-User-Id") for h in handler.headers if h.get("X-User-Id")}
        assert lanes == {"bob"}


class TestTransportAndCredentialRobustness:
    def test_auth_headers_use_the_user_id_over_the_identity_lane(self):
        credentials = sdk.Credentials(
            base_url="http://127.0.0.1:8764",
            identity="alice@acme",
            user_id="bob",
        )
        assert sdk.auth_headers(credentials)["X-User-Id"] == "bob"

    def test_resolve_credentials_ignores_a_group_readable_file(self, tmp_path: Path):
        path = tmp_path / "credentials.env"
        path.write_text("FORESIGHT_API_KEY=file-key\nFORESIGHT_USER_ID=bob\n", encoding="utf-8")
        path.chmod(0o644)
        credentials = sdk.resolve_credentials(env={}, creds_path=str(path))
        assert credentials.api_key == ""
        assert credentials.user_id == ""

    def test_resolve_credentials_reads_a_private_file(self, tmp_path: Path):
        path = tmp_path / "credentials.env"
        path.write_text("FORESIGHT_API_KEY=file-key\n", encoding="utf-8")
        path.chmod(0o600)
        assert sdk.resolve_credentials(env={}, creds_path=str(path)).api_key == "file-key"

    def test_resolve_credentials_reads_a_group_readable_file_where_modes_are_synthetic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Windows derives st_mode from the read-only flag, so every file there reads
        # as 0o666; the caller's ACL is the whole access control and this check must
        # not reject the file.
        path = tmp_path / "credentials.env"
        path.write_text("FORESIGHT_API_KEY=file-key\n", encoding="utf-8")
        path.chmod(0o644)
        monkeypatch.setattr(sdk, "_posix_modes_are_enforced", lambda: False)
        assert sdk.resolve_credentials(env={}, creds_path=str(path)).api_key == "file-key"

    def test_post_keeps_the_base_query_and_targets_the_path(self, stub_server):
        base, handler = stub_server
        handler.responses["/ui/api/inject?route=blue"] = (
            http.HTTPStatus.OK,
            {"formatted": "routed"},
        )
        client = sdk.ForesightClient(
            credentials=sdk.Credentials(base_url=f"{base}/?route=blue"),
        )
        status, data = client.post("/ui/api/inject", {"text": "hi"})
        assert status == http.HTTPStatus.OK
        assert data == {"formatted": "routed"}

    def test_parse_sse_result_keeps_a_line_separator_inside_a_payload(self):
        separator = chr(0x2028)
        body = 'data: {"result": {"content": [{"type": "text", "text": "a' + separator + 'b"}]}}\n\n'
        parsed = sdk.parse_sse_result(body)
        assert parsed is not None
        assert parsed["result"]["content"][0]["text"] == "a" + separator + "b"
