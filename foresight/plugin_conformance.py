"""Plugin conformance harness (FOR-71).

Proves each plugin in ``plugins/`` actually works rather than merely
containing the right files:

* contract checks — manifest parses against the specVersion-1 contract and
  every declared artifact path exists;
* SDK adoption — informational check for use of the shared Python/TS SDKs;
* functional hook probe — runnable Python hook artifacts are executed in a
  subprocess against an in-process stub Foresight server. The probe asserts
  the benefit marker (``[FORESIGHT CONTINUITY CONTEXT]``) reaches stdout and
  that the plugin really called ``/ui/api/inject``;
* fail-safe probe — the same hook is re-run against an unreachable server
  and must exit 0 without a traceback, proving a down Foresight never breaks
  the host agent session.

Plugins without a runnable hook surface (provider packages, TS extensions)
report ``skip`` for the functional checks — their conformance is covered by
their own test suites until host-fixture agents land (FOR-72+).

CLI::

    python -m foresight.plugin_conformance [plugins_dir] [--json] [--filter ID]

Exit status is 1 when any check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

from foresight import plugin_manifest

PASS = "pass"
FAIL = "fail"
SKIP = "skip"

HOOK_STDIN = json.dumps(
    {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "conformance-probe",
        "prompt": "conformance probe: recall my preferences",
    }
)
BENEFIT_MARKER = "[FORESIGHT CONTINUITY CONTEXT]"
STUB_FORMATTED = "## Relevant memories\n- prefers concise answers"

_SDK_MARKERS = ("foresight_plugin_sdk", "@foresight/plugin-sdk")


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""


@dataclass
class PluginReport:
    plugin_id: str
    plugin_dir: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(c.status == FAIL for c in self.checks):
            return FAIL
        if any(c.status == SKIP for c in self.checks):
            return SKIP
        return PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin_id,
            "dir": self.plugin_dir,
            "status": self.status,
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in self.checks],
        }


class _StubHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[str]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).requests.append(self.path)
        if self.path == "/ui/api/inject":
            payload: dict[str, Any] = {
                "formatted": STUB_FORMATTED,
                "top_score": 0.5,
                "static_and_safety": "",
            }
        else:
            payload = {}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:
        pass


class _StubServer:
    """In-process stub Foresight server for functional probes."""

    def __init__(self) -> None:
        self._handler: type[_StubHandler] = type(
            "ConformanceStubHandler",
            (_StubHandler,),
            {"requests": []},
        )
        self._server = HTTPServer(("127.0.0.1", 0), self._handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    @property
    def requests(self) -> list[str]:
        return list(self._handler.requests)

    def __enter__(self) -> _StubServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)


def _check_manifest(plugin_dir: Path) -> tuple[plugin_manifest.PluginManifest | None, CheckResult]:
    manifest_path = plugin_dir / plugin_manifest.PLUGIN_MANIFEST_FILENAME
    try:
        manifest = plugin_manifest.load_plugin_manifest(manifest_path)
    except Exception as exc:
        return None, CheckResult("manifest-valid", FAIL, f"{manifest_path.name}: {exc}")
    return manifest, CheckResult("manifest-valid", PASS, f"specVersion {manifest.spec_version}")


def _check_artifacts(plugin_dir: Path, manifest: plugin_manifest.PluginManifest) -> CheckResult:
    missing = [a.path for a in manifest.artifacts if not (plugin_dir / a.path).exists()]
    if missing:
        return CheckResult("artifacts-exist", FAIL, f"missing: {', '.join(missing)}")
    return CheckResult("artifacts-exist", PASS, f"{len(manifest.artifacts)} artifacts")


def _check_sdk_adoption(plugin_dir: Path, manifest: plugin_manifest.PluginManifest) -> CheckResult:
    haystacks: list[str] = []
    for artifact in manifest.artifacts:
        path = plugin_dir / artifact.path
        if path.is_file() and path.suffix in {".py", ".ts", ".js", ".json", ".sh"}:
            try:
                haystacks.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    if any(marker in text for text in haystacks for marker in _SDK_MARKERS):
        return CheckResult("sdk-adoption", PASS, "uses shared plugin SDK")
    return CheckResult("sdk-adoption", SKIP, "no shared SDK import detected")


def _hook_artifacts(plugin_dir: Path, manifest: plugin_manifest.PluginManifest) -> list[Path]:
    return [plugin_dir / a.path for a in manifest.artifacts if a.path.endswith(".py") and "hook" in a.path.lower()]


def _run_hook(
    hook: Path,
    foresight_url: str,
    work_dir: Path,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["FORESIGHT_MCP_URL"] = foresight_url
    env["XDG_CACHE_HOME"] = str(work_dir / "cache")
    env["XDG_CONFIG_HOME"] = str(work_dir / "config")
    for key in ("FORESIGHT_API_KEY", "FORESIGHT_IDENTITY", "FORESIGHT_USER_ID", "FORESIGHT_HOOK_MODE"):
        env.pop(key, None)
    return subprocess.run(
        check=False,
        args=[sys.executable, str(hook), "prompt"],
        input=HOOK_STDIN,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(work_dir),
    )


def _probe_hooks(plugin_dir: Path, manifest: plugin_manifest.PluginManifest) -> list[CheckResult]:
    hooks = _hook_artifacts(plugin_dir, manifest)
    if not hooks:
        return [
            CheckResult(
                "functional-hook",
                SKIP,
                "no runnable Python hook artifact; covered by host-fixture agents (FOR-72+)",
            )
        ]
    results: list[CheckResult] = []
    with tempfile.TemporaryDirectory(prefix="foresight-conformance-") as tmp, _StubServer() as stub:
        work_dir = Path(tmp)
        for hook in hooks:
            rel = hook.relative_to(plugin_dir)
            try:
                proc = _run_hook(hook, stub.base_url, work_dir)
            except subprocess.TimeoutExpired:
                results.append(CheckResult("functional-hook", FAIL, f"{rel}: timed out"))
                continue
            if proc.returncode != 0:
                results.append(
                    CheckResult("functional-hook", FAIL, f"{rel}: exit {proc.returncode}: {proc.stderr[:200]}")
                )
                continue
            saw_inject = "/ui/api/inject" in stub.requests
            has_marker = BENEFIT_MARKER in proc.stdout
            if saw_inject and has_marker:
                results.append(CheckResult("functional-hook", PASS, f"{rel}: inject called, benefit marker emitted"))
            else:
                results.append(
                    CheckResult(
                        "functional-hook",
                        FAIL,
                        f"{rel}: inject_called={saw_inject} marker={has_marker} stdout={proc.stdout[:120]!r}",
                    )
                )

            dead = _run_hook(hook, "http://127.0.0.1:1", work_dir)
            if dead.returncode == 0 and "Traceback" not in dead.stderr:
                results.append(CheckResult("fail-safe", PASS, f"{rel}: server-down exits cleanly"))
            else:
                results.append(
                    CheckResult(
                        "fail-safe",
                        FAIL,
                        f"{rel}: exit {dead.returncode} stderr={dead.stderr[:200]!r}",
                    )
                )
    return results


def check_plugin(plugin_dir: Path) -> PluginReport:
    plugin_dir = plugin_dir.resolve()
    report = PluginReport(plugin_id=plugin_dir.name, plugin_dir=str(plugin_dir))
    manifest, result = _check_manifest(plugin_dir)
    report.checks.append(result)
    if manifest is None:
        return report
    report.plugin_id = manifest.id
    report.checks.append(_check_artifacts(plugin_dir, manifest))
    report.checks.append(_check_sdk_adoption(plugin_dir, manifest))
    report.checks.extend(_probe_hooks(plugin_dir, manifest))
    return report


def run_conformance(plugins_root: Path, only: str | None = None) -> list[PluginReport]:
    reports: list[PluginReport] = []
    for child in sorted(plugins_root.iterdir()):
        if not child.is_dir() or (only and child.name != only):
            continue
        if not (child / plugin_manifest.PLUGIN_MANIFEST_FILENAME).exists():
            continue
        reports.append(check_plugin(child))
    return reports


def _print_report(reports: list[PluginReport]) -> None:
    for report in reports:
        sys.stdout.write(f"{report.plugin_id}: {report.status.upper()}\n")
        for check in report.checks:
            sys.stdout.write(f"  [{check.status}] {check.name}: {check.detail}\n")
    counts = {status: sum(1 for r in reports if r.status == status) for status in (PASS, FAIL, SKIP)}
    sys.stdout.write(f"\n{len(reports)} plugins — {counts[PASS]} pass, {counts[FAIL]} fail, {counts[SKIP]} skip\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Foresight plugin conformance harness")
    parser.add_argument("plugins_dir", nargs="?", default="plugins", type=Path)
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    parser.add_argument("--filter", default=None, help="only check the plugin with this directory name")
    args = parser.parse_args(argv)

    reports = run_conformance(args.plugins_dir, only=args.filter)
    if not reports:
        sys.stderr.write(f"no plugin manifests found under {args.plugins_dir}\n")
        return 1
    if args.json:
        sys.stdout.write(json.dumps([r.to_dict() for r in reports], indent=2) + "\n")
    else:
        _print_report(reports)
    return 1 if any(r.status == FAIL for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
