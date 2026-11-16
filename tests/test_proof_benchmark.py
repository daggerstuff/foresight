"""Tests for the Foresight Production Value & Proof Benchmark Suite."""

from __future__ import annotations

from foresight.proof_benchmark import (
    ProductionProofReport,
    ProofBenchmarkRunner,
    ScenarioProofResult,
)
from foresight.server import init_db

# Every surface Foresight claims to support should appear in the readiness map.
MIN_SUPPORTED_SURFACES = 7

# Floor the token-compression scenario must clear on a real corpus.
MIN_EXPECTED_TOKEN_REDUCTION_PCT = 75.0


def test_scenario_proof_result_dataclass():
    improvement_factor = 10.0
    latency_ms = 4.2
    res = ScenarioProofResult(
        scenario_id="test_01",
        name="Test Continuity",
        dimension="Continuity",
        description="Testing continuity",
        passed=True,
        amnesia_score=0.0,
        foresight_score=1.0,
        improvement_factor=improvement_factor,
        latency_ms=latency_ms,
        details={"key": "val"},
        key_takeaway="Proved continuity",
    )
    assert res.passed is True
    assert res.improvement_factor == improvement_factor
    assert res.latency_ms == latency_ms


def test_production_proof_report_methods():
    scenarios = [
        ScenarioProofResult(
            scenario_id="s1",
            name="Scenario 1",
            dimension="Continuity",
            description="Desc 1",
            passed=True,
            amnesia_score=0.0,
            foresight_score=1.0,
            improvement_factor=10.0,
            latency_ms=5.0,
            key_takeaway="Proof 1",
        ),
        ScenarioProofResult(
            scenario_id="s2",
            name="Scenario 2",
            dimension="Performance",
            description="Desc 2",
            passed=True,
            amnesia_score=0.0,
            foresight_score=1.0,
            improvement_factor=10.0,
            latency_ms=6.0,
            key_takeaway="Proof 2",
        ),
    ]

    composite_production_score = 98.5
    token_reduction_pct = 92.5
    avg_tokens_saved_per_turn = 3200
    monthly_token_cost_savings_usd = 42.24
    report = ProductionProofReport(
        timestamp="2026-08-22T20:00:00Z",
        total_scenarios=2,
        passed_scenarios=2,
        success_rate_pct=100.0,
        avg_latency_ms=5.5,
        p95_latency_ms=6.0,
        amnesia_avg_score=0.0,
        foresight_avg_score=1.0,
        overall_lift_pct=10000.0,
        composite_production_score=composite_production_score,
        estimated_hours_saved_monthly=8.5,
        token_efficiency_ratio=3.8,
        token_reduction_pct=token_reduction_pct,
        avg_tokens_saved_per_turn=avg_tokens_saved_per_turn,
        estimated_monthly_token_cost_savings_usd=monthly_token_cost_savings_usd,
        scenarios=scenarios,
        surface_readiness={"OpenCode": True, "Claude Code": True},
    )

    d = report.to_dict()
    assert d["composite_production_score"] == composite_production_score
    assert d["token_reduction_pct"] == token_reduction_pct
    assert d["avg_tokens_saved_per_turn"] == avg_tokens_saved_per_turn
    assert d["estimated_monthly_token_cost_savings_usd"] == monthly_token_cost_savings_usd
    assert len(d["scenarios"]) == len(scenarios)

    text = report.format_text()
    assert "FORESIGHT PRODUCTION VALUE & PROOF BENCHMARK" in text
    assert "Scenario 1" in text
    assert "OpenCode" in text
    assert "92.5%" in text


def test_surface_readiness_check():
    runner = ProofBenchmarkRunner()
    surfaces = runner._check_surface_readiness()
    assert isinstance(surfaces, dict)
    assert len(surfaces) >= MIN_SUPPORTED_SURFACES


def test_token_savings_scenario():

    init_db()
    runner = ProofBenchmarkRunner(user_id="test_token_bench", tenant_id="test_tenant")
    scenario = runner._test_token_savings_and_context_compression()
    assert scenario.passed is True
    assert scenario.dimension == "Cost & Efficiency"
    assert scenario.details["token_reduction_pct"] >= MIN_EXPECTED_TOKEN_REDUCTION_PCT
    assert scenario.details["tokens_saved_per_turn"] > 0
    assert scenario.details["baseline_tokens"] > scenario.details["foresight_injected_tokens"]
