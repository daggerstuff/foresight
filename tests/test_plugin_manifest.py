"""Tests for the agent-plugin manifest contract (FOR-68)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from foresight.plugin_manifest import (
    PLUGIN_MANIFEST_FILENAME,
    PLUGIN_MANIFEST_SCHEMA_PATH,
    AgentTarget,
    PluginManifestError,
    PluginSurface,
    _main,
    iter_plugin_manifests,
    load_plugin_manifest,
    manifest_json_schema,
    parse_plugin_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"


def _minimal_manifest(**overrides):
    data = {
        "specVersion": 1,
        "id": "test-plugin",
        "name": "Test Plugin",
        "version": "0.1.0",
        "targets": ["generic-mcp"],
        "surfaces": ["mcp"],
        "requires": {"foresight": ">=0.19.0", "transports": ["http"], "capabilities": []},
    }
    data.update(overrides)
    return data


def _artifact_accepts(path_value: str) -> bool:
    try:
        parse_plugin_manifest(_minimal_manifest(artifacts=[{"type": "hook", "path": path_value}]))
    except PluginManifestError:
        return False
    return True


def _path_schema() -> dict:
    schema = manifest_json_schema()
    artifact = schema["$defs"]["PluginArtifact"]["properties"]["path"]
    assert artifact, "PluginArtifact.path missing from the generated schema"
    return artifact


def _schema_accepts(path_value: str) -> bool:
    path_schema = _path_schema()
    if len(path_value) < path_schema.get("minLength", 0):
        return False
    pattern = path_schema.get("pattern")
    if pattern is not None and re.search(pattern, path_value) is None:
        return False
    forbidden = [rule["pattern"] for rule in path_schema["not"]["anyOf"]]
    return all(re.search(pattern, path_value) is None for pattern in forbidden)


class TestParsePluginManifest:
    def test_minimal_valid(self):
        manifest = parse_plugin_manifest(_minimal_manifest())
        assert manifest.id == "test-plugin"
        assert manifest.spec_version == 1
        assert manifest.targets == [AgentTarget.GENERIC_MCP]
        assert manifest.surfaces == [PluginSurface.MCP]

    def test_rejects_missing_required(self):
        data = _minimal_manifest()
        del data["targets"]
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(data)

    def test_rejects_unknown_target(self):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(targets=["not-an-agent"]))

    def test_rejects_unknown_surface(self):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(surfaces=["telepathy"]))

    def test_rejects_bad_spec_version(self):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(specVersion=2))

    def test_spec_version_is_required(self):
        data = _minimal_manifest()
        del data["specVersion"]
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(data)

    @pytest.mark.parametrize("value", [True, False, "1", "01"])
    def test_rejects_non_integer_spec_version(self, value):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(specVersion=value))

    def test_accepts_integral_float_spec_version(self):
        assert parse_plugin_manifest(_minimal_manifest(specVersion=1.0)).spec_version == 1

    def test_rejects_extra_fields(self):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(unexpectedField=True))

    @pytest.mark.parametrize("python_name", ["spec_version", "envFile", "dry_run_flag"])
    def test_rejects_python_style_extra_fields(self, python_name):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(**{python_name: 1}))

    def test_rejects_absolute_artifact_path(self):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(artifacts=[{"type": "hook", "path": "/etc/passwd"}]))

    def test_rejects_parent_artifact_path(self):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(artifacts=[{"type": "hook", "path": "../escape.sh"}]))

    @pytest.mark.parametrize(
        "bad_path",
        ["..\\..\\x", "\\Windows\\x", "C:/x", "C:x", "a\\..\\b", "~/.foresight/credentials.env"],
    )
    def test_rejects_escaping_artifact_paths(self, bad_path: str):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(artifacts=[{"type": "hook", "path": bad_path}]))

    def test_accepts_windows_style_relative_path(self):
        manifest = parse_plugin_manifest(
            _minimal_manifest(artifacts=[{"type": "hook", "path": "hooks\\foresight_hook.py"}])
        )
        assert manifest.artifacts[0].path == "hooks\\foresight_hook.py"

    def test_empty_targets_rejected(self):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(targets=[]))

    @pytest.mark.parametrize("bad_version", ["1.0", "v1.0.0", "1.0.0.0", "latest", "1.0.0-"])
    def test_rejects_non_semver_version(self, bad_version: str):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(version=bad_version))

    def test_accepts_semver_with_prerelease_and_build(self):
        manifest = parse_plugin_manifest(_minimal_manifest(version="1.2.3-rc.1+build.5"))
        assert manifest.version == "1.2.3-rc.1+build.5"

    @pytest.mark.parametrize("missing", ["transports", "capabilities"])
    def test_requires_must_declare_transports_and_capabilities(self, missing: str):
        requires = {"foresight": ">=0.19.0", "transports": ["http"], "capabilities": []}
        del requires[missing]
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(requires=requires))

    def test_accepts_empty_transports_and_capabilities(self):
        manifest = parse_plugin_manifest(
            _minimal_manifest(requires={"foresight": ">=0.19.0", "transports": [], "capabilities": []})
        )
        assert manifest.requires.transports == []
        assert manifest.requires.capabilities == []

    def test_install_requires_command_or_notes(self):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(install={"dryRunFlag": "--dry-run"}))

    def test_install_accepts_notes_without_command(self):
        manifest = parse_plugin_manifest(_minimal_manifest(install={"notes": "Install via /plugins UI"}))
        assert manifest.install is not None
        assert manifest.install.command is None
        assert manifest.install.notes == "Install via /plugins UI"

    def test_full_manifest_round_trip(self, tmp_path: Path):
        manifest_file = tmp_path / PLUGIN_MANIFEST_FILENAME
        manifest_file.write_text(
            json.dumps(
                _minimal_manifest(
                    description="desc",
                    author={"name": "acme"},
                    license="MIT",
                    homepage="https://example.com",
                    artifacts=[{"type": "hook", "path": "hooks/h.py", "surface": "hooks"}],
                    credentials={"envFile": "~/.config/foresight/credentials.env", "keys": ["FORESIGHT_API_KEY"]},
                    install={"command": "bash install.sh", "dryRunFlag": "--dry-run"},
                    uninstall={"notes": "manual"},
                )
            )
        )
        manifest = load_plugin_manifest(tmp_path)
        assert manifest.artifacts[0].surface == PluginSurface.HOOKS
        assert manifest.credentials is not None
        assert manifest.credentials.keys == ["FORESIGHT_API_KEY"]
        assert manifest.install is not None
        assert manifest.install.dry_run_flag == "--dry-run"


class TestManifestDocumentRoot:
    def test_parse_rejects_array_root(self):
        with pytest.raises(PluginManifestError, match="expected a JSON object"):
            parse_plugin_manifest([])

    def test_parse_rejects_string_root(self):
        with pytest.raises(PluginManifestError, match="expected a JSON object"):
            parse_plugin_manifest("nope")

    def test_load_rejects_array_root(self, tmp_path: Path):
        (tmp_path / PLUGIN_MANIFEST_FILENAME).write_text(json.dumps([_minimal_manifest()]))
        with pytest.raises(PluginManifestError, match="expected a JSON object"):
            load_plugin_manifest(tmp_path)


class TestLoadPluginManifest:
    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(PluginManifestError, match="not found"):
            load_plugin_manifest(tmp_path)

    def test_invalid_json(self, tmp_path: Path):
        (tmp_path / PLUGIN_MANIFEST_FILENAME).write_text("{nope")
        with pytest.raises(PluginManifestError, match="invalid JSON"):
            load_plugin_manifest(tmp_path)

    def test_error_includes_path(self, tmp_path: Path):
        (tmp_path / PLUGIN_MANIFEST_FILENAME).write_text(json.dumps({"id": "x"}))
        with pytest.raises(PluginManifestError, match=str(tmp_path)):
            load_plugin_manifest(tmp_path)

    def test_non_utf8_file(self, tmp_path: Path):
        (tmp_path / PLUGIN_MANIFEST_FILENAME).write_bytes(b'\xff\xfe{"id": "x"}')
        with pytest.raises(PluginManifestError, match="UTF-8"):
            load_plugin_manifest(tmp_path)

    def test_unreadable_file(self, tmp_path: Path):
        manifest_file = tmp_path / PLUGIN_MANIFEST_FILENAME
        manifest_file.write_text(json.dumps(_minimal_manifest()))
        manifest_file.chmod(0o000)
        try:
            if os.access(manifest_file, os.R_OK):
                pytest.skip("running as a user that ignores file permissions")
            with pytest.raises(PluginManifestError, match="cannot read"):
                load_plugin_manifest(tmp_path)
        finally:
            manifest_file.chmod(0o644)


class TestIterPluginManifests:
    def test_finds_nested_manifests(self):
        found = list(iter_plugin_manifests(PLUGINS_ROOT))
        ids = {load_plugin_manifest(p).id for p in found}
        assert {"claude", "hermes", "mastracode"} <= ids

    def test_single_file_path(self, tmp_path: Path):
        manifest_file = tmp_path / PLUGIN_MANIFEST_FILENAME
        manifest_file.write_text(json.dumps(_minimal_manifest()))
        assert list(iter_plugin_manifests(manifest_file)) == [manifest_file]

    def test_missing_path_raises(self, tmp_path: Path):
        with pytest.raises(PluginManifestError, match="no such file or directory"):
            list(iter_plugin_manifests(tmp_path / "does-not-exist"))

    def test_cli_fails_on_missing_path(self, tmp_path: Path):
        assert _main([str(PLUGINS_ROOT / "claude"), str(tmp_path / "does-not-exist")]) == 1


class TestSchemaSync:
    def test_checked_in_schema_matches_model(self):
        schema_path = REPO_ROOT / PLUGIN_MANIFEST_SCHEMA_PATH
        assert schema_path.exists(), f"{PLUGIN_MANIFEST_SCHEMA_PATH} missing — regenerate with --write-schema"
        on_disk = json.loads(schema_path.read_text())
        assert on_disk == manifest_json_schema()

    def test_schema_requires_spec_version(self):
        assert "specVersion" in manifest_json_schema()["required"]

    def test_schema_spec_version_is_const_one(self):
        assert manifest_json_schema()["properties"]["specVersion"]["const"] == 1

    @pytest.mark.parametrize(
        "path_value",
        [
            "hooks/h.py",
            "hooks\\h.py",
            ".claude-plugin/plugin.json",
            "docs/README.md",
            "a~b/c",
            "",
            "..",
            "a/../b",
            "a\\..\\b",
            "..\\..\\x",
            "\\Windows\\x",
            "C:/x",
            "C:x",
            "/etc/passwd",
            "~/x",
        ],
    )
    def test_schema_artifact_path_matches_loader(self, path_value: str):
        assert _schema_accepts(path_value) == _artifact_accepts(path_value), path_value


class TestRepoManifests:
    @pytest.mark.parametrize("plugin_id", ["claude", "hermes", "mastracode"])
    def test_existing_plugins_have_valid_manifests(self, plugin_id: str):
        manifest = load_plugin_manifest(PLUGINS_ROOT / plugin_id)
        assert manifest.id == plugin_id
        # Every declared artifact path must resolve to a real file in the plugin directory.
        for artifact in manifest.artifacts:
            artifact_path = PLUGINS_ROOT / plugin_id / artifact.path
            assert artifact_path.is_file(), f"{plugin_id}: artifact {artifact.path} is not a file"
