"""Benchmark command: run the Foresight Production Value & Proof Benchmark Suite."""

from __future__ import annotations

import typer

from foresight.proof_benchmark import run_proof_benchmark
from foresight_cli.utils import output as out

app = typer.Typer(help="Run the Foresight Production Value & Proof Benchmark Suite.")


@app.command()
def run(
    report: str | None = typer.Option(None, "--report", "-r", help="Write JSON benchmark report to file"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output benchmark report as raw JSON"),
):
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
        json_output=json_output or out.get_settings().mode == "json",
        report_path=report,
    )

    if json_output or out.get_settings().mode == "json":
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
        if report:
            out.info(f"Benchmark proof report written to {report}")
