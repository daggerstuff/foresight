"""Allow `python -m foresight_cli` to work."""

from foresight_cli.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
