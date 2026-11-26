"""Tests for FOR-55: skill crystallization as reviewable, HMAC-signed SKILL.md."""

from __future__ import annotations

import json
import re
import sqlite3
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

import foresight.skill_crystallization as sc
from foresight.experience import EpisodeVerdict, build_episode, distill_experiences, record_outcome
from foresight.memory_relationships import LinkMemoriesOptions, get_memory_relationship_store
from foresight.observations import archive_observation, curate_observations, list_observations
from foresight.profile_export import (
    SkillSignatureUnavailableError,
    sign_document,
    sign_skill_document,
    verify_skill_document,
)
from foresight.reflect import UserScope
from foresight.server import (
    USER_ID,
    CurationRunAction,
    InjectContextParams,
    LinkMemoriesParams,
    ManageCurationRunsParams,
    ManageInferredParams,
    ManageMemoriesParams,
    ManageMemoryVersionsParams,
    _execute_curation_run,
    get_db_connection,
    inject_context,
    link_memories,
    manage_curation_runs,
    manage_inferred,
    manage_memories,
    manage_memory_versions,
    mcp,
    resource_skills,
)
from foresight.skill_crystallization import (
    _existing_skill_by_observation,
    crystallize_skills,
    export_skills_to_dir,
    list_skills,
    load_skill,
    parse_skill_markdown,
    retire_stale_skills,
    select_skill_candidates,
    verify_skill_markdown,
)
from foresight.tenant_context import reset_tenant_context, set_current_account_id, set_current_user_id
from foresight_cli import cli as cli_module

TENANT = "_test_"
THREE_EPISODES = 3
VERSION_TWO = 2


@pytest.fixture(autouse=True)
def setup_test_ctx():
    set_current_user_id(USER_ID)
    set_current_account_id(TENANT)
    yield
    reset_tenant_context()


def _scope(uid: str = USER_ID, tenant: str = TENANT) -> UserScope:
    return UserScope(user_id=uid, tenant_id=tenant)


def _store_procedural(
    content: str,
    *,
    procedure: str = "deploy-smoke-check",
    outcome: str = "success",
    resolved: bool = False,
    is_sensitive: bool | None = None,
) -> str:
    metrics: dict[str, object] = {"procedure_key": procedure, "outcome": outcome}
    if resolved:
        metrics["resolved"] = True
    tags = ["procedural", f"procedure:{procedure}", f"outcome:{outcome}"]
    raw = manage_memories(
        ManageMemoriesParams(
            action="store",
            content=content,
            category="procedural",
            scope="arc",
            retention="long_term",
            memory_type="procedural",
            tags=tags,
            metrics=metrics,
            is_sensitive=is_sensitive,
            user_id=USER_ID,
        )
    )
    match = re.search(r"Stored memory (\S+)\.", raw)
    assert match, raw
    return match.group(1)


def _seed_three_successes(procedure: str = "deploy-smoke-check") -> list[str]:
    return [
        _store_procedural(f"Run alembic upgrade head and smoke check step 1 ({procedure})", procedure=procedure),
        _store_procedural(f"Verify /healthz returns 200 after migration ({procedure})", procedure=procedure),
        _store_procedural(f"Confirm worker queue drains cleanly after rollout ({procedure})", procedure=procedure),
    ]


def test_proof_threshold_requires_n_successes_and_no_unresolved_failure():
    # Only 2 successes: below default threshold of 3.
    _store_procedural("Step A of release check", procedure="release-check")
    _store_procedural("Step B of release check", procedure="release-check")
    curate_observations(_scope())
    assert select_skill_candidates(_scope()) == []

    # Add 3rd success -> crosses threshold.
    _store_procedural("Step C of release check", procedure="release-check")
    curate_observations(_scope())
    candidates = select_skill_candidates(_scope())
    assert len(candidates) == 1
    assert candidates[0]["skill_name"] == "release-check"
    assert candidates[0]["success_count"] == THREE_EPISODES
    assert candidates[0]["failure_count"] == 0

    # Add an unresolved failure -> blocks promotion.
    _store_procedural("Step D failed with timeout on release-check", procedure="release-check", outcome="failure")
    curate_observations(_scope())
    assert select_skill_candidates(_scope()) == []


