"""Tests for the shared plugin SDK (FOR-69) and its Claude hook consumer."""

from __future__ import annotations

import http
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SDK_DIR = REPO_ROOT / "plugins" / "sdk" / "python"
HOOK_PATH = REPO_ROOT / "plugins" / "claude" / "hooks" / "foresight_hook.py"

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

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append((self.path, body))
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
    handler = type("Handler", (_StubHandler,), {"responses": {}, "requests": []})
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

    def test_post_json_unreachable_raises_foresight_error(self):
        with pytest.raises(sdk.ForesightError):
            sdk.post_json("http://127.0.0.1:1/unreachable", {}, timeout=1.0)

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

    def test_post_json_allows_cleartext_credentials_to_ipv6_loopback(self):
        with pytest.raises(sdk.ForesightError) as excinfo:
            sdk.post_json(
                "http://[::1]:1/mcp",
                {},
                headers={"Authorization": "Bearer k"},
                timeout=1.0,
            )
        assert "cleartext" not in str(excinfo.value)

    def test_post_json_unauthenticated_cleartext_off_loopback_is_allowed(self, monkeypatch):
        def no_connect(*_args, **_kwargs):
            raise AssertionError("transport should be reached for an unauthenticated request")

        monkeypatch.setattr(sdk.http.client, "HTTPConnection", no_connect)
        with pytest.raises(sdk.ForesightError) as excinfo:
            sdk.post_json("http://foresight.example.com/mcp", {"a": 1})
        assert "cleartext" not in str(excinfo.value)

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

    def test_capture_text_and_transcript(self, stub_server):
        base, handler = stub_server
        handler.responses["/ui/api/capture"] = (http.HTTPStatus.OK, {})
        handler.responses["/session/transcript"] = (http.HTTPStatus.OK, {})
        client = sdk.ForesightClient(credentials=sdk.Credentials(base_url=base))
        client.capture_text("note", "sess1")
        client.capture_transcript("sess1", [{"role": "user", "content": "hi"}])
        paths = [p for p, _ in handler.requests]
        assert "/ui/api/capture" in paths
        assert "/session/transcript" in paths
        assert handler.requests[paths.index("/ui/api/capture")][1] == {
            "text": "note",
            "session_id": "sess1",
        }
        assert handler.requests[paths.index("/session/transcript")][1] == {
            "session_id": "sess1",
            "messages": [{"role": "user", "content": "hi"}],
        }

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
            credentials=sdk.Credentials(base_url="http://127.0.0.1:1"),
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

    def _run_hook(self, action: str, payload: dict, base_url: str, tmp_path: Path):
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
        return subprocess.run(
            [sys.executable, str(HOOK_PATH), action],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )

    def test_prompt_injects_context_block(self, stub_server, tmp_path: Path):
        base, handler = stub_server
        handler.responses["/ui/api/inject"] = (
            http.HTTPStatus.OK,
            {"formatted": "recalled memory", "top_score": 0.5},
        )
        handler.responses["/ui/api/telemetry/gating"] = (http.HTTPStatus.OK, {})
        result = self._run_hook(
            "prompt",
            {"prompt": "continue the migration", "session_id": "s1"},
            base,
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert "FORESIGHT CONTINUITY CONTEXT" in result.stdout
        assert "recalled memory" in result.stdout
        inject_calls = [b for p, b in handler.requests if p == "/ui/api/inject"]
        assert inject_calls
        assert inject_calls[0]["text"] == "continue the migration"

    def test_hook_survives_server_down(self, tmp_path: Path):
        result = self._run_hook(
            "prompt",
            {"prompt": "hello there", "session_id": "s2"},
            "http://127.0.0.1:1",
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
