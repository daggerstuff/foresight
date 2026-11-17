"""Eval command: run the evaluation harness and generate a report."""

from __future__ import annotations

import argparse

from pydantic import BaseModel, ConfigDict, Field

from foresight import eval_harness
from foresight_cli.parser import CommandSpec
from foresight_cli.utils import output as out


class EvalRunParams(BaseModel):
    """Arguments for :func:`run`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    db_path: str | None = Field(default=None, description="Path to temp database (default: auto tempfile)")
    report: str | None = Field(default=None, description="Write JSON report to file")
    budget: int = Field(default=2000, description="Character budget for injection payloads")
    compare: str | None = Field(default=None, description="Path to a baseline JSON report to diff against")
    save_baseline: str | None = Field(default=None, description="Save the report as a baseline JSON at this path")
    json_output: bool = Field(default=False, description="Output report as JSON")


def run(params: EvalRunParams) -> None:
    """Run the full evaluation harness and print a summary report.

    Seeds fixture memories, runs all 7 evaluation scenarios against
    inject_context and get_relevant_memories, then prints a detailed
    report with metrics on payload size, latency, retrieval quality,
    and PII safety.
    """
    report_obj = eval_harness.run_eval(
        eval_harness.EvalRunOptions(
            db_path=params.db_path,
            report_path=params.report,
            budget_chars=params.budget,
            compare_path=params.compare,
            save_baseline=params.save_baseline,
            json_output=params.json_output or out.get_settings().mode == "json",
        )
    )

    passed = report_obj.summary["passed"]
    total = report_obj.summary["total"]
    pct = report_obj.summary["pass_rate_pct"]

    if out.get_settings().mode == "json":
        out.print_json(report_obj.to_dict())
    elif out.get_settings().mode == "agent":
        out.data(
            "maintenance_eval_result",
            {
                "passed": passed,
                "total": total,
                "pass_rate_pct": pct,
            },
        )
    else:
        out.done(f"{passed}/{total} scenarios passed ({pct:.1f}%)")
        for sr in report_obj.scenarios:
            status = "✓" if sr.passed else "✗"
            icon = "green" if sr.passed else "red"
            payload = f"payload={sr.injection_payload_size} chars"
            latency = f"latency={sr.latency_ms:.1f}ms"
            findings = f"pii={len(sr.pii_findings)}"
            out.stderr(
                f"  [{icon}]{status}[/] {sr.scenario_id}: {payload}, {latency}, {findings}",
                style=icon,
            )
        if report_obj.baseline_diff is not None:
            baseline_diff = report_obj.baseline_diff
            out.info("Baseline comparison:")
            out.stderr(
                "  payload="
                f"{_format_pct_change(baseline_diff.get('payload_change_pct'))}, "
                "latency="
                f"{_format_pct_change(baseline_diff.get('latency_change_pct'))}, "
                f"pass_rate={baseline_diff.get('pass_rate_change', 0):+.1f}%, "
                f"pii={baseline_diff.get('pii_change', 0):+d}"
            )
            for scenario_diff in baseline_diff.get("scenario_diffs", []):
                out.stderr(
                    "  "
                    f"{scenario_diff['scenario_id']}: "
                    f"payload={scenario_diff.get('payload_change', 0):+d} chars, "
                    f"latency={scenario_diff.get('latency_change', 0):+.2f}ms, "
                    f"status={scenario_diff.get('status_change', 'unchanged')}"
                )
        if params.report:
            out.info(f"Maintenance eval report written to {params.report}")
        if params.save_baseline:
            out.info(f"Baseline report written to {params.save_baseline}")


def _format_pct_change(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.1f}%"


def _run_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db-path", default=None, help="Path to temp database (default: auto tempfile)")
    parser.add_argument("--report", "-r", default=None, help="Write JSON report to file")
    parser.add_argument(
        "--budget",
        "-b",
        type=int,
        default=2000,
        help="Character budget for injection payloads",
    )
    parser.add_argument(
        "--compare",
        "-c",
        default=None,
        help="Path to a baseline JSON report to diff against",
    )
    parser.add_argument(
        "--save-baseline",
        default=None,
        help="Save the report as a baseline JSON at this path",
    )
    parser.add_argument("--json", "-j", dest="json_output", action="store_true", help="Output report as JSON")


COMMANDS = {"run": CommandSpec(run, EvalRunParams, _run_parser)}