def test_resolved_failure_does_not_block_and_sensitive_is_excluded():
    _seed_three_successes("safe-backup")
    _store_procedural(
        "Backup failed once due to transient lock (now resolved)",
        procedure="safe-backup",
        outcome="failure",
        resolved=True,
    )
    curate_observations(_scope())
    candidates = select_skill_candidates(_scope())
    assert len(candidates) == 1
    assert candidates[0]["skill_name"] == "safe-backup"

    # Sensitive procedural memories are excluded from distillation.
    _store_procedural("Secret rotation step 1", procedure="secret-proc", is_sensitive=True)
    _store_procedural("Secret rotation step 2", procedure="secret-proc", is_sensitive=True)
    _store_procedural("Secret rotation step 3", procedure="secret-proc", is_sensitive=True)
    curate_observations(_scope())
    assert all(c["skill_name"] != "secret-proc" for c in select_skill_candidates(_scope()))


def test_resolved_failure_stays_out_of_the_procedure_and_counts_separately(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FORESIGHT_PROFILE_SIGNING_KEY", "resolved-failure-key")
    _seed_three_successes("safe-rollback")
    _store_procedural(
        "Rollback failed once on a transient lock (now resolved)",
        procedure="safe-rollback",
        outcome="failure",
        resolved=True,
    )
    curate_observations(_scope())
    result = crystallize_skills(_scope())
    assert result["staged_count"] == 1

    exported = export_skills_to_dir(_scope(), tmp_path, include_unapproved=True)
    assert exported["exported_count"] == 1
    content = (tmp_path / "safe-rollback" / "SKILL.md").read_text(encoding="utf-8")

    procedure = content.split("## Procedure")[1].split("## Provenance")[0]
    assert "transient lock" not in procedure
    assert "failures: 1, unresolved: 0" in content


def test_episodes_table_integration_when_present(tmp_path: Path, monkeypatch):
    ep_db = tmp_path / "episodes.db"
    setup_conn = sqlite3.connect(ep_db)
    setup_conn.executescript(
        """
        CREATE TABLE episodes (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            outcome TEXT,
            outcome_score REAL,
            resolved INTEGER DEFAULT 0,
            summary TEXT
        );
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            content TEXT,
            category TEXT,
            memory_type TEXT,
            tags TEXT,
            metrics TEXT,
            is_ghost INTEGER DEFAULT 0,
            is_latest INTEGER DEFAULT 1,
            superseded_by TEXT,
            is_sensitive INTEGER DEFAULT 0,
            expired_at TEXT,
            expires_at TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE memory_relationships (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            source_memory_id TEXT NOT NULL,
            target_memory_id TEXT NOT NULL,
            relationship_type TEXT NOT NULL
        );
        CREATE TABLE observations (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            agent_id TEXT,
            project TEXT,
            pattern_key TEXT NOT NULL,
            observation_text TEXT NOT NULL,
            category TEXT,
            confidence REAL,
            supporting_memory_ids TEXT,
            evidence_count INTEGER,
            status TEXT,
            first_seen_at TEXT,
            last_refined_at TEXT,
            version INTEGER
        );
        """
    )
    for idx in range(1, 4):
        setup_conn.execute(
            "INSERT INTO episodes (id, tenant_id, user_id, outcome, outcome_score, resolved, summary) "
            "VALUES (?, ?, ?, 'success', 0.9, 1, ?)",
            (f"ep_{idx}", TENANT, USER_ID, f"Episode {idx}: ran pytest cleanly"),
        )
    setup_conn.execute(
        """INSERT INTO observations
        (id, tenant_id, user_id, agent_id, project, pattern_key, observation_text,
         category, confidence, supporting_memory_ids, evidence_count, status,
         first_seen_at, last_refined_at, version)
        VALUES (?, ?, ?, 'default', 'default', 'procedural:pytest-check',
                'Run pytest with -x before merging', 'procedural', 0.9, ?, 3, 'active',
                '2026-11-20T00:00:00Z', '2026-11-20T00:00:00Z', 1)""",
        ("obs_ep_1", TENANT, USER_ID, json.dumps(["ep_1", "ep_2", "ep_3"])),
    )
    setup_conn.commit()
    setup_conn.close()

    def _sqlite_connect():
        c = sqlite3.connect(ep_db)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr("foresight.reflect._connect", _sqlite_connect)
    monkeypatch.setattr("foresight.skill_crystallization._connect", _sqlite_connect)

    candidates = select_skill_candidates(_scope())
    assert any(c["skill_name"] == "pytest-check" and c["success_count"] == THREE_EPISODES for c in candidates)


def test_curation_run_surfaces_candidates_and_stages_skill():
    _seed_three_successes("curation-proc")
    curate_observations(_scope())

    with patch("foresight.server._start_curation_worker", side_effect=_execute_curation_run):
        created = json.loads(
            manage_curation_runs(
                ManageCurationRunsParams(
                    options=CurationRunAction(action="create", source_bank_id="default"),
                    user_id=USER_ID,
                )
            )
        )
        assert created["ok"] is True
        fetched = json.loads(
            manage_curation_runs(
                ManageCurationRunsParams(
                    options=CurationRunAction(action="get", run_id=created["run"]["id"]),
                    user_id=USER_ID,
                )
            )
        )

    summary = fetched["run"]["summary"]
    assert any(c["skill_name"] == "curation-proc" for c in summary["skill_candidates"])
    assert any(s["skill_name"] == "curation-proc" and s["status"] == "pending" for s in summary["staged_skills"])


def test_skill_hmac_signing_and_memory_versions(monkeypatch):
    monkeypatch.setenv("FORESIGHT_PROFILE_SIGNING_KEY", "test-skill-hmac-secret")
    _seed_three_successes("signed-deploy")
    curate_observations(_scope())

    res = crystallize_skills(_scope())
    assert res["staged_count"] == 1
    skill_id = res["skills"][0]["memory_id"]

    pending = list_skills(_scope(), status="pending")
    assert len(pending) == 1
    skill = pending[0]
    assert skill["signature_valid"] is True

    ok, parsed = verify_skill_markdown(skill["content"])
    assert ok is True
    assert parsed["name"] == "signed-deploy"
    assert parsed["metadata"]["signature_alg"] == "HMAC-SHA256"

    # Tampering breaks verification.
    tampered = skill["content"].replace("signed-deploy", "tampered-skill", 1)
    assert verify_skill_markdown(tampered)[0] is False
    assert verify_skill_document({"skill_name": "tampered"}, skill["signature"]) is False

    # Version 1 is recorded in memory_versions.
    v1_text = manage_memory_versions(ManageMemoryVersionsParams(action="list", memory_id=skill_id, user_id=USER_ID))
    assert "v1:" in v1_text

    # Refine observation with a 4th success and re-crystallize -> version 2 in memory_versions.
    _store_procedural("Step 4: verify telemetry metrics post-deploy", procedure="signed-deploy")
    curate_observations(_scope())
    res2 = crystallize_skills(_scope())
    assert res2["updated_count"] == 1
    assert res2["skills"][0]["version"] == VERSION_TWO

    v2_text = manage_memory_versions(ManageMemoryVersionsParams(action="list", memory_id=skill_id, user_id=USER_ID))
    assert "v2:" in v2_text
    assert "v1:" in v2_text


def test_approval_queue_gates_agent_loading_and_injection():
    _seed_three_successes("gated-skill")
    curate_observations(_scope())
    res = crystallize_skills(_scope())
    skill_id = res["skills"][0]["memory_id"]

    # Staged skill appears in manage_inferred queue.
    queue = json.loads(manage_inferred(ManageInferredParams(action="list", user_id=USER_ID)))
    assert any(item["id"] == skill_id and item["category"] == "skill" for item in queue["items"])

    # Before approval: NOT loadable by agents, resource, or inject_context.
    assert list_skills(_scope(), status="approved") == []
    assert load_skill("gated-skill", _scope()) is None
    assert json.loads(resource_skills())["count"] == 0
    injected_before = json.loads(
        inject_context(
            InjectContextParams(
                conversation_text="gated-skill procedure",
                user_id=USER_ID,
                include_details=True,
                min_relevance=0.0,
            )
        )
    )
    assert injected_before["skills"] == []
    assert all(m["memory_id"] != skill_id for m in injected_before["memories"])

    # Approve via manage_inferred -> becomes loadable.
    approved = json.loads(manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID)))
    assert approved["ok"] is True
    assert approved["status"] == "approved"

    assert len(list_skills(_scope(), status="approved")) == 1
    loaded = load_skill("gated-skill", _scope())
    assert loaded is not None
    assert loaded["memory_id"] == skill_id
    assert json.loads(resource_skills())["count"] == 1
    injected_after = json.loads(
        inject_context(
            InjectContextParams(
                conversation_text="gated-skill procedure",
                user_id=USER_ID,
                include_details=True,
                min_relevance=0.0,
            )
        )
    )
    assert any(s["memory_id"] == skill_id for s in injected_after["skills"])

    # Undo returns skill to pending queue and removes agent loadability.
    undone = json.loads(manage_inferred(ManageInferredParams(action="undo", memory_id=skill_id, user_id=USER_ID)))
    assert undone["ok"] is True
    assert load_skill("gated-skill", _scope()) is None

    # Tenant/user isolation: another user/tenant cannot load approved skill.
    manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID))
    assert load_skill("gated-skill", UserScope(user_id="other_user", tenant_id=TENANT)) is None
    assert load_skill("gated-skill", UserScope(user_id=USER_ID, tenant_id="other_tenant")) is None


