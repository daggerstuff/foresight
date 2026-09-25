"""Tests for PIX-4703 inferred-fact review queue (``manage_inferred`` MCP tool).

Covers:

- Migration v16 columns (``review_status``, ``review_reason``) exist on
  ``memories`` with NULL defaults and existing rows are unaffected.
- Queue ordering: supporting-parent count desc, then ``created_at`` desc.
- Queue filtering: excludes non-inferred, declined, ghosted, and approved rows.
- ``approve`` / ``decline`` / ``undo`` state transitions, idempotency, and
  user/tenant scoping.
- ``decline`` soft-forgets (``is_ghost = 1``) but stays recoverable via
  ``undo``.
- The ``curate_review`` prompt surfaces the inferred queue and tool name.
"""

import json
import sqlite3
import uuid

import pytest

UID = "_test_user_"
TENANT = "_test_"
NOW = "2026-09-25T10:00:00+00:00"


@pytest.fixture
def pg_db(monkeypatch):
    """Postgres test backend (conftest session fixture) with tenant context and
    the ``sqlite3.connect("postgres")`` mock from test_server.py, so tests can
    reach the same DB the server tools write to via get_db_connection()."""
    from foresight.memory_relationships import reset_memory_relationship_store
    from foresight.tenant_context import set_current_account_id, set_current_user_id

    set_current_user_id(UID)
    set_current_account_id(TENANT)
    reset_memory_relationship_store()

    real_sqlite3_connect = sqlite3.connect

    def mock_sqlite3_connect(database, *args, **kwargs):
        if database == "postgres":
            from foresight.server import PostgresPooledConnection, _global_backend

            pool = _global_backend._pool
            return PostgresPooledConnection(pool.getconn(), pool)
        return real_sqlite3_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", mock_sqlite3_connect)

    yield

    reset_memory_relationship_store()
    from foresight.tenant_context import reset_tenant_context

    reset_tenant_context()


def _pg():
    """DB handle over the mocked sqlite3.connect("postgres") shim."""
    return sqlite3.connect("postgres")


def _tool(action, **kwargs):
    from foresight.server import manage_inferred

    return manage_inferred(action=action, **kwargs)


def _list_items(limit=None):
    res = json.loads(_tool("list", user_id=UID, limit=limit))
    assert res["ok"], res
    return res


