"""Tests for evaluation harness (PIX-3953).

Covers: PII scanner, fixture seeding, scenario execution, report
formatting, metric collection, edge cases, and baseline comparison.
"""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace

import pytest

import foresight.eval_harness as eval_harness_module
from foresight.eval_harness import (
    EXPERIENCE_SEED_MEMORIES,
    FIXTURE_MEMORIES,
    PII_MATCH_PREVIEW_CHARS,
    SCENARIOS,
    EvalHarness,
    EvalReport,
    EvalRunOptions,
    ScenarioResult,
    run_eval,
    scan_for_pii,
)
from foresight.tenant_context import (
    get_current_account_id,
    get_current_user_id,
    reset_tenant_context,
    set_current_account_id,
    set_current_user_id,
)
from foresight_cli.commands import eval as eval_cmd

# seed_fixtures is the floor every scenario relies on; more is fine, less is a
# broken fixture set.
MIN_SEEDED_ENTITIES = 5
MIN_SEEDED_LINKS = 5

# Scenarios whose expected memories do not exist until seeding distils them.
# The harness resolves these from its own seeded episodes at run time; the ids
# below are placeholders that must never survive into a run.
GENERATED_SCENARIO_IDS = {
    "S12_experience_repeat_task": "__experience_success__",
    "S13_experience_failure_signature": "__experience_failure__",
}

# =============================================================================
# PII Scanner
# =============================================================================


class TestPiiScanner:
    def test_detects_email(self):
        findings = scan_for_pii("Contact me at user@example.com for details.")
        assert len(findings) >= 1
        assert findings[0]["type"] == "email"

    def test_detects_phone(self):
        findings = scan_for_pii("Call me at 555-123-4567")
        assert len(findings) >= 1
        assert findings[0]["type"] == "phone"

    def test_detects_ssn(self):
        findings = scan_for_pii("SSN: 123-45-6789")
        assert len(findings) >= 1
        assert findings[0]["type"] == "ssn"

    def test_detects_api_key(self):
        findings = scan_for_pii("api_key = sk-abcdef1234567890abcdef12")
        assert len(findings) >= 1
        assert findings[0]["type"] == "api_key"

    def test_detects_ip_address(self):
        findings = scan_for_pii("Server IP is 192.168.1.1")
        assert len(findings) >= 1
        assert findings[0]["type"] == "ip_address"

    def test_clean_text_no_findings(self):
        findings = scan_for_pii("The user prefers TypeScript with explicit generics.")
        assert findings == []

    def test_empty_text(self):
        assert scan_for_pii("") == []

    def test_matches_are_truncated(self):
        findings = scan_for_pii("email is verylongemailaddress@example.com for testing")
        if findings:
            assert len(findings[0]["match"]) <= PII_MATCH_PREVIEW_CHARS


# =============================================================================
# Fixture definitions
# =============================================================================


