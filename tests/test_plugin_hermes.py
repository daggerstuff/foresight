"""Tests for the Foresight Hermes Agent memory provider plugin."""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest

from plugins.hermes import (
    PRE_COMPRESS_CHECKPOINT_API_VERSION,
    ForesightMemoryProvider,
    MemoryWrite,
    _Client,
    register,
)
from plugins.hermes.cli import register_cli

# sync_turn records the user turn and the assistant reply.
SYNC_TURN_MESSAGE_COUNT = 2

# _Client.request defaults to an 8 second socket timeout.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 8.0


class TestClient:
    def test_request_headers_and_url(self):
        client = _Client(base_url="http://test-server:8764", api_key="test-key", user_id="test-user")
        with patch("plugins.hermes._send") as mock_send:
            mock_send.return_value = (200, "OK", json.dumps({"ok": True, "count": 42}).encode("utf-8"))

            res = client.request("POST", "/test", body={"hello": "world"}, params={"limit": 5})

            assert res == {"ok": True, "count": 42}
            assert mock_send.call_count == 1
            method, url, data, headers, timeout = mock_send.call_args[0]
            assert method == "POST"
            assert url == "http://test-server:8764/test?limit=5"
            assert headers["Authorization"] == "Bearer test-key"
            assert headers["X-User-Id"] == "test-user"
            assert headers["Content-Type"] == "application/json"
            assert headers["User-Agent"] == "hermes-foresight/2.0"
            assert json.loads(data.decode("utf-8")) == {"hello": "world"}
            assert timeout == DEFAULT_REQUEST_TIMEOUT_SECONDS

    def test_request_transport_round_trip(self):
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                received["path"] = self.path
                received["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
                received["user_id"] = self.headers.get("X-User-Id")
                payload = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = _Client(base_url=f"http://127.0.0.1:{server.server_port}", api_key="", user_id="chad")
            assert client.request("POST", "/memories", body={"content": "note"}, params={"limit": 2}) == {"ok": True}
        finally:
            server.shutdown()
            server.server_close()

        assert received["path"] == "/memories?limit=2"
        assert received["body"] == {"content": "note"}
        assert received["user_id"] == "chad"

    def test_request_error_status(self):
        client = _Client(base_url="http://test-server:8764", api_key="", user_id="")
        mock_send_result = (404, "Not Found", json.dumps({"detail": "no such memory"}).encode("utf-8"))
        with (
            patch("plugins.hermes._send", return_value=mock_send_result),
            pytest.raises(RuntimeError, match="HTTP 404: no such memory"),
        ):
            client.request("GET", "/memories/missing")

    def test_request_connection_failure(self):
        client = _Client(base_url="http://test-server:8764", api_key="", user_id="")
        with (
            patch("plugins.hermes._send", side_effect=ConnectionRefusedError("refused")),
            pytest.raises(RuntimeError, match="Connection failed: refused"),
        ):
            client.request("GET", "/memories")

    def test_health_check(self):
        client = _Client(base_url="http://test-server:8764", api_key="", user_id="chad")
        memory_count = 12
        with patch.object(client, "request") as mock_req:
            mock_req.return_value = {"ok": True, "result": f"Memories ({memory_count} found):\n- [123] note"}
            h = client.health()
            assert h["ok"] is True
            assert h["count"] == memory_count
            assert "latency_ms" in h

    def test_health_failure(self):
        client = _Client(base_url="http://test-server:8764", api_key="", user_id="chad")
        with patch.object(client, "request", side_effect=RuntimeError("connection refused")):
            h = client.health()
            assert h["ok"] is False
            assert "connection refused" in h["error"]

    def test_reflect_sends_question(self):
        client = _Client(base_url="http://test-server:8764", api_key="", user_id="chad")
        with patch.object(client, "request") as mock_req:
            mock_req.return_value = {"answer": "synthesized reasoning"}
            ans = client.reflect("What was decided?")
            assert ans == {"answer": "synthesized reasoning"}
            mock_req.assert_called_once_with(
                "POST",
                "/reflect",
                body={"question": "What was decided?"},
                timeout=15.0,
            )

    def test_transcript_payload(self):
        client = _Client(base_url="http://test-server:8764", api_key="k", user_id="u")
        with patch.object(client, "request") as mock_req:
            mock_req.return_value = {"ok": True}
            client.transcript(
                session_id="s1",
                messages=[{"role": "user", "content": "hi"}],
                project_path="/repo",
                user_id="u",
                infer=True,
            )
            mock_req.assert_called_once_with(
                "POST",
                "/session/transcript",
                body={
                    "session_id": "s1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "infer": True,
                    "project_path": "/repo",
                    "user_id": "u",
                },
                timeout=30.0,
            )


class TestProvider:
    def test_provider_registration(self):
        ctx = MagicMock()
        register(ctx)
        ctx.register_memory_provider.assert_called_once()
        prov = ctx.register_memory_provider.call_args[0][0]
        assert isinstance(prov, ForesightMemoryProvider)
        assert prov.name == "foresight"
        assert prov.is_available() is True
        assert prov.pre_compress_checkpoint_api_version == PRE_COMPRESS_CHECKPOINT_API_VERSION

    def test_initialize(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FORESIGHT_USER_ID", raising=False)
        monkeypatch.delenv("FORESIGHT_IDENTITY", raising=False)
        prov = ForesightMemoryProvider()
        max_memories = 7
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(f"memory:\n  foresight:\n    url: http://custom:9999\n    max_memories: {max_memories}\n")

        prov.initialize(
            session_id="sess_abc",
            hermes_home=str(tmp_path),
            cwd="/test/workspace",
            user_id="alice:dev",
        )

        assert prov._session_id == "sess_abc"
        assert prov._project_path == "/test/workspace"
        assert prov._max_memories == max_memories
        assert prov._client is not None
        assert prov._client.base_url == "http://custom:9999"
        # Colon sanitized to underscore for Foresight
        assert prov._client.user_id == "alice_dev"

    def test_prefetch_trivial_prompt_skipped(self):
        prov = ForesightMemoryProvider()
        prov.initialize("sess_1")
        assert prov.prefetch("") == ""
        assert prov.prefetch("ok") == ""
        assert prov.prefetch("thanks") == ""
        assert prov.prefetch("yes") == ""
        assert prov.prefetch("/help") == ""
        assert prov.recall_status() is None

    def test_prefetch_substantive_recall(self):
        prov = ForesightMemoryProvider()
        prov.initialize("sess_1")
        assert prov._client is not None

        mock_inject = {
            "formatted": "[Relevant Context - 2 memories surfaced]\n- [1] fact one\n- [2] fact two",
            "memories": [{"id": "1"}, {"id": "2"}],
        }
        with patch.object(prov._client, "inject", return_value=mock_inject) as mock_inj:
            result = prov.prefetch("tell me about project x")
            assert "## Foresight Memory" in result
            assert "fact one" in result
            assert "fact two" in result

            status = prov.recall_status()
            assert status is not None
            assert status.provider_label == "Foresight"
            assert status.count == len(mock_inject["memories"])
            assert status.glyph == "🔮"

            # Cache hit for identical query
            result2 = prov.prefetch("tell me about project x")
            assert result2 == result
            assert mock_inj.call_count == 1

    def test_sync_turn_and_session_end(self):
        prov = ForesightMemoryProvider()
        prov.initialize("sess_1", cwd="/project")
        assert prov._client is not None

        with patch.object(prov._client, "transcript") as mock_transcript:
            prov.sync_turn("What is the goal?", "Build memory provider.", session_id="sess_1")
            if prov._sync_thread:
                prov._sync_thread.join(timeout=2.0)

            mock_transcript.assert_called_once()
            _args, kwargs = mock_transcript.call_args
            assert kwargs["session_id"] == "sess_1"
            assert kwargs["project_path"] == "/project"
            assert len(kwargs["messages"]) == SYNC_TURN_MESSAGE_COUNT
            assert kwargs["messages"][0]["content"] == "What is the goal?"

    def test_on_pre_compress(self):
        prov = ForesightMemoryProvider()
        prov.initialize("sess_1")
        assert prov._client is not None

        messages = [
            {"role": "user", "content": "msg 1"},
            {"role": "assistant", "content": "msg 2"},
        ]
        with patch.object(prov._client, "transcript") as mock_transcript:
            ret = prov.on_pre_compress(messages, require_checkpoint=True)
            assert "Distilled 2 messages" in ret
            mock_transcript.assert_called_once()

    def test_on_memory_write_mirror(self):
        prov = ForesightMemoryProvider()
        prov.initialize("sess_1")
        assert prov._client is not None

        with patch.object(prov._client, "store") as mock_store:
            prov.on_memory_write("add", "user", "Prefer pnpm")
            if prov._memwrite_thread:
                prov._memwrite_thread.join(timeout=2.0)
            mock_store.assert_called_once_with(
                "Prefer pnpm",
                MemoryWrite(category="preference", importance=0.7, tags=["built-in-mirror", "target:user"]),
            )

    def test_status_config(self):
        prov = ForesightMemoryProvider()
        prov.initialize("sess_1")
        assert prov._client is not None

        memory_count = 100
        with patch.object(prov._client, "health", return_value={"ok": True, "latency_ms": 45, "count": memory_count}):
            status = prov.get_status_config({})
            assert status["url"] == prov._client.base_url
            assert "connected ✓ (45ms)" in status["connection"]
            assert status["memories_in_brain"] == memory_count
            assert "hands-off proactive recall" in status["mode"]

    def test_tools_dispatch(self):
        prov = ForesightMemoryProvider()
        prov.initialize("sess_1")
        assert prov._client is not None

        schemas = prov.get_tool_schemas()
        names = {s["name"] for s in schemas}
        assert "foresight_search" in names
        assert "foresight_remember" in names
        assert "foresight_reflect" in names
        assert "foresight_profile" in names
        assert "foresight_context" in names

        with patch.object(prov._client, "reflect", return_value={"answer": "thoughtful answer"}):
            out = prov.handle_tool_call("foresight_reflect", {"question": "why?"})
            assert "thoughtful answer" in out

        with patch.object(prov._client, "store", return_value={"memory_id": "mem_123"}):
            out = prov.handle_tool_call(
                "foresight_remember",
                {"content": "fact", "category": "fact", "importance": 0.8},
            )
            assert "mem_123" in out


class TestCLI:
    def test_cli_parser_registered(self):
        parser = argparse.ArgumentParser()
        register_cli(parser)
        limit = 3
        args = parser.parse_args(["search", "fireworks", "--limit", str(limit)])
        assert args.foresight_action == "search"
        assert args.query == "fireworks"
        assert args.limit == limit

    def test_cli_status_command(self, capsys):
        parser = argparse.ArgumentParser()
        register_cli(parser)
        args = parser.parse_args(["status"])

        with patch("plugins.hermes.cli._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.base_url = "https://foresight.pixelated.love"
            mock_client.user_id = "chad"
            mock_client.api_key = "test_key"
            mock_client.health.return_value = {"ok": True, "latency_ms": 85, "count": 550}
            mock_client.list_memories.return_value = {"result": "- [123] Fireworks trained"}
            mock_get_client.return_value = mock_client

            args.func(args)
            captured = capsys.readouterr().out
            assert "Foresight Memory Brain Status" in captured
            assert "https://foresight.pixelated.love" in captured
            assert "connected ✓ (85ms)" in captured
            assert "550" in captured
            assert "Fireworks trained" in captured
