#!/usr/bin/env python3
"""Entry point for running foresight-mcp as a module."""

from __future__ import annotations

import contextlib
import json as _json
import sys
from importlib.metadata import PackageNotFoundError

from .server import get_system_status, init_db, main as run_server


def main() -> None:
    """Support lightweight CLI flags before starting the MCP server."""
    if "-h" in sys.argv or "--help" in sys.argv:
        sys.stdout.write("Usage: foresight-mcp [--health] [--health --json] [--version]\n")
        return

    if "--health" in sys.argv:
        init_db()
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

    run_server()


if __name__ == "__main__":
    main()