class TestFixtures:
    def test_fixtures_have_required_fields(self):
        for mem in FIXTURE_MEMORIES:
            assert "id" in mem, f"Missing id in fixture: {mem}"
            assert "content" in mem, f"Missing content in fixture: {mem.get('id')}"
            assert "scope" in mem, f"Missing scope in fixture: {mem['id']}"
            assert "category" in mem, f"Missing category in fixture: {mem['id']}"
            assert "importance" in mem, f"Missing importance in fixture: {mem['id']}"

    def test_fixtures_have_unique_ids(self):
        ids = [mem["id"] for mem in FIXTURE_MEMORIES]
        assert len(ids) == len(set(ids)), f"Duplicate IDs found: {ids}"

    def test_all_scenario_expected_ids_exist(self):
        fixture_ids = {mem["id"] for mem in FIXTURE_MEMORIES}
        for scenario in SCENARIOS:
            if scenario.id in GENERATED_SCENARIO_IDS:
                continue
            missing = scenario.expected_memory_ids - fixture_ids
            assert not missing, f"Scenario {scenario.id} expects unknown IDs: {missing}"

    def test_generated_scenarios_declare_placeholders_only(self):
        declared = {s.id: s.expected_memory_ids for s in SCENARIOS if s.id in GENERATED_SCENARIO_IDS}
        assert set(declared) == set(GENERATED_SCENARIO_IDS)
        for scenario_id, placeholder in GENERATED_SCENARIO_IDS.items():
            assert declared[scenario_id] == {placeholder}, f"{scenario_id} declares {declared[scenario_id]}"

    def test_generated_placeholders_resolve_to_this_runs_distilled_memories(self):
        harness = EvalHarness(user_id="_eval_gen_user_", tenant_id="_eval_gen_tenant_")
        try:
            harness.seed_fixtures()
            resolved = harness.generated_expectations
            assert set(resolved) == set(GENERATED_SCENARIO_IDS)
            placeholders = set(GENERATED_SCENARIO_IDS.values())
            conn = harness._get_connection()
            for outcome, scenario_id in (
                ("success", "S12_experience_repeat_task"),
                ("failure", "S13_experience_failure_signature"),
            ):
                memory_ids = resolved[scenario_id]
                assert len(memory_ids) == 1, f"{scenario_id} resolved to {memory_ids}"
                memory_id = next(iter(memory_ids))
                assert memory_id not in placeholders, f"{scenario_id} kept the placeholder {memory_id}"
                row = conn.execute(
                    "SELECT metrics FROM memories WHERE id = ? AND tenant_id = ? AND user_id = ? "
                    "AND memory_type = 'procedural'",
                    (memory_id, harness.tenant_id, harness.user_id),
                ).fetchone()
                assert row is not None, f"{scenario_id} resolved to a missing memory {memory_id}"
                metrics = json.loads(row["metrics"] or "{}")
                assert harness.seeded_episode_ids[outcome] in (metrics.get("episode_ids") or []), (
                    f"{scenario_id} memory {memory_id} did not come from this run's {outcome} episode"
                )
                scenario = next(s for s in SCENARIOS if s.id == scenario_id)
                assert harness._resolve_expectations(scenario).expected_memory_ids == memory_ids
        finally:
            harness.close()


# =============================================================================
# Scenario definitions
# =============================================================================


class TestScenarios:
    def test_scenarios_have_required_fields(self):
        for s in SCENARIOS:
            assert s.id, "Scenario missing id"
            assert s.query, f"Scenario {s.id} missing query"
            assert s.expected_memory_ids, f"Scenario {s.id} missing expected_memory_ids"
            assert s.expectation, f"Scenario {s.id} missing expectation"

    def test_scenario_ids_are_unique(self):
        ids = [s.id for s in SCENARIOS]
        assert len(ids) == len(set(ids)), f"Duplicate scenario IDs: {ids}"


# =============================================================================
# Harness — fixture seeding
# =============================================================================