def test_tampered_skill_in_db_is_refused_by_agent_loader():
    _seed_three_successes("tamper-guard")
    curate_observations(_scope())
    res = crystallize_skills(_scope())
    skill_id = res["skills"][0]["memory_id"]
    manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID))
    assert load_skill("tamper-guard", _scope()) is not None

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE memories SET content = REPLACE(content, 'tamper-guard', 'evil-override') WHERE id = ?",
            (skill_id,),
        )
        conn.commit()
    finally:
        conn.close()

    assert load_skill("tamper-guard", _scope()) is None
    assert list_skills(_scope(), status="approved") == []


def test_skill_auto_retires_when_evidence_superseded_or_contradicted():
    ids = _seed_three_successes("retirable-skill")
    curate_observations(_scope())
    res = crystallize_skills(_scope())
    skill_id = res["skills"][0]["memory_id"]
    manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID))
    assert load_skill("retirable-skill", _scope()) is not None

    # Contradict one supporting memory -> skill auto-retires and is flagged in review queue.
    contra_id = _store_procedural(
        "Do NOT use old migration order; it corrupts indexes",
        procedure="other",
        outcome="failure",
    )
    link_memories(
        LinkMemoriesParams(
            source_memory_id=contra_id,
            target_memory_id=ids[0],
            relationship_type="contradicts",
            user_id=USER_ID,
        )
    )

    assert load_skill("retirable-skill", _scope()) is None
    retired_list = list_skills(_scope(), status="retired")
    assert len(retired_list) == 1
    assert retired_list[0]["memory_id"] == skill_id
    assert retired_list[0]["retired"] is True

    # Flagged in manage_inferred review queue and cannot be approved while retired.
    queue = json.loads(manage_inferred(ManageInferredParams(action="list", user_id=USER_ID)))
    flagged = next(i for i in queue["items"] if i["id"] == skill_id)
    assert flagged["review_status"] == "retired"
    assert "Retired:" in flagged["review_reason"]

    approve_retired = json.loads(
        manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID))
    )
    assert approve_retired["ok"] is False
    assert "retired" in approve_retired["error"]["message"].lower()