def _insert_memory(
    conn,
    memory_id,
    content,
    *,
    uid=UID,
    tenant=TENANT,
    created_at=NOW,
    inferred=1,
    is_ghost=0,
    review_status=None,
    review_reason=None,
    importance=0.5,
):
    conn.execute(
        """
        INSERT INTO memories
            (id, content, tenant_id, user_id, category, scope, retention,
             created_at, updated_at, tags, is_ghost, inferred,
             review_status, review_reason, importance)
        VALUES (?, ?, ?, ?, 'fact', 'session', 'short_term',
                ?, ?, '[]', ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            content,
            tenant,
            uid,
            created_at,
            created_at,
            is_ghost,
            inferred,
            review_status,
            review_reason,
            importance,
        ),
    )
    conn.commit()


def _add_derives_edge(conn, source_id, target_id, *, uid=UID, tenant=TENANT):
    conn.execute(
        """
        INSERT INTO memory_relationships
            (id, tenant_id, user_id, source_memory_id, target_memory_id,
             relationship_type, confidence, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, 'derives', 1.0, '{}', ?)
        ON CONFLICT (id) DO NOTHING
        """,
        (uuid.uuid4().hex, tenant, uid, source_id, target_id, NOW),
    )
    conn.commit()


def _state(conn, memory_id):
    row = conn.execute(
        "SELECT inferred, is_ghost, review_status, review_reason FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    assert row is not None, f"memory {memory_id} missing"
    return row


# ====== Migration v16 ======


@pytest.mark.usefixtures("pg_db")
class TestMigrationV16Columns:
    def test_columns_exist_nullable_no_default(self):
        conn = _pg()
        try:
            rows = conn.execute(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_name = 'memories' "
                "AND column_name IN ('review_status', 'review_reason')"
            ).fetchall()
        finally:
            conn.close()
        by_name = {r["column_name"]: r for r in rows}
        assert set(by_name) == {"review_status", "review_reason"}
        for col in by_name.values():
            assert col["data_type"] == "text"
            assert col["is_nullable"] == "YES"
            assert col["column_default"] is None

    def test_rows_default_to_unreviewed(self):
        conn = _pg()
        try:
            conn.execute(
                "INSERT INTO memories (id, content, tenant_id, user_id, created_at) VALUES (?, ?, ?, ?, ?)",
                ("plain_row", "plain content", TENANT, UID, NOW),
            )
            conn.commit()
            row = conn.execute(
                "SELECT inferred, is_latest, review_status, review_reason FROM memories WHERE id = ?",
                ("plain_row",),
            ).fetchone()
        finally:
            conn.close()
        assert row["inferred"] == 0
        assert row["is_latest"] == 1
        assert row["review_status"] is None
        assert row["review_reason"] is None


# ====== Queue listing ======


@pytest.mark.usefixtures("pg_db")
class TestInferredQueue:
    def _seed_ordering_fixture(self, conn):
        for pid in ("par_1", "par_2", "par_3", "par_4"):
            _insert_memory(conn, pid, f"parent fact {pid}", inferred=0)
        _insert_memory(conn, "inf_w", "two parents oldest", created_at="2026-09-25T10:00:00+00:00")
        _insert_memory(conn, "inf_x", "one parent oldest", created_at="2026-09-25T08:00:00+00:00")
        _insert_memory(conn, "inf_y", "one parent newer", created_at="2026-09-25T09:00:00+00:00")
        _insert_memory(conn, "inf_z", "no parents newest", created_at="2026-09-25T11:00:00+00:00")
        _add_derives_edge(conn, "inf_w", "par_1")
        _add_derives_edge(conn, "inf_w", "par_2")
        _add_derives_edge(conn, "inf_x", "par_3")
        _add_derives_edge(conn, "inf_y", "par_4")

    def test_orders_by_parent_count_then_recency(self):
        conn = _pg()
        try:
            self._seed_ordering_fixture(conn)
        finally:
            conn.close()
        ids = [i["id"] for i in _list_items()["items"]]
        assert ids == ["inf_w", "inf_y", "inf_x", "inf_z"]

    def test_excludes_non_inferred_declined_ghosted_and_approved(self):
        conn = _pg()
        try:
            _insert_memory(conn, "inf_pending", "still pending", created_at=NOW)
            _insert_memory(
                conn,
                "inf_declined",
                "declined row",
                review_status="declined",
                review_reason="bad inference",
            )
            _insert_memory(conn, "inf_ghosted", "ghosted row", is_ghost=1)
            _insert_memory(conn, "inf_stated", "plain stated fact", inferred=0)
            _insert_memory(
                conn,
                "inf_approved",
                "approved row",
                inferred=0,
                review_status="approved",
            )
        finally:
            conn.close()
        ids = [i["id"] for i in _list_items()["items"]]
        assert ids == ["inf_pending"]

    def test_respects_limit_and_caps_at_50(self):
        conn = _pg()
        try:
            for n, ts in enumerate(
                ("2026-09-25T10:00:00+00:00", "2026-09-25T09:00:00+00:00", "2026-09-25T08:00:00+00:00")
            ):
                _insert_memory(conn, f"inf_{n}", f"inferred fact {n}", created_at=ts)
        finally:
            conn.close()
        res = _list_items(limit=2)
        assert res["count"] == 2
        assert res["limit"] == 2
        assert [i["id"] for i in res["items"]] == ["inf_0", "inf_1"]
        assert _list_items(limit=100)["limit"] == 50

    def test_items_round_trip_content_and_fields(self):
        conn = _pg()
        try:
            _insert_memory(conn, "inf_detail", "Client appears to sleep better after medication change")
        finally:
            conn.close()
        item = _list_items()["items"][0]
        assert item["id"] == "inf_detail"
        assert item["content"] == "Client appears to sleep better after medication change"
        assert item["category"] == "fact"
        assert item["parent_count"] == 0
        assert item["review_status"] is None


# ====== approve ======


@pytest.mark.usefixtures("pg_db")
class TestInferredApprove:
    def test_approve_clears_inferred_and_stamps_status(self):
        conn = _pg()
        try:
            _insert_memory(conn, "inf_a", "inferred fact to approve")
        finally:
            conn.close()
        res = json.loads(_tool("approve", user_id=UID, memory_id="inf_a"))
        assert res == {
            "ok": True,
            "action": "approve",
            "memory_id": "inf_a",
            "already_applied": False,
            "status": "approved",
        }
        conn = _pg()
        try:
            row = _state(conn, "inf_a")
        finally:
            conn.close()
        assert row["inferred"] == 0
        assert row["review_status"] == "approved"
        assert row["review_reason"] is None
        assert row["is_ghost"] == 0
        # approved memories leave the queue
        assert _list_items()["count"] == 0

    def test_approve_is_idempotent(self):
        conn = _pg()
        try:
            _insert_memory(conn, "inf_b", "inferred fact")
        finally:
            conn.close()
        assert json.loads(_tool("approve", user_id=UID, memory_id="inf_b"))["already_applied"] is False
        res = json.loads(_tool("approve", user_id=UID, memory_id="inf_b"))
        assert res["ok"] is True
        assert res["already_applied"] is True

    def test_approve_non_inferred_errors(self):
        conn = _pg()
        try:
            _insert_memory(conn, "inf_c", "stated fact", inferred=0)
        finally:
            conn.close()
        res = json.loads(_tool("approve", user_id=UID, memory_id="inf_c"))
        assert res["ok"] is False
        assert "not inferred" in res["error"]["message"]

    def test_approve_missing_memory_errors(self):
        res = json.loads(_tool("approve", user_id=UID, memory_id="nope"))
        assert res["ok"] is False
        assert "not found" in res["error"]["message"]


# ====== decline ======


@pytest.mark.usefixtures("pg_db")
class TestInferredDecline:
    def test_decline_soft_forgets_and_records_reason(self):
        conn = _pg()
        try:
            _insert_memory(conn, "inf_d", "bad inferred fact")
        finally:
            conn.close()
        res = json.loads(_tool("decline", user_id=UID, memory_id="inf_d", reason="contradicts source"))
        assert res["ok"] is True
        assert res["already_applied"] is False
        assert res["status"] == "declined"
        conn = _pg()
        try:
            row = _state(conn, "inf_d")
        finally:
            conn.close()
        assert row["is_ghost"] == 1
        assert row["review_status"] == "declined"
        assert row["review_reason"] == "contradicts source"
        # declined memories leave the queue
        assert _list_items()["count"] == 0

    def test_decline_is_idempotent(self):
        conn = _pg()
        try:
            _insert_memory(conn, "inf_e", "bad inferred fact")
        finally:
            conn.close()
        assert json.loads(_tool("decline", user_id=UID, memory_id="inf_e"))["already_applied"] is False
        res = json.loads(_tool("decline", user_id=UID, memory_id="inf_e", reason="again"))
        assert res["ok"] is True
        assert res["already_applied"] is True

    def test_decline_non_inferred_errors(self):
        conn = _pg()
        try:
            _insert_memory(conn, "inf_f", "stated fact", inferred=0)
        finally:
            conn.close()
        res = json.loads(_tool("decline", user_id=UID, memory_id="inf_f"))
        assert res["ok"] is False
        assert "not inferred" in res["error"]["message"]


# ====== undo ======


@pytest.mark.usefixtures("pg_db")
class TestInferredUndo:
    def test_undo_after_decline_restores_to_queue(self):
        conn = _pg()
        try:
            _insert_memory(conn, "inf_g", "bad inferred fact")
        finally:
            conn.close()
        assert json.loads(_tool("decline", user_id=UID, memory_id="inf_g", reason="wrong"))["ok"]
        assert _list_items()["count"] == 0
        res = json.loads(_tool("undo", user_id=UID, memory_id="inf_g"))
        assert res["ok"] is True
        assert res["status"] == "unreviewed"
        conn = _pg()
        try:
            row = _state(conn, "inf_g")
        finally:
            conn.close()
        assert row["inferred"] == 1
        assert row["is_ghost"] == 0
        assert row["review_status"] is None
        assert row["review_reason"] is None
        assert [i["id"] for i in _list_items()["items"]] == ["inf_g"]

    def test_undo_after_approve_restores_to_queue(self):
        conn = _pg()
        try:
            _insert_memory(conn, "inf_h", "inferred fact")
        finally:
            conn.close()
        assert json.loads(_tool("approve", user_id=UID, memory_id="inf_h"))["ok"]
        assert _list_items()["count"] == 0
        res = json.loads(_tool("undo", user_id=UID, memory_id="inf_h"))
        assert res["ok"] is True
        conn = _pg()
        try:
            row = _state(conn, "inf_h")
        finally:
            conn.close()
        assert row["inferred"] == 1
        assert row["review_status"] is None
        assert _list_items()["count"] == 1

    def test_undo_without_prior_action_errors(self):
        conn = _pg()
        try:
            _insert_memory(conn, "inf_i", "fresh inferred fact")
        finally:
            conn.close()
        res = json.loads(_tool("undo", user_id=UID, memory_id="inf_i"))
        assert res["ok"] is False
        assert "no review action to undo" in res["error"]["message"]

    def test_undo_missing_memory_errors(self):
        res = json.loads(_tool("undo", user_id=UID, memory_id="nope"))
        assert res["ok"] is False
        assert "not found" in res["error"]["message"]


# ====== scoping ======


@pytest.mark.usefixtures("pg_db")
class TestScoping:
    def test_other_user_rows_are_isolated(self):
        conn = _pg()
        try:
            _insert_memory(conn, "inf_mine", "my inferred fact")
            _insert_memory(
                conn,
                "inf_theirs",
                "their inferred fact",
                uid="_other_user_",
            )
        finally:
            conn.close()
        ids = [i["id"] for i in _list_items()["items"]]
        assert "inf_theirs" not in ids
        assert "inf_mine" in ids
        for action in ("approve", "decline", "undo"):
            kw = {"reason": "x"} if action == "decline" else {}
            res = json.loads(_tool(action, user_id=UID, memory_id="inf_theirs", **kw))
            assert res["ok"] is False
            assert "not found" in res["error"]["message"]

    def test_other_tenant_rows_are_isolated(self):
        conn = _pg()
        try:
            _insert_memory(conn, "inf_mine", "my inferred fact")
            _insert_memory(
                conn,
                "inf_elsewhere",
                "other tenant inferred fact",
                tenant="_other_tenant_",
            )
        finally:
            conn.close()
        ids = [i["id"] for i in _list_items()["items"]]
        assert "inf_elsewhere" not in ids
        assert "inf_mine" in ids
        res = json.loads(_tool("approve", user_id=UID, memory_id="inf_elsewhere"))
        assert res["ok"] is False
        assert "not found" in res["error"]["message"]


# ====== tool dispatch ======


@pytest.mark.usefixtures("pg_db")
class TestManageInferredDispatch:
    def test_no_action_or_options_errors(self):
        from foresight.server import manage_inferred

        res = json.loads(manage_inferred())
        assert res["ok"] is False
        assert "either 'options' or 'action' must be provided" in res["error"]["message"]

    def test_action_without_memory_id_errors(self):
        res = json.loads(_tool("approve", user_id=UID))
        assert res["ok"] is False
        assert "'memory_id' is required" in res["error"]["message"]

    def test_options_object_is_accepted(self):
        from foresight.server import InferredAction

        conn = _pg()
        try:
            _insert_memory(conn, "inf_opt", "inferred fact via options")
        finally:
            conn.close()
        from foresight.server import manage_inferred

        res = json.loads(manage_inferred(options=InferredAction(action="list", limit=5), user_id=UID))
        assert res["ok"] is True
        assert res["count"] == 1

    def test_limit_floors_at_one(self):
        res = _list_items(limit=0)
        assert res["limit"] == 1


# ====== curate_review prompt wiring ======


@pytest.mark.usefixtures("pg_db")
class TestCurateReviewPrompt:
    def test_prompt_surfaces_queue_and_tool(self):
        conn = _pg()
        try:
            _insert_memory(
                conn,
                "inf_prompt",
                "Client appears to be making steady progress toward stated goals",
            )
        finally:
            conn.close()
        from foresight.server import curate_review_prompt

        prompt = curate_review_prompt(user_id=UID)
        assert "Inferred Memory Queue" in prompt
        assert "manage_inferred" in prompt
        assert "inf_prompt" in prompt

    def test_prompt_with_empty_queue_still_renders(self):
        from foresight.server import curate_review_prompt

        prompt = curate_review_prompt(user_id=UID)
        assert "Inferred Memory Queue" in prompt
        assert '"count": 0' in prompt
