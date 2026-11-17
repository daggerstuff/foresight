"""Shell completion scripts generated from the assembled argparse tree."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_SHELL = "bash"
PROGRAM = "foresight"
COMMANDS = ("foresight", "foresight-cli")

_INSTALL_DIRS = {
    "bash": (".bash_completions", ".sh"),
    "zsh": (".zsh_completions", ".zsh"),
    "fish": (".config/fish/completions", ".fish"),
    "powershell": (".powershell_completions", ".ps1"),
}


def _spec(parser: argparse.ArgumentParser) -> dict[tuple[str, ...], list[str]]:
    spec: dict[tuple[str, ...], list[str]] = {}
    pending: list[tuple[tuple[str, ...], argparse.ArgumentParser]] = [((), parser)]
    while pending:
        path, node = pending.pop()
        commands: list[str] = []
        options: list[str] = []
        subparsers: list[argparse._SubParsersAction] = []
        for action in node._actions:
            if isinstance(action, argparse._SubParsersAction):
                subparsers.append(action)
                commands.extend(action.choices)
            elif action.option_strings:
                options.extend(action.option_strings)
        spec[path] = [*commands, *options]
        for action in subparsers:
            for name, child in action.choices.items():
                pending.append(((*path, name), child))
    return spec


def _children(spec: dict[tuple[str, ...], list[str]], path: tuple[str, ...]) -> list[str]:
    depth = len(path)
    return sorted({key[depth] for key in spec if len(key) > depth and key[:depth] == path})


def _bash(spec: dict[tuple[str, ...], list[str]]) -> str:
    lines = [
        "_foresight_completion() {",
        '    local cur="${COMP_WORDS[COMP_CWORD]}"',
        '    local key=""',
        "    local i w",
        "    for ((i = 1; i < COMP_CWORD; i++)); do",
        '        w="${COMP_WORDS[i]}"',
        '        case "$w" in',
        "            -*) continue ;;",
        "        esac",
        '        if [ -z "$key" ]; then key="$w"; else key="$key $w"; fi',
        "    done",
        '    local candidates=""',
        '    case "$key" in',
    ]
    for path in sorted(spec):
        lines.append(f'        "{" ".join(path)}") candidates="{" ".join(spec[path])}" ;;')
    lines += [
        '        *) candidates="" ;;',
        "    esac",
        '    COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )',
        "}",
    ]
    for name in COMMANDS:
        lines.append(f"complete -o default -F _foresight_completion {name}")
    return "\n".join(lines) + "\n"


def _zsh(spec: dict[tuple[str, ...], list[str]]) -> str:
    lines = [
        f"#compdef {' '.join(COMMANDS)}",
        "_foresight() {",
        '    local key=""',
        "    local w",
        "    for w in ${words[2,CURRENT-1]}; do",
        '        case "$w" in',
        "            -*) continue ;;",
        "        esac",
        '        key="${key:+$key }$w"',
        "    done",
        "    local -a candidates",
        '    case "$key" in',
    ]
    for path in sorted(spec):
        lines.append(f'        "{" ".join(path)}") candidates=({" ".join(spec[path])}) ;;')
    lines += [
        "        *) candidates=() ;;",
        "    esac",
        "    compadd -a candidates",
        "}",
        f"compdef _foresight {' '.join(COMMANDS)}",
    ]
    return "\n".join(lines) + "\n"


def _fish(spec: dict[tuple[str, ...], list[str]]) -> str:
    lines: list[str] = []
    for name in COMMANDS:
        lines.append(f"complete -c {name} -f")
    for path in sorted(spec):
        conditions = [f"__fish_seen_subcommand_from {part}" for part in path]
        children = _children(spec, path)
        if children:
            conditions.append(f"not __fish_seen_subcommand_from {' '.join(children)}")
        condition = "; and ".join(conditions) if conditions else "true"
        for name in COMMANDS:
            lines.append(f'complete -c {name} -n "{condition}" -a "{" ".join(spec[path])}"')
    return "\n".join(lines) + "\n"


def _powershell(spec: dict[tuple[str, ...], list[str]]) -> str:
    lines = [
        f"Register-ArgumentCompleter -Native -CommandName {' '.join(COMMANDS)} -ScriptBlock {{",
        "    param($wordToComplete, $commandAst, $cursorPosition)",
        "    $words = $commandAst.CommandElements | Select-Object -Skip 1 |",
        "        ForEach-Object { $_.Extent.Text }",
        "    $key = ($words | Where-Object { $_ -notlike '-*' }) -join ' '",
        "    $candidates = switch ($key) {",
    ]
    for path in sorted(spec):
        words = ", ".join(f"'{word}'" for word in spec[path])
        lines.append(f"        '{' '.join(path)}' {{ @({words}) }}")
    lines += [
        "        default { @() }",
        "    }",
        '    $candidates | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {',
        "        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)",
        "    }",
        "}",
    ]
    return "\n".join(lines) + "\n"


_GENERATORS = {
    "bash": _bash,
    "zsh": _zsh,
    "fish": _fish,
    "powershell": _powershell,
}


def resolve_shell(name: str | None) -> str:
    """Map a requested shell name onto a supported generator."""
    if not name:
        return DEFAULT_SHELL
    lowered = name.strip().lower()
    return lowered if lowered in _GENERATORS else DEFAULT_SHELL


def shell_script(parser: argparse.ArgumentParser, shell: str | None) -> str:
    """Render the completion script for ``shell`` (bash when unknown)."""
    resolved = resolve_shell(shell)
    return _GENERATORS[resolved](_spec(parser))


def install(parser: argparse.ArgumentParser, shell: str | None) -> Path:
    """Write the completion script for ``shell`` into the user's home directory."""
    resolved = resolve_shell(shell)
    directory, suffix = _INSTALL_DIRS[resolved]
    target = Path.home() / directory / f"{PROGRAM}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(shell_script(parser, resolved))
    sys.stdout.write(f"{resolved} completion installed in {target}\n")
    sys.stdout.write("Completion will take effect once you restart the terminal\n")
    return target