def test_archive_observation_auto_retires_skill():
    _seed_three_successes("obs-archive-skill")
    curate_observations(_scope())
    obs = next(o for o in list_observations(_scope()) if o["pattern_key"] == "procedural:obs-archive-skill")
    res = crystallize_skills(_scope())
    skill_id = res["skills"][0]["memory_id"]
    manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID))

    assert archive_observation(obs["id"], user_id=USER_ID, tenant_id=TENANT) is True
    assert load_skill("obs-archive-skill", _scope()) is None
    assert len(list_skills(_scope(), status="retired")) == 1


def test_expired_contradiction_source_does_not_retire_skill():
    ids = _seed_three_successes("expiring-contradiction")
    curate_observations(_scope())
    skill_id = crystallize_skills(_scope())["skills"][0]["memory_id"]
    manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID))

    contra_id = _store_procedural(
        "Do NOT use the old migration order; it corrupts indexes",
        procedure="other",
        outcome="failure",
    )
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE memories SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", contra_id),
        )
        conn.commit()
    finally:
        conn.close()

    link_memories(
        LinkMemoriesParams(
            source_memory_id=contra_id,
            target_memory_id=ids[0],
            relationship_type="contradicts",
            user_id=USER_ID,
        )
    )

    assert retire_stale_skills(_scope()) == []
    assert list_skills(_scope(), status="retired") == []
    assert load_skill("expiring-contradiction", _scope()) is not None


