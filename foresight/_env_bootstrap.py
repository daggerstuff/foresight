"""Load repository ``.env`` files ahead of modules that snapshot the environment.

``foresight.config`` binds its constants from ``os.environ`` the first time it is
imported, so anything that wants additional ``.env`` files honoured has to load
them before that import happens. Importing this module performs the load, which
lets callers satisfy the ordering with a plain import statement instead of
executing setup code above their imports.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

ENV_CANDIDATES: tuple[Path, ...] = (
    Path.cwd() / ".env",
    Path.cwd() / ".env.local",
    Path(__file__).parent.parent / ".env",
    Path(__file__).parent.parent / ".env.local",
    Path(__file__).parent.parent.parent / ".env",
    Path(__file__).parent.parent.parent / ".env.local",
)


def load_env_files() -> tuple[Path, ...]:
    """Load every candidate ``.env`` file that exists, returning the ones loaded.

    ``load_dotenv`` is called without ``override``: values already present in the
    process environment always win over a file.
    """
    loaded: list[Path] = []
    for candidate in ENV_CANDIDATES:
        if candidate.exists():
            load_dotenv(candidate, override=False)
            loaded.append(candidate)
    return tuple(loaded)


ENV_FILES_LOADED = load_env_files()
