"""Contract tests for the CLI's parser plumbing and its argument reporting.

Three invariants live here. The ``[default: X]`` annotations the help output
carries are derived from the parser objects and cross-checked against what the
configured formatter actually renders, so a refactor that drops the custom
formatter fails here instead of silently removing every annotation from 38
command screens. A parameter model that rejects the parsed namespace must
surface as the CLI's own usage error rather than a traceback. And a hand-edited
config file that sets ``user_id`` to ``null`` must not put ``None`` behind a
field declared ``str``.
"""

from __future__ import annotations

import argparse
import json

import pytest
from pydantic import BaseModel, Field

import foresight_cli.cli as cli_module
from foresight_cli.utils import config as cfg

ANNOTATED_OPTION_TOTAL = 75
ANNOTATED_COMMAND_TOTAL = 42
USAGE_ERROR_STATUS = 2


def _subparser_actions(parser: argparse.ArgumentParser) -> list[argparse._SubParsersAction]:
    return [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]


def _leaf_parsers(parser: argparse.ArgumentParser) -> list[tuple[str, argparse.ArgumentParser]]:
    """Return every leaf command parser together with its command path."""
    leaves: list[tuple[str, argparse.ArgumentParser]] = []

    def walk(node: argparse.ArgumentParser, path: str) -> None:
        actions = _subparser_actions(node)
        if not actions:
            leaves.append((path, node))
            return
        for action in actions:
            for name, child in action.choices.items():
                walk(child, f"{path} {name}".strip())

    walk(parser, "")
    return leaves


def _carries_default_annotation(action: argparse.Action) -> bool:
    """Report whether the help contract annotates ``action`` with its default.

    Only options carrying a real, non-boolean default are annotated: an absent
    default stays silent, and a boolean flag already states its default in its
    own name. ``0`` and ``0.0`` are real defaults and must render.
    """
    if not action.option_strings:
        return False
    default = action.default
    return default is not None and default is not argparse.SUPPRESS and not isinstance(default, bool)


def _annotated_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [action for action in parser._actions if _carries_default_annotation(action)]


def _command_parser(parser: argparse.ArgumentParser, path: list[str]) -> argparse.ArgumentParser:
    """Walk the command tree to the parser registered at ``path``."""
    node = parser
    for name in path:
        actions = _subparser_actions(node)
        node = actions[0].choices[name]
    return node


def test_every_annotated_option_renders_its_default():
    leaves = _leaf_parsers(cli_module.build_parser())
    assert len(leaves) > ANNOTATED_COMMAND_TOTAL

    total = 0
    annotated_commands = 0
    for path, parser in leaves:
        expected = len(_annotated_actions(parser))
        rendered = parser.format_help().count("[default: ")
        assert rendered == expected, f"{path}: {expected} annotated options, {rendered} rendered"
        total += expected
        annotated_commands += 1 if expected else 0

    assert total == ANNOTATED_OPTION_TOTAL
    assert annotated_commands == ANNOTATED_COMMAND_TOTAL


def test_falsy_defaults_render_and_absent_defaults_stay_silent():
    integer_zero = 0
    float_zero = 0
    absent = 0

    for path, parser in _leaf_parsers(cli_module.build_parser()):
        help_text = parser.format_help()
        assert "[default: None]" not in help_text, path
        for action in parser._actions:
            if not action.option_strings or isinstance(action.default, bool):
                continue
            if action.default is None or action.default is argparse.SUPPRESS:
                absent += 1
            elif isinstance(action.default, int) and action.default == 0:
                integer_zero += 1
                assert "[default: 0]" in help_text, path
            elif isinstance(action.default, float) and action.default == 0.0:
                float_zero += 1
                assert "[default: 0.0]" in help_text, path

    assert integer_zero > 0
    assert float_zero > 0
    assert absent > 0


class _ConstrainedClusterParams(BaseModel):
    """A parameter model whose constraint argparse does not enforce."""

    min_cluster_size: int = Field(default=2, ge=10)


def test_rejected_parameters_are_a_usage_error_not_a_traceback(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "config.json")

    build_parser = cli_module.build_parser

    def constrained_parser() -> argparse.ArgumentParser:
        parser = build_parser()
        _command_parser(parser, ["graph", "cluster"]).set_defaults(params_type=_ConstrainedClusterParams)
        return parser

    monkeypatch.setattr(cli_module, "build_parser", constrained_parser)

    with pytest.raises(SystemExit) as exit_info:
        cli_module.main(["graph", "cluster", "--min-size", "3"])

    assert exit_info.value.code == USAGE_ERROR_STATUS
    stderr = capsys.readouterr().err
    assert "Traceback" not in stderr
    assert "error:" in stderr
    assert "min_cluster_size" in stderr


def test_null_user_id_in_config_does_not_reach_the_declared_field(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"user_id": None}))
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_PATH", config_path)
    for variable in ("FORESIGHT_DB_URL", "FORESIGHT_USER_ID", "FORESIGHT_BANK_ID", "FORESIGHT_DB_PATH"):
        monkeypatch.delenv(variable, raising=False)

    loaded = cfg.CliConfig.load()

    assert isinstance(loaded.user_id, str)
    assert loaded.user_id == cfg.DEFAULTS["user_id"]
