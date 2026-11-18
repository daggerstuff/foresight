"""Updating fields that used to be frozen at store time.

Importance, emotional context, metrics, expiration and taxonomy were accepted on
store and then unreachable: the update path only wrote content, category, scope,
retention and tags.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from foresight.server import (
    ManageMemoriesParams,
    MemoryUpdateOptions,
    SearchMemoriesParams,
    manage_memories,
    search_memories,
)
from foresight.tenant_context import (
    reset_tenant_context,
    set_current_account_id,
    set_current_user_id,
)


@pytest.fixture(autouse=True)
def setup_test_ctx():
    set_current_user_id("_test_user_")
    set_current_account_id("_test_")
    yield
    reset_tenant_context()


def _store(content: str, **kwargs) -> str:
    kwargs.setdefault("category", "fact")
    result = manage_memories(ManageMemoriesParams(action="store", content=content, **kwargs))
    match = re.search(r"Stored memory (\S+)\.", result)
    assert match, result
    return match.group(1)


def _get(memory_id: str) -> dict:
    payload = json.loads(
        search_memories(SearchMemoriesParams(query_type="id", memory_id=memory_id, response_format="json"))
    )
    assert payload["found"] is True, payload
    return payload["item"]


def test_importance_and_metadata_are_updatable():
    memory_id = _store("update probe")
    assert _get(memory_id)["importance"] == pytest.approx(0.5)

    result = manage_memories(
        ManageMemoriesParams(
            action="update",
            memory_id=memory_id,
            updates=MemoryUpdateOptions(
                importance=0.95,
                emotional_context={"intensity": 0.8},
                metrics={"empathy": 0.7},
            ),
        )
    )

    assert result == f"Updated memory {memory_id}"
    item = _get(memory_id)
    assert item["importance"] == pytest.approx(0.95)
    assert item["emotional_context"] == {"intensity": 0.8}
    assert item["metrics"] == {"empathy": 0.7}


def test_taxonomy_is_updatable():
    memory_id = _store("taxonomy probe")
    assert _get(memory_id)["memory_type"] == "semantic"

    manage_memories(
        ManageMemoriesParams(
            action="update",
            memory_id=memory_id,
            updates=MemoryUpdateOptions(memory_type="episodic"),
        )
    )

    assert _get(memory_id)["memory_type"] == "episodic"


def test_expiration_can_be_set_after_storage():
    memory_id = _store("ttl probe")
    assert _get(memory_id)["expires_at"] is None

    manage_memories(
        ManageMemoriesParams(
            action="update",
            memory_id=memory_id,
            updates=MemoryUpdateOptions(expires_at="2030-01-01T00:00:00+00:00"),
        )
    )

    stored = datetime.fromisoformat(str(_get(memory_id)["expires_at"]))
    assert stored == datetime(2030, 1, 1, tzinfo=UTC)


def test_flat_arguments_reach_the_update_path():
    memory_id = _store("flat update probe")

    manage_memories(
        ManageMemoriesParams(
            action="update",
            memory_id=memory_id,
            importance=0.8,
            emotional_context={"valence": 0.4},
            metrics={"empathy": 0.6},
            expires_at="2031-05-06T07:08:09+00:00",
            memory_type="procedural",
        )
    )

    item = _get(memory_id)
    assert item["importance"] == pytest.approx(0.8)
    assert item["emotional_context"] == {"valence": 0.4}
    assert item["metrics"] == {"empathy": 0.6}
    assert datetime.fromisoformat(str(item["expires_at"])) == datetime(2031, 5, 6, 7, 8, 9, tzinfo=UTC)
    assert item["memory_type"] == "procedural"


def test_update_still_reports_missing_memories():
    result = manage_memories(
        ManageMemoriesParams(
            action="update",
            memory_id="does-not-exist",
            updates=MemoryUpdateOptions(importance=0.9),
        )
    )

    assert result == "Memory does-not-exist not found."


def test_an_empty_update_is_still_rejected():
    memory_id = _store("no-op probe")

    result = manage_memories(ManageMemoriesParams(action="update", memory_id=memory_id, updates=MemoryUpdateOptions()))

    assert result == "No updates provided."
