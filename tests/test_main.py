from __future__ import annotations

import json
import tomllib
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

import foresight
import foresight.__main__ as main_module
from foresight_cli.cli import main as cli_main
from foresight_cli.commands import system as system_commands
from foresight_cli.utils import output as out


def test_main_health_prints_status(capsys):

    with (
        patch.object(main_module.sys, "argv", ["foresight-server", "--health"]),
        patch.object(main_module, "init_db"),
        patch.object(main_module, "_initialize_backend"),
        patch.object(main_module, "get_system_status", return_value='{"status":"healthy"}'),
    ):
        main_module.main()

    assert capsys.readouterr().out.strip() == '{"status":"healthy"}'


def test_main_help_prints_usage(capsys):

    with patch.object(main_module.sys, "argv", ["foresight-server", "--help"]):
        main_module.main()

    assert "Usage: foresight-server" in capsys.readouterr().out


def test_main_version_prints_version(capsys):

    with patch.object(main_module.sys, "argv", ["foresight-server", "--version"]):
        main_module.main()

    assert capsys.readouterr().out.strip() == f"foresight {foresight.__version__}"


def test_cli_version_prints_version(capsys):

    with pytest.raises(SystemExit) as exc:
        cli_main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"foresight {foresight.__version__}"


def test_version_matches_pyproject():

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]

    assert foresight.__version__ == declared


def test_status_human_formats_payload_budget_weights_and_injection_health():
    result = {
        "status": "healthy",
        "database": "/tmp/test.db",
        "bank_id": "default",
        "user_id": "test-user",
        "memory_count": 12,
        "crisis_signals": 0,
        "stale_count": 2,
        "payload_budget": {
            "min_lane_chars": 40,
            "default_lane_weights": {
                "blocks": 0.15,
                "dynamic": 0.15,
                "memories": 0.5,
                "safety": 0.1,
                "static": 0.1,
            },
        },
        "injection_health": {
            "total_runs": 4,
            "runs_24h": 3,
            "avg_latency_ms": 21.5,
            "avg_memories_returned": 5.25,
            "avg_memories_fetched": 7.5,
            "fast_path_rate_pct": 75.0,
            "last_run_at": "2026-06-23T00:00:00+00:00",
        },
    }
    stderr_buffer = StringIO()

    with (
        patch("foresight_cli.commands.system.init_db"),
        patch("foresight_cli.commands.system._initialize_backend"),
        patch("foresight_cli.commands.system.get_system_status", return_value=result),
        patch.object(
            out,
            "_stderr_console",
            lambda: Console(file=stderr_buffer, force_terminal=False, color_system=None),
        ),
    ):
        out.configure(mode="human", color=False, pipe_safe=False, verbose=False)
        system_commands.status(system_commands.SystemStatusParams(user_id=None))

    rendered = stderr_buffer.getvalue()
    assert "Payload Budget" in rendered
    assert "blocks: 15%" in rendered
    assert "memories: 50%" in rendered
    assert "Injection Health" in rendered
    assert "Total Runs: 4" in rendered
    assert "Fast Path Rate: 75%" in rendered


def test_status_json_mode_emits_scriptable_status_payload(capsys):
    memories_lane_weight = 0.5
    total_runs = 4
    result = {
        "status": "healthy",
        "memory_count": 12,
        "stale_count": 2,
        "payload_budget": {
            "min_lane_chars": 40,
            "default_lane_weights": {"memories": memories_lane_weight},
        },
        "injection_health": {
            "total_runs": total_runs,
            "fast_path_rate_pct": 75.0,
        },
    }

    with (
        patch("foresight_cli.commands.system.init_db"),
        patch("foresight_cli.commands.system._initialize_backend"),
        patch("foresight_cli.commands.system.get_system_status", return_value=json.dumps(result)),
    ):
        exit_code = cli_main(["--output", "json", "status"])

    assert exit_code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["payload_budget"]["default_lane_weights"]["memories"] == memories_lane_weight
    assert parsed["injection_health"]["total_runs"] == total_runs
