"""Tests for the plugin conformance harness (FOR-71)."""

from __future__ import annotations

import json
from pathlib import Path

from foresight import plugin_conformance as conformance

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"


def _report_for(reports, plugin_id: str):
    return next(r for r in reports if r.plugin_id == plugin_id)


def _check(report, name: str):
    return next(c for c in report.checks if c.name == name)


class TestRunConformance:
    def test_all_manifest_plugins_reported(self):
        reports = conformance.run_conformance(PLUGINS_DIR)
        ids = {r.plugin_id for r in reports}
        assert ids == {"claude", "hermes", "mastracode"}

    def test_no_failures_on_repo_plugins(self):
        reports = conformance.run_conformance(PLUGINS_DIR)
        assert all(r.status != conformance.FAIL for r in reports)

    def test_claude_hook_functional_and_fail_safe(self):
        reports = conformance.run_conformance(PLUGINS_DIR)
        claude = _report_for(reports, "claude")
        functional = _check(claude, "functional-hook")
        assert functional.status == conformance.PASS
        fail_safe = _check(claude, "fail-safe")
        assert fail_safe.status == conformance.PASS

    def test_non_hook_plugins_skip_functional(self):
        reports = conformance.run_conformance(PLUGINS_DIR)
        for plugin_id in ("hermes", "mastracode"):
            report = _report_for(reports, plugin_id)
            assert _check(report, "functional-hook").status == conformance.SKIP
            assert _check(report, "manifest-valid").status == conformance.PASS
            assert _check(report, "artifacts-exist").status == conformance.PASS

    def test_filter_limits_to_one_plugin(self):
        reports = conformance.run_conformance(PLUGINS_DIR, only="hermes")
        assert [r.plugin_id for r in reports] == ["hermes"]


class TestCheckPlugin:
    def test_missing_artifact_fails(self, tmp_path: Path):
        plugin_dir = tmp_path / "broken"
        plugin_dir.mkdir()
        (plugin_dir / "foresight.plugin.json").write_text(
            json.dumps(
                {
                    "specVersion": 1,
                    "id": "broken",
                    "name": "Broken",
                    "version": "0.0.1",
                    "targets": ["claude-code"],
                    "surfaces": ["hooks"],
                    "requires": {"foresight": ">=0.19.0", "capabilities": []},
                    "artifacts": [{"type": "hook", "path": "nope.py"}],
                }
            )
        )
        report = conformance.check_plugin(plugin_dir)
        assert report.status == conformance.FAIL
        assert _check(report, "artifacts-exist").status == conformance.FAIL
        assert "nope.py" in _check(report, "artifacts-exist").detail

    def test_invalid_manifest_fails_fast(self, tmp_path: Path):
        plugin_dir = tmp_path / "bad"
        plugin_dir.mkdir()
        (plugin_dir / "foresight.plugin.json").write_text("{not json")
        report = conformance.check_plugin(plugin_dir)
        assert report.status == conformance.FAIL
        assert report.checks[0].name == "manifest-valid"
        assert report.checks[0].status == conformance.FAIL

    def test_main_exit_codes(self, tmp_path: Path, capsys):
        assert conformance.main([str(PLUGINS_DIR)]) == 0
        out = capsys.readouterr().out
        assert "claude: PASS" in out
        assert conformance.main([str(tmp_path)]) == 1


class TestCliJson:
    def test_json_report_shape(self, capsys):
        code = conformance.main([str(PLUGINS_DIR), "--json", "--filter", "claude"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["plugin"] == "claude"
        assert payload[0]["status"] == "pass"
        names = {c["name"] for c in payload[0]["checks"]}
        assert {"manifest-valid", "artifacts-exist", "functional-hook", "fail-safe"} <= names