def test_crystallization_in_a_project_lane_never_reuses_the_shared_lane_skill():
    lane = UserScope(user_id=USER_ID, tenant_id=TENANT, agent_id="default", project="proj-b")

    _seed_three_successes("lane-scoped-proc")
    curate_observations(_scope())
    first = crystallize_skills(_scope())["skills"][0]
    manage_inferred(ManageInferredParams(action="approve", memory_id=first["memory_id"], user_id=USER_ID))
    assert load_skill("lane-scoped-proc", _scope()) is not None

    staged = crystallize_skills(lane)["skills"]
    assert staged, "a project lane must not reuse the shared-lane skill"
    second = staged[0]
    assert second["memory_id"] != first["memory_id"]

    conn = get_db_connection()
    try:
        rows = {
            row["id"]: dict(row)
            for row in conn.execute(
                "SELECT id, agent_id, project, review_status FROM memories WHERE id IN (?, ?)",
                (first["memory_id"], second["memory_id"]),
            ).fetchall()
        }
    finally:
        conn.close()

    assert rows[first["memory_id"]]["project"] in (None, "default")
    assert rows[first["memory_id"]]["review_status"] == "approved"
    assert rows[second["memory_id"]]["project"] == "proj-b"
    assert load_skill("lane-scoped-proc", _scope()) is not None
    assert load_skill("lane-scoped-proc", lane) is not None


def test_list_skills_reads_a_bounded_page_instead_of_every_version(monkeypatch):
    """A long-lived scope holds many superseded versions; the read stays paged."""
    calls: list[tuple[str, tuple]] = []

    class _EmptyCursor:
        def fetchall(self):
            return []

    class _RecordingConn:
        def execute(self, sql, params=()):
            calls.append((str(sql), tuple(params)))
            return _EmptyCursor()

        def close(self):
            return None

    monkeypatch.setattr(sc, "_connect", _RecordingConn)
    monkeypatch.setattr(sc, "_SKILL_LIST_PAGE_SIZE", 2)
    scope = UserScope(user_id=USER_ID, tenant_id=TENANT)
    assert list_skills(scope, limit=5, reconcile=False, verify_signatures=False) == []
    assert calls, "list_skills must read from the database"
    sql, params = calls[0]
    assert " LIMIT " in sql.upper()
    assert params[-2:] == (2, 0)


