"""Foresight agent-plugin manifest contract (FOR-68).

Every native agent integration ships a ``foresight.plugin.json`` manifest so
installers, the conformance harness, and docs can treat plugins uniformly.
The canonical JSON Schema lives at ``plugins/manifest.schema.json`` and is
generated from :func:`manifest_json_schema`; ``tests/test_plugin_manifest.py``
fails if the checked-in file drifts.

Manifest files are self-describing: they name the agent targets, the surfaces
the plugin implements (MCP config, hooks, skills, provider, ...), the Foresight
capabilities it requires, the artifacts it installs, and how to install and
uninstall it. Credentials are referenced by env-file/key name only — never
inline secrets.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PLUGIN_MANIFEST_FILENAME = "foresight.plugin.json"
PLUGIN_MANIFEST_SCHEMA_PATH = "plugins/manifest.schema.json"


class AgentTarget(StrEnum):
    """Agent surfaces a plugin can target. See plugins/README.md for the matrix."""

    CLAUDE_CODE = "claude-code"
    CLAUDE_DESKTOP = "claude-desktop"
    GEMINI_CLI = "gemini-cli"
    ANTIGRAVITY = "antigravity"
    CODEX = "codex"
    CURSOR = "cursor"
    DEVIN = "devin"
    GOOSE = "goose"
    HERMES = "hermes"
    MASTRACODE = "mastracode"
    OMO = "omo"
    OPENCODE = "opencode"
    OHMYPI = "ohmypi"
    PI = "pi"
    DEEPSEEK_HARNESS = "deepseek-harness"
    COPILOT = "copilot"
    GENERIC_MCP = "generic-mcp"
    WEB = "web"


class PluginSurface(StrEnum):
    """Capability surfaces a plugin artifact can implement."""

    MCP = "mcp"
    REST = "rest"
    HOOKS = "hooks"
    SKILLS = "skills"
    RULES = "rules"
    EXTENSION = "extension"
    PROVIDER = "provider"
    STATUS_UI = "status-ui"


class ServerTransport(StrEnum):
    HTTP = "http"
    STDIO = "stdio"
    SSE = "sse"


class ArtifactType(StrEnum):
    """Kind of installed artifact a manifest entry describes."""

    CONFIG = "config"
    HOOK = "hook"
    SKILL = "skill"
    RULE = "rule"
    EXTENSION = "extension"
    PROVIDER = "provider"
    CLI = "cli"
    DOCS = "docs"
    NATIVE_MANIFEST = "native-manifest"
    SOURCE = "source"


class ForesightCapability(StrEnum):
    """Foresight capabilities a plugin may require. Mirrors server/REST surface."""

    CONTEXT_INJECT = "context.inject"
    CONTEXT_BLOCKS = "context-blocks"
    CAPTURE_TRANSCRIPT = "capture.transcript"
    CAPTURE_IN_FLIGHT = "capture.in-flight"
    MEMORY_SEARCH = "memory.search"
    MEMORY_STORE = "memory.store"
    MEMORY_UPDATE = "memory.update"
    MEMORY_DELETE = "memory.delete"
    MEMORY_REFLECT = "memory.reflect"
    PROFILE_READ = "profile.read"
    PROFILE_WRITE = "profile.write"
    STATUS_HEALTH = "status.health"
    STATUS_DOCTOR = "status.doctor"
    DOCUMENTS_INGEST = "documents.ingest"
    TEMPORAL_QUERY = "temporal.query"
    CURATION_MANAGE = "curation.manage"
    TELEMETRY_GATING = "telemetry.gating"


class PluginManifestError(ValueError):
    """Raised when a foresight.plugin.json fails contract validation."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PluginAuthor(_StrictModel):
    name: str = Field(min_length=1)
    url: str | None = None


class PluginRequires(_StrictModel):
    """What the plugin needs from a Foresight deployment."""

    foresight: str = Field(
        min_length=1,
        description='Version constraint, e.g. ">=0.19.0". Free-form semver range.',
    )
    transports: list[ServerTransport] = Field(default_factory=list)
    capabilities: list[ForesightCapability] = Field(default_factory=list)


class PluginArtifact(_StrictModel):
    """One artifact the plugin installs into the agent's home/config."""

    type: ArtifactType
    path: str = Field(min_length=1, description="Path relative to the plugin directory.")
    surface: PluginSurface | None = Field(
        default=None,
        description="Capability surface this artifact implements, if any.",
    )
    targets: list[AgentTarget] | None = Field(
        default=None,
        description="Agent targets this artifact applies to; null/omitted means all targets.",
    )
    description: str | None = None

    @field_validator("path")
    @classmethod
    def _path_is_relative(cls, value: str) -> str:
        if value.startswith(("/", "~")) or ".." in Path(value).parts:
            raise ValueError("artifact path must be relative to the plugin directory")
        return value


