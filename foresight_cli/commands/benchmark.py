"""Benchmark command: run the Foresight Production Value & Proof Benchmark Suite."""

from __future__ import annotations

import argparse

from pydantic import BaseModel, ConfigDict, Field

from foresight.proof_benchmark import run_proof_benchmark
from foresight_cli.parser import CommandSpec
from foresight_cli.utils import output as out


class BenchmarkRunParams(BaseModel):
    """Arguments for :func:`run`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    report: str | None = Field(default=None, description="Write JSON benchmark report to file")
    json_output: bool = Field(default=False, description="Output benchmark report as raw JSON")


def run(params: BenchmarkRunParams) -> None:
    """Run the Foresight Production Value & Proof Benchmark Suite.

    Evaluates:
    1. Cross-Session Workflow Constraint Adherence
    2. Architecture & Infrastructure Decision Memory
    3. Technical Fact & Bug Workaround Retention
    4. Zero-Touch Multi-Turn Self-Noting
    5. Natural Language Phrase Trigger Proactive Capture
    6. Automated Git Commit Context Enrichment
    7. FastMCP 4.0 Stateless Single-Roundtrip Retrieval Latency
    8. Autonomous Self-Curation & Context Distillation
    9. Multi-Surface Integration Matrix (8+ developer tool surfaces)
    """
    report_obj = run_proof_benchmark(
        json_output=params.json_output or out.get_settings().mode == "json",
        report_path=params.report,
    )

    if params.json_output or out.get_settings().mode == "json":
        out.print_json(report_obj.to_dict())
    elif out.get_settings().mode == "agent":
        out.data(
            "production_benchmark_result",
            {
                "composite_production_score": report_obj.composite_production_score,
                "passed": report_obj.passed_scenarios,
                "total": report_obj.total_scenarios,
                "success_rate_pct": report_obj.success_rate_pct,
                "overall_lift_pct": report_obj.overall_lift_pct,
                "estimated_hours_saved_monthly": report_obj.estimated_hours_saved_monthly,
            },
        )
    else:
        report_obj.print_rich()
        if params.report:
            out.info(f"Benchmark proof report written to {params.report}")


def _run_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--report", "-r", default=None, help="Write JSON benchmark report to file")
    parser.add_argument(
        "--json",
        "-j",
        dest="json_output",
        action="store_true",
        help="Output benchmark report as raw JSON",
    )


COMMANDS = {"run": CommandSpec(run, BenchmarkRunParams, _run_parser)}
