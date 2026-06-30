#!/usr/bin/env python3
"""Entry point for running foresight-mcp as a module."""

from __future__ import annotations

import contextlib
import sys
from importlib.metadata import PackageNotFoundError

from .server import get_system_status, init_db, main as run_server


def main() -> None:
    """Support lightweight CLI flags before starting the MCP server."""
    if "-h" in sys.argv or "--help" in sys.argv:
        return

    if "--health" in sys.argv:
        import json as _json

        init_db()
        result = get_system_status()
        if "--json" in sys.argv:
            _json.loads(result) if isinstance(result, str) else result
        else:
            pass
        return

    if "--version" in sys.argv:
        with contextlib.suppress(PackageNotFoundError):
            pass
        return

    run_server()


if __name__ == "__main__":
    main()