class PluginCredentials(_StrictModel):
    """Credential *references* only — manifests must never embed secrets."""

    env_file: str | None = Field(
        default=None,
        alias="envFile",
        description="Path to the env file holding secrets, e.g. ~/.config/foresight/credentials.env",
    )
    keys: list[str] = Field(default_factory=list)


class PluginInstall(_StrictModel):
    command: str = Field(min_length=1, description="Shell command that installs the plugin.")
    dry_run_flag: str | None = Field(
        default=None,
        alias="dryRunFlag",
        description="Flag that previews changes without applying them.",
    )


class PluginUninstall(_StrictModel):
    command: str | None = None
    notes: str | None = None


class PluginManifest(_StrictModel):
    """Root manifest model; one per plugin directory as ``foresight.plugin.json``."""

    spec_version: int = Field(
        default=1,
        alias="specVersion",
        description="Contract version; currently only 1.",
    )
    id: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="Stable plugin id, e.g. 'claude', 'hermes', 'mastracode'.",
    )
    name: str = Field(min_length=1)
    version: str = Field(min_length=1, description="Plugin semver, independent of the Foresight server version.")
    description: str | None = None
    author: PluginAuthor | None = None
    license: str | None = None
    homepage: str | None = None
    repository: str | None = None
    targets: list[AgentTarget] = Field(min_length=1)
    surfaces: list[PluginSurface] = Field(min_length=1)
    requires: PluginRequires
    artifacts: list[PluginArtifact] = Field(default_factory=list)
    credentials: PluginCredentials | None = None
    install: PluginInstall | None = None
    uninstall: PluginUninstall | None = None
    keywords: list[str] = Field(default_factory=list)

    @field_validator("spec_version")
    @classmethod
    def _supported_spec_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported specVersion; only 1 is defined")
        return value


def parse_plugin_manifest(data: Mapping[str, Any]) -> PluginManifest:
    """Validate an already-decoded manifest mapping."""
    try:
        return PluginManifest.model_validate(dict(data))
    except ValidationError as exc:
        raise PluginManifestError(f"invalid plugin manifest: {exc}") from exc


def load_plugin_manifest(path: str | Path) -> PluginManifest:
    """Load and validate ``foresight.plugin.json`` from a file or plugin directory."""
    p = Path(path)
    manifest_path = p if p.is_file() else p / PLUGIN_MANIFEST_FILENAME
    try:
        data = json.loads(manifest_path.read_text())
    except FileNotFoundError as exc:
        raise PluginManifestError(f"{manifest_path}: file not found") from exc
    except json.JSONDecodeError as exc:
        raise PluginManifestError(f"{manifest_path}: invalid JSON: {exc}") from exc
    try:
        return parse_plugin_manifest(data)
    except PluginManifestError as exc:
        raise PluginManifestError(f"{manifest_path}: {exc}") from exc


def iter_plugin_manifests(root: str | Path) -> Iterator[Path]:
    """Yield every ``foresight.plugin.json`` under ``root`` (sorted, deterministic)."""
    r = Path(root)
    if r.is_file():
        yield r
        return
    yield from sorted(r.rglob(PLUGIN_MANIFEST_FILENAME))


def manifest_json_schema() -> dict[str, Any]:
    """Return the JSON Schema for the manifest contract (canonical: plugins/manifest.schema.json)."""
    return PluginManifest.model_json_schema()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m foresight.plugin_manifest",
        description="Validate foresight.plugin.json manifests.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Plugin directories, manifest files, or a root to search recursively.",
    )
    parser.add_argument(
        "--write-schema",
        type=Path,
        metavar="PATH",
        help="Write the JSON Schema to PATH and exit (no validation).",
    )
    args = parser.parse_args(argv)

    if args.write_schema is not None:
        schema = manifest_json_schema()
        args.write_schema.parent.mkdir(parents=True, exist_ok=True)
        args.write_schema.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        sys.stdout.write(f"wrote {args.write_schema}\n")
        return 0

    if not args.paths:
        parser.error("at least one path is required unless --write-schema is used")

    failures = 0
    checked = 0
    for path in args.paths:
        for manifest_path in iter_plugin_manifests(path):
            checked += 1
            try:
                manifest = load_plugin_manifest(manifest_path)
            except PluginManifestError as exc:
                failures += 1
                sys.stderr.write(f"FAIL {manifest_path}: {exc}\n")
                continue
            sys.stdout.write(
                f"OK   {manifest_path}  ({manifest.id} v{manifest.version}, targets={len(manifest.targets)})\n"
            )
    if checked == 0:
        sys.stderr.write("no foresight.plugin.json files found\n")
        return 2
    if failures:
        sys.stderr.write(f"{failures}/{checked} manifest(s) invalid\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
