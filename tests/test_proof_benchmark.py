"""Tests for the Foresight Production Value & Proof Benchmark Suite."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from foresight.proof_benchmark import (
    ProductionProofReport,
    ProofBenchmarkRunner,
    ScenarioProofResult,
    run_proof_benchmark,
)


def test_scenario_proof_result_dataclass():
    res = ScenarioProofResult(
        scenario_id="test_01",
        name="Test Continuity",
        dimension="Continuity",
        description="Testing continuity",
        passed=True,
        amnesia_score=0.0,
        foresight_score=1.0,
        improvement_factor=10.0,
        latency_ms=4.2,
        details={"key": "val"},
        key_takeaway="Proved continuity",
    )
    assert res.passed is True
    assert res.improvement_factor == 10.0
    assert res.latency_ms == 4.2


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
        composite_production_score=98.5,
        estimated_hours_saved_monthly=8.5,
        token_efficiency_ratio=3.8,
        scenarios=scenarios,
        surface_readiness={"OpenCode": True, "Claude Code": True},
    )

    d = report.to_dict()
    assert d["composite_production_score"] == 98.5
    assert len(d["scenarios"]) == 2

    text = report.format_text()
    assert "FORESIGHT PRODUCTION VALUE & PROOF BENCHMARK" in text
    assert "Scenario 1" in text
    assert "OpenCode" in text


def test_surface_readiness_check():
    runner = ProofBenchmarkRunner()
    surfaces = runner._check_surface_readiness()
    assert isinstance(surfaces, dict)
    assert len(surfaces) >= 7
