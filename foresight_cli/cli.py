"""Foresight CLI — memory, context-blocks, curation, system, and TUI.

Usage:
  foresight [--agent|--json] <command> [options]
  foresight tui                # Launch interactive TUI
  foresight store <content>    # Quick memory store
  foresight status             # System health
  foresight --help             # Full help
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from foresight.config import load_dotenv_walkup

from . import completion
from .commands import (
    analysis,
    benchmark,
    blocks,
    curate,
    decay,
    documents,
    eval as eval_cmd,
    graph,
    memory,
    rrf,
    security,
    system,
    users,
)
from .parser import SUPPRESS, UsageError, add_command, add_group, new_parser, program_name, register_commands
from .utils import config as cfg, output as out

ForesightTUI: type | None
try:
    from .tui.app import ForesightTUI as _ForesightTUI

    ForesightTUI = _ForesightTUI
except ImportError:
    ForesightTUI = None


class OutputMode(StrEnum):
    human = "human"
    agent = "agent"
    json = "json"


_DEFAULT_OUTPUT = OutputMode.human
_OUTPUT_CHOICES = tuple(mode.value for mode in OutputMode)

_GROUPS = (
    ("memory", "Store, retrieve, search memories.", memory),
    ("analysis", "Synthesize, reflect, profile, diff, rollback.", analysis),
    ("blocks", "Manage context blocks.", blocks),
    ("curate", "Manage curation runs.", curate),
    ("benchmark", "Run Production Value & Proof Benchmark Suite.", benchmark),
    ("security", "Manage security, sensitivity policies, and AES-256-GCM encryption.", security),
    ("user", "Provision users and API keys for remote/multi-user access.", users),
    ("eval", "Run evaluation harness (PIX-3953).", eval_cmd),
    (
        "system",
        "System status, init, doctor, config, stats, temporal, history, maintenance, reprocess, tenant.",
        system,
    ),
    ("rrf", "View and tune RRF retrieval weights.", rrf),
    ("documents", "Manage source documents and chunks.", documents),
    ("graph", "Graph, entity, and clustering operations.", graph),
    ("decay", "Decay, strength, recovery, and relevance.", decay),
)

_ALIASES = (
    ("store", memory, "store"),
    ("get", memory, "get"),
    ("list", memory, "list"),
    ("query", memory, "query"),
    ("delete", memory, "delete"),
    ("reinforce", memory, "reinforce"),
    ("search", memory, "search"),
    ("export", memory, "export"),
    ("import", memory, "import"),
    ("status", system, "status"),
    ("init", system, "init"),
    ("doctor", system, "doctor"),
    ("stats", system, "stats"),
    ("config", system, "config"),
    ("synthesize", analysis, "synthesize"),
    ("reflect", analysis, "reflect"),
    ("profile", analysis, "profile"),
    ("prove", benchmark, "run"),
)


class TuiParams(BaseModel):
    """Arguments for :func:`tui`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    user_id: str | None = Field(default=None, description="User ID override")


def _decode_tool_result(result: str | dict) -> dict:
    """Wrap plain-text errors into JSON format for CLI JSON output mode.

    Args:
        result: Either a plain text error string or an already-parsed dict.

    Returns:
        A dict with ``{"ok": True, ...}`` for raw dict passthrough,
        or ``{"ok": False, "error": {"message": text}}`` for plain text.
    """
    if isinstance(result, dict):
        return result
    return {"ok": False, "error": {"message": result}}


def tui(params: TuiParams) -> None:
    """Launch the Foresight interactive TUI (Textual)."""
    if ForesightTUI is None:
        out.error("Cannot launch TUI — missing dependency. Install with: pip install 'foresight[tui]'")
        raise SystemExit(1)

    ui_cfg = cfg.ensure_config()
    resolved_uid = cfg.get_user_id(params.user_id)
    tui_app = ForesightTUI(user_id=resolved_uid, config=ui_cfg)
    with contextlib.suppress(KeyboardInterrupt):
        tui_app.run()


def _tui_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user-id", "-u", default=None, help=SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    """Assemble the full command tree."""
    parser = new_parser(program_name(), "Foresight Memory Management — CLI, TUI, and agent tools.")
    parser.add_argument(
        "--output",
        "-o",
        dest="global_output",
        type=str.lower,
        choices=_OUTPUT_CHOICES,
        default=None,
        help="Output mode: human (rich), agent (tagged), json (raw JSON)",
    )
    parser.add_argument("--agent", "-a", dest="global_agent", action="store_true", help=SUPPRESS)
    parser.add_argument("--json", "-j", dest="global_json", action="store_true", help=SUPPRESS)
    parser.add_argument("--user-id", "-u", dest="global_user_id", default=None, help=SUPPRESS)
    parser.add_argument("--verbose", "-v", dest="global_verbose", action="store_true", help="Verbose output")
    parser.add_argument(
        "--install-completion",
        dest="global_install_completion",
        action="store_true",
        default=None,
        help="Install completion for the current shell.",
    )
    parser.add_argument(
        "--show-completion",
        dest="global_show_completion",
        metavar="SHELL",
        nargs="?",
        const="",
        default=None,
        help="Show completion for the current shell, to copy it or customize the installation.",
    )

    subparsers = parser.add_subparsers(required=True, metavar="COMMAND")
    _tui_parser(add_command(subparsers, "tui", tui, TuiParams))
    for name, summary, module in _GROUPS:
        register_commands(add_group(subparsers, name, summary), module.COMMANDS)
    for name, module, command in _ALIASES:
        register_commands(subparsers, module.COMMANDS, {name: command})
    return parser


def _configure(namespace: argparse.Namespace) -> None:
    load_dotenv_walkup()
    mode = OutputMode(namespace.global_output) if namespace.global_output else _DEFAULT_OUTPUT
    if namespace.global_agent:
        mode = OutputMode.agent
    elif namespace.global_json:
        mode = OutputMode.json
    out.configure(
        mode=mode.value,
        color=True,
        pipe_safe=mode in (OutputMode.agent, OutputMode.json),
        verbose=namespace.global_verbose,
    )
    cfg.get_user_id(namespace.global_user_id)


def _completion_request(parser: argparse.ArgumentParser, arguments: list[str]) -> int | None:
    """Handle the completion flags eagerly, before command resolution."""
    for index, word in enumerate(arguments):
        if word == "--install-completion":
            completion.install(parser, None)
            return 0
        if word == "--show-completion" or word.startswith("--show-completion="):
            shell = word.partition("=")[2] or None
            if shell is None and index + 1 < len(arguments) and not arguments[index + 1].startswith("-"):
                shell = arguments[index + 1]
            sys.stdout.write(completion.shell_script(parser, shell))
            return 0
    return None


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv``, dispatch the selected command, and return its exit status."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not arguments:
        parser.print_help()
        return 2
    handled = _completion_request(parser, arguments)
    if handled is not None:
        return handled
    namespace, extras = parser.parse_known_args(arguments)
    if extras:
        namespace.command_parser.error(f"unrecognized arguments: {' '.join(extras)}")
    _configure(namespace)
    params_type = namespace.params_type
    params = params_type(**{name: getattr(namespace, name) for name in params_type.model_fields})
    try:
        namespace.handler(params)
    except UsageError as exc:
        namespace.command_parser.error(str(exc))
    return 0
