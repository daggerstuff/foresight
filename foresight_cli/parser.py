"""Shared argparse plumbing for the Foresight CLI.

Each command module exposes a ``COMMANDS`` mapping of :class:`CommandSpec`
entries. ``foresight_cli.cli`` assembles them into the command tree and
dispatches to a handler with the parameter object that handler accepts.
"""

from __future__ import annotations

import argparse
import inspect
import os
import sys
from collections.abc import Callable, Mapping
from typing import NamedTuple

from pydantic import BaseModel


class HelpDefaultsFormatter(argparse.RawDescriptionHelpFormatter):
    """Append the Typer-era ``[default: X]`` suffix to an option's help text.

    ``argparse`` never renders a default on its own, and
    ``ArgumentDefaultsHelpFormatter`` writes ``(default: None)`` for every
    argument that has none. This formatter annotates only options whose default
    is a real value: an absent default (``None``) stays silent, and a boolean
    flag stays bare because the flag itself already states its default.
    """

    def _get_help_string(self, action: argparse.Action) -> str:
        text = action.help if isinstance(action.help, str) else ""
        annotation = _default_annotation(action)
        if annotation is None:
            return text
        return f"{text} {annotation}"

    def _split_lines(self, text: str, width: int) -> list[str]:
        body, annotation = _split_annotation(text)
        if annotation is None:
            return super()._split_lines(text, width)
        lines = super()._split_lines(body, width) if body else []
        if not lines:
            return [annotation]
        merged = f"{lines[-1]} {annotation}"
        if len(merged) <= width:
            return [*lines[:-1], merged]
        return [*lines, annotation]


def _default_annotation(action: argparse.Action) -> str | None:
    """Return the default to display for ``action``, or ``None`` to display nothing."""
    if not action.option_strings:
        return None
    default = action.default
    if default is None or default is argparse.SUPPRESS or isinstance(default, bool):
        return None
    return f"[default: {default}]"


def _split_annotation(text: str) -> tuple[str, str | None]:
    """Separate a trailing ``[default: X]`` annotation from the help text before it."""
    marker = " [default: "
    index = text.rfind(marker)
    if index < 0 or not text.endswith("]"):
        return text, None
    return text[:index], text[index + 1 :]


FORMATTER = HelpDefaultsFormatter
SUPPRESS = argparse.SUPPRESS


class CommandSpec(NamedTuple):
    """A leaf command: its handler, its parameter object, and its flag declarations."""

    handler: Callable[..., None]
    params: type[BaseModel]
    arguments: Callable[[argparse.ArgumentParser], None] | None = None


class UsageError(Exception):
    """Raised when a command rejects its arguments; the CLI reports it as a usage error."""


def program_name() -> str:
    """Return the invocation name the CLI was started with."""
    path = sys.argv[0] if sys.argv else ""
    if os.path.basename(path) == "__main__.py":
        package = getattr(sys.modules.get("__main__"), "__package__", None)
        if package:
            return f"python -m {package}"
    return os.path.basename(path) or "foresight"


def _attach_help(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--help", action="help", help="Show this message and exit.")
    return parser


def new_parser(prog: str, description: str | None = None) -> argparse.ArgumentParser:
    """Build a parser with the CLI's shared help text and strict option matching."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description=description,
        add_help=False,
        allow_abbrev=False,
        formatter_class=FORMATTER,
    )
    return _attach_help(parser)


def _add_parser(
    subparsers: argparse._SubParsersAction,
    name: str,
    summary: str,
    description: str | None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        name,
        help=summary,
        description=description,
        add_help=False,
        allow_abbrev=False,
        formatter_class=FORMATTER,
    )
    return _attach_help(parser)


def add_group(subparsers: argparse._SubParsersAction, name: str, summary: str) -> argparse._SubParsersAction:
    """Register a command group and return the subparsers its commands attach to."""
    parser = _add_parser(subparsers, name, summary, summary)
    return parser.add_subparsers(required=True, metavar="COMMAND")


def add_command(
    subparsers: argparse._SubParsersAction,
    name: str,
    handler: Callable[..., None],
    params_type: type[BaseModel],
) -> argparse.ArgumentParser:
    """Register a leaf command, deriving its help text from the handler docstring."""
    docstring = inspect.cleandoc(getattr(handler, "__doc__", None) or "")
    summary = docstring.splitlines()[0] if docstring else ""
    parser = _add_parser(subparsers, name, summary, docstring or None)
    parser.set_defaults(handler=handler, params_type=params_type, command_parser=parser)
    return parser


def invoke(handler: Callable[..., None], params: BaseModel) -> None:
    """Run ``handler`` with ``params``, or with no argument if it declares none.

    A command whose parameter model carries no fields has nothing to pass, so its
    handler is allowed to omit the parameter entirely.
    """
    if inspect.signature(handler).parameters:
        handler(params)
    else:
        handler()


def register_commands(
    subparsers: argparse._SubParsersAction,
    commands: Mapping[str, CommandSpec],
    names: Mapping[str, str] | None = None,
) -> None:
    """Register commands; ``names`` maps the registered name onto a key in ``commands``."""
    pairs = [(name, name) for name in commands] if names is None else list(names.items())
    for name, key in pairs:
        spec = commands[key]
        parser = add_command(subparsers, name, spec.handler, spec.params)
        if spec.arguments is not None:
            spec.arguments(parser)
