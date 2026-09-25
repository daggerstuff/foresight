"""REST API tests for the PIX-4704 HTTP surface.

Every endpoint wraps the same tool handler the MCP surface exposes, so these
tests assert the HTTP mapping (auth, identity headers, status codes) around
return values produced by the real tool functions against the Postgres test
backend. Return-value shapes mirror the legacy tool contract: JSON tool
responses map to their explicit ok/error status, legacy plain-text returns
become ``{"ok": true, "result": ...}``, and "not found" returns map to 404.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from starlette.testclient import TestClient

from foresight.auth import Role, get_auth_manager
from foresight.server import mcp

TENANT = "_test_"
USER = "alice"


def _headers(api_key: str, user: str = USER, tenant: str = TENANT) -> dict[str, str]:
    return {"X-API-Key": api_key, "X-User-Id": user, "X-Tenant-Id": tenant}


def _bearer(token: str, user: str = USER, tenant: str = TENANT) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-User-Id": user, "X-Tenant-Id": tenant}


def _memory_id_from_store(result: str) -> str:
    match = re.search(r"Stored memory (\S+)\.", result)
    assert match, result
    return match.group(1)


@pytest.fixture(scope="module")
def client():
    with TestClient(mcp.http_app()) as test_client:
        yield test_client


@pytest.fixture
def api_key() -> str:
    user = get_auth_manager().create_user("rest_user", "rest-user@test.local", "password-123")
    return user.api_key


def _store_memory(client: TestClient, api_key: str, content: str, **extra: Any) -> str:
    resp = client.post("/memories", json={"content": content, **extra}, headers=_headers(api_key))
    assert resp.status_code == 201, resp.text
    return _memory_id_from_store(resp.json()["result"])


def _set_vector_id(memory_id: str) -> None:
    """Archive requires vector_id; the embedding pipeline is out of scope for Phase A."""
    from foresight import server as server_module

    with server_module._global_backend.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE memories SET vector_id = %s WHERE id = %s", (f"vec_{memory_id}", memory_id))
        conn.commit()


class TestOpenApi:
    def test_spec_served_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert spec["openapi"] == "3.1.0"
        assert spec["info"]["title"] == "Foresight REST API"

    def test_spec_documents_all_endpoints(self, client: TestClient) -> None:
        spec = client.get("/openapi.json").json()
        for path in (
            "/memories",
            "/memories/batch",
            "/memories/{memory_id}",
            "/search",
            "/inject",
            "/documents",
            "/documents/{document_id}",
            "/profile",
        ):
            assert path in spec["paths"], path
        schemas = spec["components"]["schemas"]
        for model in (
            "StoreMemoryBody",
            "UpdateMemoryBody",
            "SearchBody",
            "InjectBody",
            "CreateDocumentBody",
            "BatchStoreBody",
        ):
            assert model in schemas, model


class TestAuth:
    def test_missing_credentials_rejected(self, client: TestClient) -> None:
        resp = client.post("/memories", json={"content": "no creds"}, headers={"X-Tenant-Id": TENANT})
        assert resp.status_code == 401
        assert resp.json()["ok"] is False
        assert "Authentication required" in resp.json()["error"]["message"]

    def test_invalid_credentials_rejected(self, client: TestClient, api_key: str) -> None:
        del api_key
        resp = client.post("/memories", json={"content": "bogus"}, headers=_headers("not-a-real-key"))
        assert resp.status_code == 401
        assert "Invalid or expired credentials" in resp.json()["error"]["message"]

    def test_api_key_via_x_api_key_header(self, client: TestClient, api_key: str) -> None:
        resp = client.post("/memories", json={"content": "key header auth"}, headers=_headers(api_key))
        assert resp.status_code == 201
        assert resp.json()["ok"] is True

    def test_api_key_via_bearer_header(self, client: TestClient, api_key: str) -> None:
        resp = client.post("/memories", json={"content": "bearer auth"}, headers=_bearer(api_key))
        assert resp.status_code == 201

    def test_session_token_via_bearer_header(self, client: TestClient) -> None:
        manager = get_auth_manager()
        user = manager.create_user("rest_session_user", "rest-session@test.local", "password-123")
        session_id = manager.create_session(user)
        resp = client.post("/memories", json={"content": "session auth"}, headers=_bearer(session_id))
        assert resp.status_code == 201

    def test_unauthenticated_mode_allows_anonymous(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FORESIGHT_ALLOW_UNAUTHENTICATED", "1")
        resp = client.post("/memories", json={"content": "open mode"}, headers={"X-Tenant-Id": TENANT})
        assert resp.status_code == 201


class TestIdentityAndScoping:
    def test_invalid_user_header_400(self, client: TestClient, api_key: str) -> None:
        resp = client.get("/memories", headers=_headers(api_key, user="has space"))
        assert resp.status_code == 400
        assert "Invalid X-User-Id" in resp.json()["error"]["message"]

    def test_invalid_tenant_header_400(self, client: TestClient, api_key: str) -> None:
        resp = client.get("/memories", headers=_headers(api_key, tenant="bad tenant!"))
        assert resp.status_code == 400
        assert "Invalid X-Tenant-Id" in resp.json()["error"]["message"]

    def test_tenant_access_denied_403(self, client: TestClient) -> None:
        user = get_auth_manager().create_user(
            "rest_scoped_user",
            "rest-scoped@test.local",
            "password-123",
            role=Role.USER,
            tenant_access=["_other_tenant_"],
        )
        resp = client.post("/memories", json={"content": "denied"}, headers=_headers(user.api_key))
        assert resp.status_code == 403
        assert "Tenant access denied" in resp.json()["error"]["message"]

    def test_user_scoping_isolates_memories(self, client: TestClient, api_key: str) -> None:
        _store_memory(client, api_key, "alice secret note")

        mine = client.get("/memories?q=alice", headers=_headers(api_key))
        assert mine.status_code == 200
        assert mine.json()["ok"] is True
        assert "alice secret note" in mine.json()["result"]

        theirs = client.get("/memories?q=alice", headers=_headers(api_key, user="bob"))
        assert theirs.status_code == 200
        assert theirs.json()["result"] == "No memories found."


class TestMemoryLifecycle:
    def test_store_returns_201_with_memory_id(self, client: TestClient, api_key: str) -> None:
        resp = client.post(
            "/memories",
            json={"content": "rest store check", "category": "fact", "importance": 0.7},
            headers=_headers(api_key),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["ok"] is True
        assert body["result"].startswith("Stored memory ")

    def test_id_lookup_after_store(self, client: TestClient, api_key: str) -> None:
        content = "parity lookup target"
        mid = _store_memory(client, api_key, content)
        resp = client.post("/search", json={"query_type": "id", "memory_id": mid}, headers=_headers(api_key))
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert f"[{mid}]" in result
        assert f"Content: {content}" in result

    def test_patch_updates_content(self, client: TestClient, api_key: str) -> None:
        mid = _store_memory(client, api_key, "before patch")
        resp = client.patch(f"/memories/{mid}", json={"content": "after patch"}, headers=_headers(api_key))
        assert resp.status_code == 200
        assert resp.json()["result"] == f"Updated memory {mid}"

        lookup = client.post("/search", json={"query_type": "id", "memory_id": mid}, headers=_headers(api_key))
        assert "Content: after patch" in lookup.json()["result"]

    def test_patch_missing_memory_404(self, client: TestClient, api_key: str) -> None:
        resp = client.patch("/memories/no-such-id", json={"content": "x"}, headers=_headers(api_key))
        assert resp.status_code == 404

    def test_delete_removes_memory(self, client: TestClient, api_key: str) -> None:
        mid = _store_memory(client, api_key, "doomed memory")
        resp = client.delete(f"/memories/{mid}", headers=_headers(api_key))
        assert resp.status_code == 200
        assert resp.json()["result"] == f"Deleted memory {mid}"

        lookup = client.post("/search", json={"query_type": "id", "memory_id": mid}, headers=_headers(api_key))
        assert lookup.status_code == 404

    def test_delete_missing_memory_404(self, client: TestClient, api_key: str) -> None:
        resp = client.delete("/memories/no-such-id", headers=_headers(api_key))
        assert resp.status_code == 404

    def test_archive_with_vector_id(self, client: TestClient, api_key: str) -> None:
        mid = _store_memory(client, api_key, "archivable memory")
        _set_vector_id(mid)
        resp = client.delete(f"/memories/{mid}", params={"archive": "true"}, headers=_headers(api_key))
        assert resp.status_code == 200
        assert resp.json()["result"].startswith(f"Archived memory {mid} to ghost node")

    def test_id_lookup_missing_404(self, client: TestClient, api_key: str) -> None:
        resp = client.post("/search", json={"query_type": "id", "memory_id": "no-such-id"}, headers=_headers(api_key))
        assert resp.status_code == 404


class TestOtherEndpoints:
    def test_batch_store_multiple_items(self, client: TestClient, api_key: str) -> None:
        resp = client.post(
            "/memories/batch",
            json={"items": [{"content": "batch one"}, {"content": "batch two"}]},
            headers=_headers(api_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["action"] == "batch_store"
        assert body["count"] == 2
        assert all(r["ok"] for r in body["results"])
        assert all(r["memory_id"] for r in body["results"])

    def test_batch_rejects_empty_items_422(self, client: TestClient, api_key: str) -> None:
        resp = client.post("/memories/batch", json={"items": []}, headers=_headers(api_key))
        assert resp.status_code == 422

    def test_inject_returns_formatted(self, client: TestClient, api_key: str) -> None:
        resp = client.post(
            "/inject", json={"conversation_text": "we were talking about anxiety"}, headers=_headers(api_key)
        )
        assert resp.status_code == 200
        assert "formatted" in resp.json()

    def test_document_create_and_get(self, client: TestClient, api_key: str) -> None:
        resp = client.post(
            "/documents",
            json={"title": "Rest Doc", "content": "paragraph text\n\nsecond paragraph with more detail here"},
            headers=_headers(api_key),
        )
        assert resp.status_code == 201
        body = resp.json()
        doc_id = body["document"]["id"]
        assert doc_id
        assert isinstance(body["chunks"], list)
        assert body["chunks"]

        fetched = client.get(f"/documents/{doc_id}", headers=_headers(api_key))
        assert fetched.status_code == 200
        assert fetched.json()["id"] == doc_id
        assert fetched.json()["title"] == "Rest Doc"

    def test_get_missing_document_404(self, client: TestClient, api_key: str) -> None:
        resp = client.get("/documents/no-such-doc", headers=_headers(api_key))
        assert resp.status_code == 404

    def test_profile_layers(self, client: TestClient, api_key: str) -> None:
        resp = client.get("/profile", headers=_headers(api_key))
        assert resp.status_code == 200
        body = resp.json()
        assert "static" in body
        assert "dynamic" in body

    def test_store_missing_content_422(self, client: TestClient, api_key: str) -> None:
        resp = client.post("/memories", json={"category": "fact"}, headers=_headers(api_key))
        assert resp.status_code == 422
        assert "Invalid request body" in resp.json()["error"]["message"]

    def test_invalid_json_body_400(self, client: TestClient, api_key: str) -> None:
        resp = client.post(
            "/memories",
            content=b"not json",
            headers={**_headers(api_key), "content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert "valid JSON" in resp.json()["error"]["message"]
