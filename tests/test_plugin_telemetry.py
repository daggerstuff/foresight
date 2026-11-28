"""Tests for the plugin benefit/telemetry standard (FOR-70)."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from foresight.auth import get_auth_manager
from foresight.server import mcp
from foresight.telemetry import (
    MAX_RECENT_PLUGIN_EVENTS,
    PLUGIN_EVENT_FIELD_VALUE_MAX,
    TelemetryStore,
    set_telemetry_store,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SDK_DIR = REPO_ROOT / "plugins" / "sdk" / "python"
EVENT_SCHEMA_PATH = REPO_ROOT / "plugins" / "telemetry" / "event.schema.json"
SAMPLES_DIR = REPO_ROOT / "plugins" / "telemetry" / "samples"

_spec = importlib.util.spec_from_file_location("foresight_plugin_sdk", SDK_DIR / "foresight_plugin_sdk.py")
assert _spec is not None
assert _spec.loader is not None
sdk = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sdk
_spec.loader.exec_module(sdk)


@pytest.fixture
def telemetry_store(tmp_path: Path):
    store = TelemetryStore(file_path=tmp_path / "telemetry.json")
    set_telemetry_store(store)
    yield store
    set_telemetry_store(None)


class TestRecordPluginEvent:
    def test_counts_and_recent(self, telemetry_store: TelemetryStore):
        telemetry_store.record_plugin_event("recall.injected", plugin="claude", surface="claude-code", latency_ms=42.5)
        telemetry_store.record_plugin_event("recall.injected", plugin="claude")
        summary = telemetry_store.get_summary()
        assert summary["plugin_events"]["counts"] == {"recall.injected": 2}
        recent = summary["plugin_events"]["recent"]
        assert len(recent) == summary["plugin_events"]["counts"]["recall.injected"]
        assert recent[0]["latency_ms"] == pytest.approx(42.5)
        assert recent[0]["plugin"] == "claude"

    def test_fields_sanitized_to_scalars(self, telemetry_store: TelemetryStore):
        telemetry_store.record_plugin_event(
            "capture.stored",
            fields={
                "items": 3,
                "kinds": "decision,preference",
                "nested": {"drop": "me"},
                "long": "x" * 500,
                "nan": math.nan,
            },
        )
        entry = telemetry_store.get_summary()["plugin_events"]["recent"][0]
        fields = entry["fields"]
        assert fields["items"] == pytest.approx(3)
        assert fields["kinds"] == "decision,preference"
        assert "nested" not in fields
        assert "nan" not in fields
        assert len(fields["long"]) == PLUGIN_EVENT_FIELD_VALUE_MAX

    def test_recent_ring_bounded(self, telemetry_store: TelemetryStore):
        for i in range(105):
            telemetry_store.record_plugin_event(f"evt.{i}")
        recent = telemetry_store.get_summary()["plugin_events"]["recent"]
        assert len(recent) == MAX_RECENT_PLUGIN_EVENTS
        assert telemetry_store.get_summary()["plugin_events"]["counts"]["evt.0"] == 1

    def test_bad_event_name_rejected(self, telemetry_store: TelemetryStore):
        with pytest.raises(ValueError, match="plugin event name"):
            telemetry_store.record_plugin_event("")
        with pytest.raises(ValueError, match="plugin event name"):
            telemetry_store.record_plugin_event("x" * 65)

    def test_persistence_round_trip(self, tmp_path: Path):
        path = tmp_path / "telemetry.json"
        store = TelemetryStore(file_path=path)
        store.record_plugin_event("plugin.health", plugin="hermes", fields={"ok": True})
        reloaded = TelemetryStore(file_path=path)
        counts = reloaded.get_summary()["plugin_events"]["counts"]
        assert counts == {"plugin.health": 1}


@pytest.fixture(scope="module")
def client():
    with TestClient(mcp.http_app()) as test_client:
        yield test_client


class TestTelemetryEventEndpoint:
    @pytest.fixture
    def api_key(self) -> str:
        user = get_auth_manager().create_user("telemetry_user", "telemetry@test.local", "pw-12345")
        return user.api_key

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"X-API-Key": api_key, "X-User-Id": "telemetry_user"}

    def test_event_recorded(self, client: TestClient, api_key: str, telemetry_store: TelemetryStore):
        resp = client.post(
            "/ui/api/telemetry/event",
            json={
                "event": "recall.injected",
                "plugin": "claude",
                "surface": "claude-code",
                "latency_ms": 12.0,
                "fields": {"memories_injected": 2},
            },
            headers=self._headers(api_key),
        )
        assert resp.status_code == HTTPStatus.OK, resp.text
        assert resp.json()["recorded"] == "recall.injected"
        counts = telemetry_store.get_summary()["plugin_events"]["counts"]
        assert counts.get("recall.injected") == 1

    def test_invalid_event_name_400(self, client: TestClient, api_key: str):
        resp = client.post(
            "/ui/api/telemetry/event",
            json={"event": ""},
            headers=self._headers(api_key),
        )
        assert resp.status_code == HTTPStatus.BAD_REQUEST

    @pytest.mark.usefixtures("telemetry_store")
    def test_summary_exposes_plugin_events(self, client: TestClient, api_key: str):
        client.post(
            "/ui/api/telemetry/event",
            json={"event": "plugin.health", "plugin": "hermes", "fields": {"ok": True}},
            headers=self._headers(api_key),
        )
        resp = client.get("/ui/api/telemetry", headers=self._headers(api_key))
        assert resp.status_code == HTTPStatus.OK
        plugin_events = resp.json()["telemetry"]["plugin_events"]
        assert plugin_events["counts"].get("plugin.health") == 1


class TestSdkTrackEvent:
    """SDK track_event posts a schema-conformant payload."""

    def test_track_event_payload(self):
        captured: dict[str, Any] = {}

        class FakeClient(sdk.ForesightClient):
            def post(self, path, payload, timeout=None):
                captured["path"] = path
                captured["payload"] = payload
                captured["timeout"] = timeout
                return 200, {"ok": True}

        client = FakeClient(credentials=sdk.Credentials(base_url="http://x"))
        client.track_event(
            "capture.stored",
            plugin="hermes",
            surface="hooks",
            latency_ms=9.0,
            fields={"items_stored": 2},
        )
        assert captured["path"] == "/ui/api/telemetry/event"
        payload = captured["payload"]
        assert payload["event"] == "capture.stored"
        assert payload["fields"] == {"items_stored": 2}
        jsonschema = pytest.importorskip("jsonschema")
        schema = json.loads(EVENT_SCHEMA_PATH.read_text())
        jsonschema.validate(payload, schema)


class TestEventSchema:
    def test_schema_loads(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = json.loads(EVENT_SCHEMA_PATH.read_text())
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_sample_event_validates(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = json.loads(EVENT_SCHEMA_PATH.read_text())
        sample = json.loads((SAMPLES_DIR / "event-recall-injected.json").read_text())
        jsonschema.validate(sample, schema)

    def test_sample_rejects_nested_fields(self):
        jsonschema = pytest.importorskip("jsonschema")
        schema = json.loads(EVENT_SCHEMA_PATH.read_text())
        bad = {"event": "x.y", "fields": {"nested": {"a": 1}}}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)