def test_two_observations_sharing_a_pattern_key_keep_separate_skills():
    """The exact observation wins over a shared pattern key."""
    conn = get_db_connection()
    try:
        for memory_id, observation_id, updated in (
            ("skill_obs_old", "obs-old", "2026-01-01T00:00:00+00:00"),
            ("skill_obs_new", "obs-new", "2026-01-02T00:00:00+00:00"),
        ):
            conn.execute(
                "INSERT INTO memories"
                " (id, content, tenant_id, user_id, category, memory_type, metrics,"
                " review_status, is_ghost, is_latest, inferred, agent_id, project,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'skill', 'procedural', ?, 'approved', 0, 1, 0,"
                " 'default', 'default', ?, ?)",
                (
                    memory_id,
                    "name: shared-pattern",
                    TENANT,
                    USER_ID,
                    json.dumps({"observation_id": observation_id, "pattern_key": "shared-key"}),
                    updated,
                    updated,
                ),
            )
        found = _existing_skill_by_observation(conn, "obs-old", "shared-key", _scope())
        assert found is not None
        assert found["id"] == "skill_obs_old"
        fallback = _existing_skill_by_observation(conn, "obs-unknown", "shared-key", _scope())
        assert fallback is not None
        assert fallback["id"] == "skill_obs_new"
    finally:
        conn.close()


def test_expired_contradiction_source_is_ignored_by_the_retirement_sweep():
    ids = _seed_three_successes("sweep-expired-contradiction")
    curate_observations(_scope())
    skill_id = crystallize_skills(_scope())["skills"][0]["memory_id"]
    manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID))

    contra_id = _store_procedural(
        "Do NOT use the old migration order; it corrupts indexes",
        procedure="other",
        outcome="failure",
    )
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE memories SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", contra_id),
        )
        conn.commit()
    finally:
        conn.close()

    get_memory_relationship_store().link_memories(
        source_memory_id=contra_id,
        target_memory_id=ids[0],
        relationship_type="contradicts",
        user_id=USER_ID,
        options=LinkMemoriesOptions(tenant_id=TENANT),
    )

    assert retire_stale_skills(_scope()) == []
    assert load_skill("sweep-expired-contradiction", _scope()) is not None


