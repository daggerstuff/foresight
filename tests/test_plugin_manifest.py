"""Tests for the agent-plugin manifest contract (FOR-68)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foresight.plugin_manifest import (
    PLUGIN_MANIFEST_FILENAME,
    PLUGIN_MANIFEST_SCHEMA_PATH,
    AgentTarget,
    PluginManifestError,
    PluginSurface,
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
        "requires": {"foresight": ">=0.19.0", "transports": ["http"]},
    }
    data.update(overrides)
    return data


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

    def test_rejects_extra_fields(self):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(unexpectedField=True))

    def test_rejects_absolute_artifact_path(self):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(artifacts=[{"type": "hook", "path": "/etc/passwd"}]))

    def test_rejects_parent_artifact_path(self):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(artifacts=[{"type": "hook", "path": "../escape.sh"}]))

    def test_empty_targets_rejected(self):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(targets=[]))

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


class TestIterPluginManifests:
    def test_finds_nested_manifests(self):
        found = list(iter_plugin_manifests(PLUGINS_ROOT))
        ids = {load_plugin_manifest(p).id for p in found}
        assert {"claude", "hermes", "mastracode"} <= ids

    def test_single_file_path(self, tmp_path: Path):
        manifest_file = tmp_path / PLUGIN_MANIFEST_FILENAME
        manifest_file.write_text(json.dumps(_minimal_manifest()))
        assert list(iter_plugin_manifests(manifest_file)) == [manifest_file]


class TestSchemaSync:
    def test_checked_in_schema_matches_model(self):
        schema_path = REPO_ROOT / PLUGIN_MANIFEST_SCHEMA_PATH
        assert schema_path.exists(), f"{PLUGIN_MANIFEST_SCHEMA_PATH} missing — regenerate with --write-schema"
        on_disk = json.loads(schema_path.read_text())
        assert on_disk == manifest_json_schema()


class TestRepoManifests:
    @pytest.mark.parametrize("plugin_id", ["claude", "hermes", "mastracode"])
    def test_existing_plugins_have_valid_manifests(self, plugin_id: str):
        manifest = load_plugin_manifest(PLUGINS_ROOT / plugin_id)
        assert manifest.id == plugin_id
        # Every declared artifact path must exist in the plugin directory.
        for artifact in manifest.artifacts:
            assert (PLUGINS_ROOT / plugin_id / artifact.path).exists(), (
                f"{plugin_id}: artifact {artifact.path} does not exist"
            )
