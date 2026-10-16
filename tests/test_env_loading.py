"""Environment bootstrapping for ``foresight.config`` (PIX-4731 follow-up).

``foresight.config`` snapshots ``os.environ`` into module constants, so the
developer ``.env`` has to be read *before* that snapshot. Entry points cannot do
it themselves — importing anything from the package runs ``foresight/__init__.py``,
which imports config first — and the raw-events layer (``FORESIGHT_RAW_RETENTION``)
is read per call so a running server and the tests can flip it.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from foresight import config as foresight_config

_SENTINEL_URL = "postgresql://sentinel:sentinel@sentinel.invalid:5432/sentinel_db"
_INHERITED = ("FORESIGHT_DB_URL", "FORESIGHT_DB_PATH", "FORESIGHT_RAW_RETENTION", "PYTEST_CURRENT_TEST")


def _run_in_child_env(tmp_path: Path, statements: list[str], extra_env: dict[str, str] | None = None) -> str:
    """Run ``python -c`` in a clean environment with ``tmp_path`` as the cwd."""
    env = {k: v for k, v in os.environ.items() if k not in _INHERITED}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra_env or {})
    result = subprocess.run(
        [sys.executable, "-c", "\n".join(statements)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class TestCallTimeRetentionFlags:
    def test_raw_retention_reads_env_per_call(self, monkeypatch):
        """The gate must follow the environment, not an import-time snapshot."""
        monkeypatch.delenv("FORESIGHT_RAW_RETENTION", raising=False)
        assert foresight_config.raw_retention_enabled() is False

        monkeypatch.setenv("FORESIGHT_RAW_RETENTION", "1")
        assert foresight_config.raw_retention_enabled() is True

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, None), ("", None), ("45", 45), (" 45 ", 45), ("not-a-number", None)],
    )
    def test_raw_retention_window_days(self, monkeypatch, value, expected):
        if value is None:
            monkeypatch.delenv("FORESIGHT_RAW_RETENTION_WINDOW_DAYS", raising=False)
        else:
            monkeypatch.setenv("FORESIGHT_RAW_RETENTION_WINDOW_DAYS", value)
        assert foresight_config.raw_retention_window_days() == expected


class TestDotenvBootstrap:
    def test_dotenv_file_reaches_import_time_constants(self, tmp_path):
        """A ``.env`` in the working directory must be visible to config's snapshot.

        Regression: the loader used to run from ``main()`` — after the package had
        already imported config — so ``FORESIGHT_RAW_RETENTION`` (and the database
        URL) set only in ``.env`` were silently ignored by ``python -m foresight``.
        """
        (tmp_path / ".env").write_text(f"FORESIGHT_DB_URL={_SENTINEL_URL}\nFORESIGHT_RAW_RETENTION=1\n")

        printed = _run_in_child_env(
            tmp_path,
            [
                "from foresight import config",
                "print(config.DB_URL, config.raw_retention_enabled())",
            ],
        )
        assert _SENTINEL_URL in printed
        assert printed.endswith("True")

    def test_exported_environment_wins_over_dotenv(self, tmp_path):
        """``.env`` must never retarget a server that was launched with an explicit URL."""
        (tmp_path / ".env").write_text(f"FORESIGHT_DB_URL={_SENTINEL_URL}\n")
        exported = "postgresql://exported:exported@exported.invalid:5432/exported_db"

        printed = _run_in_child_env(
            tmp_path,
            [
                "from foresight import config",
                "print(config.DB_URL)",
            ],
            extra_env={"FORESIGHT_DB_URL": exported},
        )
        assert printed == exported
        assert _SENTINEL_URL not in printed

    def test_loader_is_skipped_under_pytest(self, monkeypatch, tmp_path):
        """A developer ``.env`` must never leak into the test session."""
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_env_loading.py::dummy")
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(f"FORESIGHT_DB_URL={_SENTINEL_URL}\n")

        foresight_config.load_dotenv_walkup()
        assert os.environ.get("FORESIGHT_DB_URL") != _SENTINEL_URL