def test_cli_skills_list_approve_export_and_verify(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("FORESIGHT_PROFILE_SIGNING_KEY", "cli-test-key")
    _seed_three_successes("cli-workflow")
    curate_observations(_scope())

    assert cli_module.main(["--json", "skills", "crystallize", "--user-id", USER_ID]) == 0
    cryst_out = json.loads(capsys.readouterr().out)
    assert cryst_out["staged_count"] == 1
    skill_id = cryst_out["skills"][0]["memory_id"]

    # Default export skips unapproved skills.
    out_dir = tmp_path / "skills"
    empty_export = export_skills_to_dir(_scope(), out_dir)
    assert empty_export["exported_count"] == 0

    # Approve via CLI, then export to skills/-compatible layout.
    assert cli_module.main(["--json", "skills", "approve", skill_id, "--user-id", USER_ID]) == 0
    capsys.readouterr()

    assert cli_module.main(["--json", "skills", "list", "--user-id", USER_ID]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1
    assert listed["skills"][0]["skill_name"] == "cli-workflow"
    assert listed["skills"][0]["signature_status"] == "valid"

    assert cli_module.main(["--json", "skills", "export", "--output-dir", str(out_dir), "--user-id", USER_ID]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["exported_count"] == 1

    skill_file = out_dir / "cli-workflow" / "SKILL.md"
    assert skill_file.is_file()
    parsed = parse_skill_markdown(skill_file.read_text(encoding="utf-8"))
    assert parsed["name"] == "cli-workflow"

    assert cli_module.main(["--json", "skills", "verify", str(skill_file)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["signature_valid"] is True
    assert verified["signature_status"] == "valid"


def test_write_time_supersede_retires_skill(monkeypatch):
    monkeypatch.setenv("FORESIGHT_SUPERSEDE_DETECTION", "1")
    _store_procedural("Always use legacy docker-compose v1 command for local stack", procedure="docker-stack")
    _store_procedural("Verify legacy docker-compose v1 ps output shows healthy", procedure="docker-stack")
    _store_procedural("Stop legacy docker-compose v1 containers cleanly", procedure="docker-stack")
    curate_observations(_scope())

    res = crystallize_skills(_scope())
    assert res["staged_count"] == 1
    skill_id = res["skills"][0]["memory_id"]
    manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID))
    assert load_skill("docker-stack", _scope()) is not None

    # Write-time negation-flip supersede invalidates the supporting memory -> skill auto-retires.
    manage_memories(
        ManageMemoriesParams(
            action="store",
            content="We no longer use legacy docker-compose v1 command for local stack",
            category="procedural",
            memory_type="procedural",
            user_id=USER_ID,
        )
    )
    assert load_skill("docker-stack", _scope()) is None
    assert len(list_skills(_scope(), status="retired")) == 1


def test_reconciliation_retires_when_live_evidence_falls_below_the_staging_threshold():
    ids = _seed_three_successes("threshold-recheck")
    curate_observations(_scope())
    staged = crystallize_skills(_scope(), min_successes=2)
    assert staged["staged_count"] == 1
    skill_id = staged["skills"][0]["memory_id"]
    manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID))
    assert load_skill("threshold-recheck", _scope()) is not None

    conn = get_db_connection()
    conn.execute(
        "UPDATE memories SET metrics = ?, tags = ? WHERE id IN (?, ?)",
        (
            json.dumps({"procedure_key": "threshold-recheck", "outcome": "mixed"}),
            json.dumps(["procedural", "procedure:threshold-recheck", "outcome:mixed"]),
            ids[0],
            ids[1],
        ),
    )
    conn.commit()

    retired = retire_stale_skills(_scope())
    assert [item["memory_id"] for item in retired] == [skill_id]
    assert load_skill("threshold-recheck", _scope()) is None
    assert len(list_skills(_scope(), status="retired")) == 1


def test_rest_api_skills_endpoints(monkeypatch):
    monkeypatch.setenv("FORESIGHT_ALLOW_UNAUTHENTICATED", "1")
    _seed_three_successes("rest-skill")
    curate_observations(_scope())

    headers = {"X-User-Id": USER_ID, "X-Tenant-Id": TENANT}
    with TestClient(mcp.http_app()) as client:
        cryst = client.post("/skills/crystallize", json={}, headers=headers)
        assert cryst.status_code == HTTPStatus.OK
        cryst_body = cryst.json()
        assert cryst_body["staged_count"] == 1
        skill_id = cryst_body["skills"][0]["memory_id"]

        # Unapproved skill is not returned by default GET /skills or GET /skills/{name}.
        assert client.get("/skills", headers=headers).json()["count"] == 0
        assert client.get("/skills/rest-skill", headers=headers).status_code == HTTPStatus.NOT_FOUND

        manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID))

        listed = client.get("/skills", headers=headers)
        assert listed.status_code == HTTPStatus.OK
        assert listed.json()["count"] == 1
        loaded = client.get("/skills/rest-skill", headers=headers)
        assert loaded.status_code == HTTPStatus.OK
        assert loaded.json()["skill"]["memory_id"] == skill_id


def test_distilled_experience_episodes_crystallize_into_skill():
    for idx in range(3):
        sid = f"ep-cryst-{idx}"
        build_episode(
            sid,
            user_id=USER_ID,
            tenant_id=TENANT,
            messages=[
                {"role": "user", "content": "run database migration verification checklist"},
                {"role": "assistant", "content": "running migration check"},
                {"role": "tool", "content": "12 passed in 1.1s\nexit code 0"},
            ],
        )
        record_outcome(
            USER_ID,
            TENANT,
            EpisodeVerdict(outcome="success", score_source="ci_result", session_id=sid),
        )

    distilled = distill_experiences(user_id=USER_ID, tenant_id=TENANT)
    assert distilled["successes_distilled"] >= 1

    res = crystallize_skills(_scope())
    assert res["staged_count"] == 1
    skill_id = res["skills"][0]["memory_id"]
    manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID))
    loaded = load_skill(res["skills"][0]["skill_name"], _scope())
    assert loaded is not None
    assert loaded["signature_valid"] is True


