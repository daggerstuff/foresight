#!/usr/bin/env python3
"""Entry point for running foresight-mcp as a module."""
from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version

from .server import main as run_server, memory_status


def main() -> None:
    """Support lightweight CLI flags before starting the MCP server."""
    if "-h" in sys.argv or "--help" in sys.argv:
        print("Usage:")
        print("  uv run foresight-mcp          # Start MCP server")
        print("  uv run foresight-mcp --health # Print health JSON and exit")
        print("  uv run foresight-mcp --version # Print package version and exit")
        print("  uv run foresight --help        # Show CLI help")
        print("  uv run foresight-cli --help    # Show CLI help (legacy alias)")
        return

    if "--health" in sys.argv:
        print(memory_status())
        return

    if "--version" in sys.argv:
        try:
            print(version("foresight-mcp"))
        except PackageNotFoundError:
            print("0.0.0")
        return

    run_server()


if __name__ == "__main__":
    main()
