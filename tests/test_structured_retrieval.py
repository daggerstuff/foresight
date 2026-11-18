"""Structured retrieval (`response_format="json"`) and the match total.

The text response reports the page size and truncates content, which forced
every consumer to parse prose and re-hydrate each id. The JSON response carries
the records and a real total.
"""

from __future__ import annotations

import json
import re

import pytest

from foresight.server import (
    ManageMemoriesParams,
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


SEEDED_MATCHES = 3
LONG_TAIL_LEN = 200


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


def test_list_json_reports_total_not_page_size():
    for i in range(SEEDED_MATCHES):
        _store(f"pagination probe {i}")

    payload = json.loads(search_memories(SearchMemoriesParams(query_type="list", limit=1, response_format="json")))

    assert payload["count"] == 1
    assert payload["total"] == SEEDED_MATCHES
    assert payload["has_more"] is True
    assert len(payload["items"]) == 1


def test_list_json_last_page_reports_no_more():
    for i in range(SEEDED_MATCHES):
        _store(f"pagination probe {i}")

    payload = json.loads(
        search_memories(SearchMemoriesParams(query_type="list", limit=2, offset=2, response_format="json"))
    )

    assert payload["count"] == 1
    assert payload["total"] == SEEDED_MATCHES
    assert payload["has_more"] is False


def test_json_returns_untruncated_content():
    tail = "x" * LONG_TAIL_LEN
    _store(f"structured retrieval {tail}")

    payload = json.loads(search_memories(SearchMemoriesParams(query_type="list", response_format="json")))
    content = payload["items"][0]["content"]

    assert content == f"structured retrieval {tail}"


def test_text_format_is_unchanged():
    memory_id = _store("legacy prose probe")

    result = search_memories(SearchMemoriesParams(query_type="list"))

    assert result == f"Memories (1 found):\n- [{memory_id}] (session/short_term) legacy prose probe..."


def test_id_lookup_json_returns_the_record():
    memory_id = _store("id lookup probe")

    payload = json.loads(
        search_memories(SearchMemoriesParams(query_type="id", memory_id=memory_id, response_format="json"))
    )

    assert payload["found"] is True
    assert payload["item"]["id"] == memory_id
    assert payload["item"]["scope"] in {"session", "arc", "trait", "fact"}


def test_id_lookup_json_reports_a_miss_without_raising():
    payload = json.loads(
        search_memories(SearchMemoriesParams(query_type="id", memory_id="does-not-exist", response_format="json"))
    )

    assert payload["found"] is False
    assert payload["item"] is None


def test_keyword_json_respects_column_filters():
    _store("filter probe alpha", category="alpha")
    _store("filter probe beta", category="beta")

    payload = json.loads(
        search_memories(
            SearchMemoriesParams(
                query="filter probe",
                use_hybrid=False,
                category="alpha",
                response_format="json",
            )
        )
    )

    assert payload["total"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["category"] == "alpha"