def test_skill_signatures_fail_closed_without_a_configured_secret(monkeypatch):
    """A public development key must never authenticate a skill package."""
    monkeypatch.delenv("FORESIGHT_PROFILE_SIGNING_KEY", raising=False)
    monkeypatch.delenv("FORESIGHT_ENCRYPTION_KEY", raising=False)
    document = {"skill_name": "unsigned", "content": "Step 1: do the thing"}

    # sign_document falls back to the public development key, so this is the
    # signature an untrusted file can carry.
    forged = sign_document(document)
    assert verify_skill_document(document, forged) is False

    with pytest.raises(SkillSignatureUnavailableError):
        sign_skill_document(document)


def test_unsigned_artifact_reports_missing_signature_not_tampering(monkeypatch):
    monkeypatch.delenv("FORESIGHT_PROFILE_SIGNING_KEY", raising=False)
    monkeypatch.delenv("FORESIGHT_ENCRYPTION_KEY", raising=False)
    _seed_three_successes("unsigned-report")
    curate_observations(_scope())
    staged = crystallize_skills(_scope())
    skill_id = staged["skills"][0]["memory_id"]
    unsigned = next(s for s in list_skills(_scope(), status="pending") if s["memory_id"] == skill_id)
    assert 'signature: ""' in unsigned["content"]

    ok, parsed = verify_skill_markdown(unsigned["content"])
    assert ok is False
    assert parsed["signature_status"] == "missing"

    forged = re.sub(r"(?m)^\s*signature:.*$", '  signature: "deadbeef"', unsigned["content"], count=1)
    assert forged != unsigned["content"]
    ok_forged, forged_parsed = verify_skill_markdown(forged)
    assert ok_forged is False
    assert forged_parsed["signature_status"] == "invalid"


def test_configuring_a_key_resigns_a_previously_unsigned_skill(monkeypatch):
    monkeypatch.delenv("FORESIGHT_PROFILE_SIGNING_KEY", raising=False)
    monkeypatch.delenv("FORESIGHT_ENCRYPTION_KEY", raising=False)
    _seed_three_successes("late-signing")
    curate_observations(_scope())
    staged = crystallize_skills(_scope())
    assert staged["staged_count"] == 1
    skill_id = staged["skills"][0]["memory_id"]

    monkeypatch.setenv("FORESIGHT_PROFILE_SIGNING_KEY", "test-skill-hmac-secret")
    resigned = crystallize_skills(_scope())
    assert resigned["updated_count"] == 1
    repaired = next(s for s in list_skills(_scope(), status="pending") if s["memory_id"] == skill_id)
    assert repaired["signature_valid"] is True
    assert repaired["signature"]


def test_approve_signs_a_skill_staged_while_unsigned(monkeypatch):
    monkeypatch.delenv("FORESIGHT_PROFILE_SIGNING_KEY", raising=False)
    monkeypatch.delenv("FORESIGHT_ENCRYPTION_KEY", raising=False)
    _seed_three_successes("approve-signing")
    curate_observations(_scope())
    skill_id = crystallize_skills(_scope())["skills"][0]["memory_id"]
    monkeypatch.setenv("FORESIGHT_PROFILE_SIGNING_KEY", "test-skill-hmac-secret")
    manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID))
    approved = next(s for s in list_skills(_scope(), status="approved") if s["memory_id"] == skill_id)
    assert approved["signature_valid"] is True
    assert approved["signature_status"] == "valid"
    assert load_skill(skill_id, _scope()) is not None


def test_expired_skill_is_not_loadable_by_memory_id(monkeypatch):
    monkeypatch.setenv("FORESIGHT_PROFILE_SIGNING_KEY", "test-skill-hmac-secret")
    _seed_three_successes("expiring-skill")
    curate_observations(_scope())
    skill_id = crystallize_skills(_scope())["skills"][0]["memory_id"]
    manage_inferred(ManageInferredParams(action="approve", memory_id=skill_id, user_id=USER_ID))
    scope = _scope()
    assert load_skill(skill_id, scope) is not None

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE memories SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", skill_id),
        )
        conn.commit()
    finally:
        conn.close()

    assert load_skill(skill_id, scope) is None