class TestHarnessSeeding:
    def test_seed_fixtures_creates_memories(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            count = harness.seed_fixtures()
            assert count > 0, "No fixtures were seeded"
            conn = harness._get_connection()
            row = conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()
            expected = len(FIXTURE_MEMORIES) + EXPERIENCE_SEED_MEMORIES
            assert row["cnt"] == expected, f"Expected {expected} memories, got {row['cnt']}"
        finally:
            harness.close()

    def test_seed_fixtures_creates_entities(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            conn = harness._get_connection()
            row = conn.execute("SELECT COUNT(*) as cnt FROM memory_entities").fetchone()
            assert row["cnt"] >= MIN_SEEDED_ENTITIES, f"Expected >= {MIN_SEEDED_ENTITIES} entities, got {row['cnt']}"
            row = conn.execute("SELECT COUNT(*) as cnt FROM memory_entity_links").fetchone()
            assert row["cnt"] >= MIN_SEEDED_LINKS, f"Expected >= {MIN_SEEDED_LINKS} links, got {row['cnt']}"
        finally:
            harness.close()

    def test_seed_fixtures_idempotent(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            harness.seed_fixtures()
            conn = harness._get_connection()
            row = conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()
            assert row["cnt"] == len(FIXTURE_MEMORIES) + EXPERIENCE_SEED_MEMORIES
        finally:
            harness.close()

    def test_seeded_experience_expectations_point_at_seeded_episodes(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            conn = harness._get_connection()
            for outcome, scenario_id in (
                ("success", "S12_experience_repeat_task"),
                ("failure", "S13_experience_failure_signature"),
            ):
                expected = harness.generated_expectations[scenario_id]
                assert len(expected) == 1, f"{scenario_id} resolved to {expected}"
                memory_id = next(iter(expected))
                row = conn.execute(
                    "SELECT memory_type, metrics, error_signature FROM memories WHERE id = ?",
                    (memory_id,),
                ).fetchone()
                assert row is not None, f"{scenario_id} expects a memory that was never written"
                assert row["memory_type"] == "procedural"
                metrics = json.loads(row["metrics"])
                assert metrics["episode_ids"] == [harness.seeded_episode_ids[outcome]]
                assert (row["error_signature"] is not None) is (outcome == "failure")
            for scenario in SCENARIOS:
                if scenario.id in GENERATED_SCENARIO_IDS:
                    assert scenario.expected_memory_ids == {GENERATED_SCENARIO_IDS[scenario.id]}
        finally:
            harness.close()

    def test_foreign_procedural_memory_cannot_satisfy_experience_scenarios(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness._apply_patches()
            conn = harness._get_connection()
            conn.execute(
                """INSERT INTO memories
                   (id, content, content_hash, scope, retention, category, user_id, bank_id, tenant_id,
                    created_at, updated_at, tags, emotional_context, metrics,
                    is_ghost, synthesized_from, importance, memory_type, is_sensitive, error_signature)
                   VALUES (?, ?, ?, 'arc', 'long_term', 'lesson', ?, 'default', ?,
                           ?, ?, '[]', '{}', ?, 0, '[]', 0.9, 'procedural', 0, ?)""",
                (
                    "decoy_procedural_failure",
                    "Unrelated failure recipe",
                    "decoy-hash",
                    harness.user_id,
                    harness.tenant_id,
                    "2024-01-01T00:00:00+00:00",
                    "2024-01-01T00:00:00+00:00",
                    json.dumps({"exp_key": "failure:decoy", "episode_ids": ["decoy_episode"], "occurrences": 1}),
                    "ConnectionRefusedError",
                ),
            )
            conn.commit()
            harness.seed_fixtures()
            resolved = {mid for ids in harness.generated_expectations.values() for mid in ids}
            assert "decoy_procedural_failure" not in resolved
            for scenario_id, memory_ids in harness.generated_expectations.items():
                assert all(m != "decoy_procedural_failure" for m in memory_ids), scenario_id
        finally:
            harness.close()

    def test_custom_user_and_tenant(self):
        harness = EvalHarness(
            db_path=":memory:",
            user_id="custom_user",
            tenant_id="custom_tenant",
        )
        try:
            harness.seed_fixtures()
            conn = harness._get_connection()
            row = conn.execute("SELECT DISTINCT user_id FROM memories").fetchone()
            assert row["user_id"] == "custom_user"
            row = conn.execute("SELECT DISTINCT tenant_id FROM memories").fetchone()
            assert row["tenant_id"] == "custom_tenant"
        finally:
            harness.close()

    def test_file_based_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            harness = EvalHarness(db_path=db_path)
            harness.seed_fixtures()
            harness.close()
            assert os.path.getsize(db_path) > 0, "DB file should contain data"
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


# =============================================================================
# Harness — scenario execution
# =============================================================================


class TestScenarioExecution:
    def test_run_scenario_returns_result(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            scenario = SCENARIOS[0]
            result = harness.run_scenario(scenario)
            assert isinstance(result, ScenarioResult)
            assert result.scenario_id == scenario.id
            assert result.query == scenario.query
            assert result.injection_payload_size >= 0
            assert result.latency_ms >= 0
            assert isinstance(result.found_memory_ids, list)
            assert isinstance(result.pii_findings, list)
        finally:
            harness.close()

    def test_run_scenario_no_error(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            for scenario in SCENARIOS:
                result = harness.run_scenario(scenario)
                assert result.error is None, f"Scenario {scenario.id} failed: {result.error}"
        finally:
            harness.close()

    def test_run_all_returns_report(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report = harness.run_all()
            assert isinstance(report, EvalReport)
            assert len(report.scenarios) == len(SCENARIOS)
            assert report.summary["total"] == len(SCENARIOS)
        finally:
            harness.close()

    def test_run_all_pii_scanning(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report = harness.run_all()
            # The fixture memories should NOT contain PII, so no findings
            total_pii = report.summary["pii_findings_total"]
            assert total_pii == 0, f"Expected 0 PII findings in clean fixtures, got {total_pii}"
        finally:
            harness.close()


# =============================================================================
# Report formatting
# =============================================================================


class TestReportFormatting:
    def test_format_text(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report = harness.run_all()
            text = report.format_text()
            assert "FORESIGHT EVALUATION HARNESS" in text
            assert report.summary["total"] > 0
            # Status characters for each scenario
            for sr in report.scenarios:
                status_char = "✓" if sr.passed else "✗"
                assert status_char in text
        finally:
            harness.close()

    def test_to_dict(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report = harness.run_all()
            d = report.to_dict()
            assert "timestamp" in d
            assert "summary" in d
            assert "scenarios" in d
            assert d["summary"]["total"] == len(SCENARIOS)
            for sd in d["scenarios"]:
                assert "scenario_id" in sd
                assert "query" in sd
                assert "passed" in sd
                assert "injection_payload_size" in sd
                assert "latency_ms" in sd
        finally:
            harness.close()

    def test_empty_report(self):
        report = EvalReport(
            timestamp="2026-01-01T00:00:00",
            scenarios=[],
            summary={"total": 0, "passed": 0, "pass_rate_pct": 0.0},
        )
        text = report.format_text()
        assert "0/0" in text
        d = report.to_dict()
        assert d["summary"]["total"] == 0


# =============================================================================
# Scenario result
# =============================================================================


class TestScenarioResult:
    def test_creation(self):
        payload_size = 500
        result = ScenarioResult(
            scenario_id="test_scenario",
            query="test query",
            passed=True,
            metrics={"total_candidates": 10},
            found_memory_ids=["mem1", "mem2"],
            missing_expected=[],
            injection_payload_size=payload_size,
            latency_ms=12.34,
            signal_counts={"keyword": 2, "tfidf_cosine": 1},
            fast_path=None,
            pii_findings=[],
        )
        assert result.passed
        assert result.scenario_id == "test_scenario"
        assert result.injection_payload_size == payload_size


# =============================================================================
# Baseline comparison
# =============================================================================


class TestBaselineComparison:
    def test_compare_baseline(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report1 = harness.run_all()
            report2 = harness.run_all()
            diff = harness.compare_baseline(report1, report2)
            assert "payload_change_pct" in diff
            assert "latency_change_pct" in diff
            assert "pass_rate_change" in diff
            assert "scenario_diffs" in diff
            assert len(diff["scenario_diffs"]) == len(SCENARIOS)
        finally:
            harness.close()

    def test_compare_baseline_scenario_status(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report1 = harness.run_all()
            report2 = harness.run_all()
            diff = harness.compare_baseline(report1, report2)
            for sd in diff["scenario_diffs"]:
                # If both runs produced same pass/fail, status is "unchanged"
                assert sd["status_change"] == "unchanged", (
                    f"Scenario {sd['scenario_id']} status changed: {sd['status_change']}"
                )
        finally:
            harness.close()


# =============================================================================
# Edge cases
# =============================================================================


class TestEdgeCases:
    def test_harness_cleanup(self):
        """Calling close() should free resources."""
        harness = EvalHarness(db_path=":memory:")
        harness.seed_fixtures()
        harness.close()
        assert harness._conn is None

    def test_run_all_multiple_times(self):
        """Running the harness multiple times should produce consistent results."""
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report1 = harness.run_all()
            report2 = harness.run_all()
            # Same fixtures, same queries — pass rates should be identical
            assert report1.summary["pass_rate_pct"] == report2.summary["pass_rate_pct"]
        finally:
            harness.close()

    def test_stale_vs_current_scoring(self):
        """S3: current_auth_approach should rank higher than stale_auth_approach."""
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            s3 = next(s for s in SCENARIOS if s.id == "S3_stale_vs_current")
            result = harness.run_scenario(s3)
            # Both expected memories should appear
            assert "current_auth_approach" in result.found_memory_ids, (
                f"current_auth_approach not found in {result.found_memory_ids}"
            )
            assert "stale_auth_approach" in result.found_memory_ids, (
                f"stale_auth_approach not found in {result.found_memory_ids}"
            )
            # Current should rank higher (lower index = higher rank)
            idx_current = result.found_memory_ids.index("current_auth_approach")
            idx_stale = result.found_memory_ids.index("stale_auth_approach")
            assert idx_current < idx_stale, "current(idx_current) should rank higher than stale(idx_stale)"
        finally:
            harness.close()


# =============================================================================
# CLI entry point
# =============================================================================


class TestRunEval:
    def test_run_eval_returns_report(self):

        report = run_eval(EvalRunOptions(db_path=":memory:", budget_chars=2000))
        assert isinstance(report, EvalReport)
        assert len(report.scenarios) == len(SCENARIOS)
        assert report.summary["total"] == len(SCENARIOS)
        assert report.summary["pass_rate_pct"] >= 0

    def test_run_eval_writes_report_file(self):

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            report_path = f.name
        try:
            run_eval(
                EvalRunOptions(
                    db_path=":memory:",
                    report_path=report_path,
                    budget_chars=2000,
                )
            )
            assert os.path.exists(report_path)
            with open(report_path) as f:
                data = json.load(f)
            assert "summary" in data
            assert "scenarios" in data
            assert data["summary"]["total"] == len(SCENARIOS)
        finally:
            if os.path.exists(report_path):
                os.unlink(report_path)

    def test_cli_eval_command_uses_scenario_result_fields(self, monkeypatch):

        scenario = ScenarioResult(
            scenario_id="S1_preference",
            query="What coding style does the user prefer for TypeScript?",
            passed=True,
            metrics={},
            found_memory_ids=["pref_concise_typescript"],
            missing_expected=[],
            injection_payload_size=321,
            latency_ms=9.5,
            signal_counts={"keyword": 1},
            fast_path=None,
            pii_findings=[],
        )
        report = EvalReport(
            timestamp="2026-01-01T00:00:00Z",
            scenarios=[scenario],
            summary={"passed": 1, "total": 1, "pass_rate_pct": 100.0},
        )

        done_calls: list[str] = []
        stderr_calls: list[str] = []
        info_calls: list[str] = []

        monkeypatch.setattr(eval_harness_module, "run_eval", lambda *_, **__: report)
        monkeypatch.setattr(eval_cmd.out, "get_settings", lambda: SimpleNamespace(mode="human"))
        monkeypatch.setattr(eval_cmd.out, "done", done_calls.append)
        monkeypatch.setattr(eval_cmd.out, "stderr", lambda message, **_: stderr_calls.append(message))
        monkeypatch.setattr(eval_cmd.out, "info", info_calls.append)

        eval_cmd.run(
            eval_cmd.EvalRunParams(
                db_path=None,
                report="/tmp/eval.json",
                budget=1500,
                json_output=False,
                compare=None,
                save_baseline="/tmp/baseline.json",
            )
        )

        assert done_calls == ["1/1 scenarios passed (100.0%)"]
        assert len(stderr_calls) == 1
        assert "payload=321 chars" in stderr_calls[0]
        assert "latency=9.5ms" in stderr_calls[0]
        assert "pii=0" in stderr_calls[0]
        assert info_calls == [
            "Maintenance eval report written to /tmp/eval.json",
            "Baseline report written to /tmp/baseline.json",
        ]


# =============================================================================
# Report save/load persistence
# =============================================================================


class TestReportPersistence:
    def test_save_and_load(self):
        harness = EvalHarness(db_path=":memory:")
        try:
            harness.seed_fixtures()
            report1 = harness.run_all()
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
                path = f.name
            try:
                report1.save(path)
                report2 = EvalReport.load(path)
                assert report2.harness_version == report1.harness_version
                assert len(report2.scenarios) == len(report1.scenarios)
                assert report2.summary["total"] == report1.summary["total"]
                for s1, s2 in zip(report1.scenarios, report2.scenarios, strict=True):
                    assert s1.scenario_id == s2.scenario_id
                    assert s1.passed == s2.passed
                    assert s1.injection_payload_size == s2.injection_payload_size
                    assert s1.latency_ms == s2.latency_ms
            finally:
                if os.path.exists(path):
                    os.unlink(path)
        finally:
            harness.close()

    def test_compare_via_cli(self):

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            baseline_path = f.name
        try:
            # Generate baseline
            run_eval(EvalRunOptions(db_path=":memory:", save_baseline=baseline_path, budget_chars=2000))
            assert os.path.exists(baseline_path)

            # Run comparison against baseline
            report = run_eval(
                EvalRunOptions(
                    db_path=":memory:",
                    compare_path=baseline_path,
                    budget_chars=2000,
                )
            )
            assert isinstance(report, EvalReport)
            assert report.summary["total"] == len(SCENARIOS)
            assert report.baseline_diff is not None
            assert "scenario_diffs" in report.baseline_diff
        finally:
            if os.path.exists(baseline_path):
                os.unlink(baseline_path)

    def test_report_file_includes_baseline_diff(self):

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as baseline_file:
            baseline_path = baseline_file.name
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as report_file:
            report_path = report_file.name
        try:
            run_eval(EvalRunOptions(db_path=":memory:", save_baseline=baseline_path, budget_chars=2000))
            run_eval(
                EvalRunOptions(
                    db_path=":memory:",
                    compare_path=baseline_path,
                    report_path=report_path,
                    budget_chars=2000,
                )
            )

            persisted_report = EvalReport.load(report_path)
            assert persisted_report.baseline_diff is not None
            assert "payload_change_pct" in persisted_report.baseline_diff
        finally:
            if os.path.exists(baseline_path):
                os.unlink(baseline_path)
            if os.path.exists(report_path):
                os.unlink(report_path)

    def test_compare_missing_baseline_does_not_crash(self):

        report = run_eval(
            EvalRunOptions(
                db_path=":memory:",
                compare_path="/nonexistent/baseline.json",
                budget_chars=2000,
            )
        )
        assert isinstance(report, EvalReport)

    def test_invalid_json_baseline_does_not_crash(self):

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("not json")
            bad_path = f.name
        try:
            report = run_eval(EvalRunOptions(db_path=":memory:", compare_path=bad_path, budget_chars=2000))
            assert isinstance(report, EvalReport)
        finally:
            if os.path.exists(bad_path):
                os.unlink(bad_path)


class TestHarnessTeardown:
    """A setup that raises halfway must not leave the process patched."""

    def test_a_setup_that_raises_after_patching_still_unpatches(self, monkeypatch):
        before_user = get_current_user_id()
        harness = EvalHarness(db_path=":memory:")
        original_db_path = eval_harness_module.config_module.DB_PATH

        # No process-wide backend to park, which is the shape that used to slip
        # through the teardown guard: nothing to restore but the patches. Both
        # accessors are faked so the harness cannot touch the real backend a
        # neighbouring test installed.
        parked: list[object] = []
        monkeypatch.setattr(eval_harness_module, "get_backend", lambda: None)
        monkeypatch.setattr(eval_harness_module, "set_backend", parked.append)

        def _interrupt(_tenant_id: str) -> None:
            raise RuntimeError("setup interrupted")

        monkeypatch.setattr(eval_harness_module, "set_current_account_id", _interrupt)

        try:
            with pytest.raises(RuntimeError, match="setup interrupted"):
                harness._apply_patches()
        finally:
            harness.close()

        assert original_db_path == eval_harness_module.config_module.DB_PATH
        assert get_current_user_id() == before_user
        assert harness._patches_applied is False
        assert harness._monkeypatches == []

    def test_a_retry_after_a_failed_setup_does_not_inherit_partial_state(self, monkeypatch, tmp_path):
        harness = EvalHarness(db_path=str(tmp_path / "retry.db"))
        original_db_path = eval_harness_module.config_module.DB_PATH

        attempts = {"count": 0}
        real_set_current_account_id = eval_harness_module.set_current_account_id

        def _interrupt_once(tenant_id: str) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("setup interrupted")
            real_set_current_account_id(tenant_id)

        monkeypatch.setattr(eval_harness_module, "set_current_account_id", _interrupt_once)

        try:
            with pytest.raises(RuntimeError, match="setup interrupted"):
                harness.seed_fixtures()

            restored_db_path = eval_harness_module.config_module.DB_PATH
            assert restored_db_path == original_db_path
            assert harness._monkeypatches == []
            assert harness._patches_applied is False

            assert harness.seed_fixtures() > 0
        finally:
            harness.close()

        restored_db_path = eval_harness_module.config_module.DB_PATH
        assert restored_db_path == original_db_path
        assert harness._monkeypatches == []
        assert harness._patches_applied is False

    def test_a_failed_setup_rollback_keeps_the_callers_scope(self, monkeypatch, tmp_path):
        harness = EvalHarness(db_path=str(tmp_path / "scope.db"))
        set_current_user_id("caller-user")
        set_current_account_id("caller-account")

        attempts = {"count": 0}
        real_set_current_account_id = eval_harness_module.set_current_account_id

        def _interrupt_once(tenant_id: str) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("setup interrupted")
            real_set_current_account_id(tenant_id)

        monkeypatch.setattr(eval_harness_module, "set_current_account_id", _interrupt_once)

        try:
            with pytest.raises(RuntimeError, match="setup interrupted"):
                harness.seed_fixtures()
            assert get_current_user_id() == "caller-user"
            assert get_current_account_id() == "caller-account"
        finally:
            harness.close()
            monkeypatch.undo()
            reset_tenant_context()
