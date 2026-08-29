"""Security assurance and regression test suite for Foresight."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from foresight.llm_providers.openai import OpenAIClient
from foresight.server import _validate_lengths, _validate_paths
from foresight.websocket.server import WebSocketServer


def test_threat_model_document_exists():
    """Verify THREAT_MODEL.md exists and contains STRIDE threat analysis."""
    root_dir = Path(__file__).parent.parent
    threat_model_file = root_dir / "THREAT_MODEL.md"
    assert threat_model_file.exists(), "THREAT_MODEL.md must exist in root repository"
    content = threat_model_file.read_text()
    assert "STRIDE" in content
    assert "Spoofing" in content
    assert "Tampering" in content
    assert "Information Disclosure" in content
    assert "AES-256-GCM" in content


def test_websocket_server_defaults_to_localhost():
    """Verify WebSocket server binds to 127.0.0.1 loopback interface by default."""
    sig = inspect.signature(WebSocketServer.start)
    assert sig.parameters["host"].default == "127.0.0.1"


def test_llm_provider_url_scheme_validation():
    """Verify LLM providers reject unsafe schemes like file:// or gopher://."""
    client = OpenAIClient(api_key="test_key", base_url="file:///etc/passwd")

    with pytest.raises(ValueError, match="Only http/https schemes are permitted"):
        client.complete("test prompt")


def test_path_validation_blocks_directory_traversal():
    """Verify path validator blocks path traversal attempts."""
    result = _validate_paths({"output_path": "../../etc/shadow"})
    assert result == "Path traversal not allowed"


def test_length_validation_bounds_user_input():
    """Verify user inputs are strictly length bounded."""
    oversized_content = "A" * 200_000
    result = _validate_lengths({"content": oversized_content})
    assert result is not None
    assert "exceeds maximum length" in result
