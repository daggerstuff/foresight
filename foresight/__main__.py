#!/usr/bin/env python3
"""Entry point for running foresight as a module."""

from __future__ import annotations

import warnings

warnings.filterwarnings("ignore", category=RuntimeWarning, message=".*found in sys.modules.*")

import contextlib
import json as _json
import os
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

from dotenv import load_dotenv

from .server import _initialize_backend, get_system_status, init_db, main as run_server


def _load_dotenv() -> None:
    """Walk up to find .env: foresight/.env → pixelated/.env → home/.env.

    Called from main() — never at import time — so importing this module
    (e.g. in tests) has no side effects on the environment. The console
    script entry point (foresight-server) imports this module and calls
    main(), so guarding behind ``if __name__ == "__main__"`` would break
    .env loading there. Skipped entirely while under pytest so test
    execution never loads a developer .env.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    _project_root = Path(__file__).resolve().parent.parent.parent
    for _candidate in [Path(".env"), _project_root / ".env", Path.home() / ".env"]:
        if _candidate.exists():
            load_dotenv(_candidate)
            break


def main() -> None:
    """Support lightweight CLI flags before starting the MCP server."""
    _load_dotenv()
    if "-h" in sys.argv or "--help" in sys.argv:
        sys.stdout.write(
            "Usage: foresight-server [--health] [--health --json] [--version] [--host HOST] [--port PORT]\n"
        )
        return

    if "--health" in sys.argv:
        init_db()
        _initialize_backend()
        result = get_system_status()
        if "--json" in sys.argv:
            payload = _json.loads(result) if isinstance(result, str) else result
            sys.stdout.write(f"{_json.dumps(payload)}\n")
        else:
            sys.stdout.write(f"{result if isinstance(result, str) else _json.dumps(result)}\n")
        return

    if "--version" in sys.argv:
        with contextlib.suppress(PackageNotFoundError):
            pass
        return

    host: str | None = None
    port: int | None = None
    if "--host" in sys.argv:
        idx = sys.argv.index("--host")
        if idx + 1 < len(sys.argv):
            host = sys.argv[idx + 1]
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    run_server(host=host, port=port)


if __name__ == "__main__":
    main()
