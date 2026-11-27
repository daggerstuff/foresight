"""Tests for the agent-plugin manifest contract (FOR-68)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from foresight.plugin_manifest import (
    EXIT_INVALID_MANIFEST,
    EXIT_NO_MANIFESTS,
    EXIT_OK,
    EXIT_UNREADABLE_PATH,
    EXIT_USAGE,
    PLUGIN_MANIFEST_FILENAME,
    PLUGIN_MANIFEST_SCHEMA_PATH,
    AgentTarget,
    PluginManifestError,
    PluginManifestReadError,
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


def _install_accepts(install: dict) -> bool:
    try:
        parse_plugin_manifest(_minimal_manifest(install=install))
    except PluginManifestError:
        return False
    return True


def _install_schema_accepts(install: dict) -> bool:
    branches = manifest_json_schema()["$defs"]["PluginInstall"].get("anyOf")
    assert branches, "PluginInstall.anyOf missing from the generated schema"
    for branch in branches:
        if all(
            isinstance(install.get(key), str) and len(install[key]) >= branch["properties"][key].get("minLength", 0)
            for key in branch["required"]
        ):
            return True
    return False


def _schema_accepts(path_value: str) -> bool:
    path_schema = _path_schema()
    if len(path_value) < path_schema.get("minLength", 0):
        return False
    pattern = path_schema.get("pattern")
    if pattern is not None and re.search(pattern, path_value) is None:
        return False
    forbidden = [rule["pattern"] for rule in path_schema["not"]["anyOf"]]
    return all(re.search(pattern, path_value) is None for pattern in forbidden)


def _source_accepts(source_value: str) -> bool:
    try:
        parse_plugin_manifest(
            _minimal_manifest(artifacts=[{"type": "source", "path": "hooks/x.py", "source": source_value}])
        )
    except PluginManifestError:
        return False
    return True


def _source_schema() -> dict:
    schema = manifest_json_schema()
    source = schema["$defs"]["PluginArtifact"]["properties"]["source"]
    assert source, "PluginArtifact.source missing from the generated schema"
    return source


def _source_schema_accepts(source_value: str) -> bool:
    source_schema = dict(_source_schema())
    for branch in source_schema.get("anyOf", []):
        if branch.get("type") != "null":
            for key, value in branch.items():
                source_schema.setdefault(key, value)
    if len(source_value) < source_schema.get("minLength", 0):
        return False
    pattern = source_schema.get("pattern")
    if pattern is not None and re.search(pattern, source_value) is None:
        return False
    forbidden = [rule["pattern"] for rule in source_schema["not"]["anyOf"]]
    return all(re.search(pattern, source_value) is None for pattern in forbidden)


def _version_accepts(version_value: str) -> bool:
    try:
        parse_plugin_manifest(_minimal_manifest(version=version_value))
    except PluginManifestError:
        return False
    return True


def _version_schema_accepts(version_value: str) -> bool:
    version_schema = manifest_json_schema()["properties"]["version"]
    pattern = version_schema.get("pattern")
    return pattern is None or re.search(pattern, version_value) is not None


class TestParsePluginManifest:
    def test_non_object_root_raises_manifest_error(self):
        for data in (None, [], "x", 1):
            with pytest.raises(PluginManifestError):
                parse_plugin_manifest(data)

    def test_null_manifest_file_raises_manifest_error(self, tmp_path):
        manifest_path = tmp_path / PLUGIN_MANIFEST_FILENAME
        manifest_path.write_text("null", encoding="utf-8")
        with pytest.raises(PluginManifestError):
            load_plugin_manifest(tmp_path)

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

    def test_rejects_home_relative_tilde_path(self):
        with pytest.raises(PluginManifestError, match="home-relative"):
            parse_plugin_manifest(_minimal_manifest(artifacts=[{"type": "hook", "path": "~alice/hook.py"}]))

    def test_accepts_nested_tilde_filename(self):
        manifest = parse_plugin_manifest(
            _minimal_manifest(artifacts=[{"type": "hook", "path": "assets/~backup/hook.py"}])
        )
        assert manifest.artifacts[0].path == "assets/~backup/hook.py"

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

    @pytest.mark.parametrize("version_value", ["1\u0661.2.3", "1.2.\u0663", "\u0661.2.3"])
    def test_rejects_non_ascii_digits(self, version_value: str):
        assert _version_accepts(version_value) is False
        assert _version_schema_accepts(version_value) is False

    @pytest.mark.parametrize("version_value", ["11.2.3", "1.2.3-rc.1+build.5", "0.0.0"])
    def test_accepts_ascii_digits(self, version_value: str):
        assert _version_accepts(version_value) is True
        assert _version_schema_accepts(version_value) is True

    @pytest.mark.parametrize("bad_version", ["1.0.0-01", "1.0.0-00.1", "1.0.0-a..b", "1.0.0+build..5"])
    def test_rejects_invalid_prerelease_identifiers(self, bad_version: str):
        with pytest.raises(PluginManifestError):
            parse_plugin_manifest(_minimal_manifest(version=bad_version))

    @pytest.mark.parametrize("good_version", ["0.0.1", "1.2.3-alpha", "1.2.3-alpha.1", "1.2.3-0.3.7", "1.2.3+build.5"])
    def test_accepts_valid_prerelease_identifiers(self, good_version: str):
        assert parse_plugin_manifest(_minimal_manifest(version=good_version)).version == good_version

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
        code = _main([str(PLUGINS_ROOT / "claude"), str(tmp_path / "does-not-exist")])
        assert code == EXIT_UNREADABLE_PATH

    def test_cli_does_not_count_missing_path_as_manifest(self, tmp_path: Path, capsys):
        code = _main([str(PLUGINS_ROOT / "claude"), str(tmp_path / "does-not-exist")])
        assert code == EXIT_UNREADABLE_PATH
        err = capsys.readouterr().err
        assert "1 path(s) could not be read" in err
        assert "manifest(s) invalid" not in err

    def test_cli_missing_paths_only(self, tmp_path: Path, capsys):
        code = _main([str(tmp_path / "does-not-exist")])
        assert code == EXIT_UNREADABLE_PATH
        err = capsys.readouterr().err
        assert "1 path(s) could not be read" in err
        assert "no foresight.plugin.json files found" not in err

    def test_cli_fails_on_empty_directory(self, tmp_path: Path, capsys):
        empty = tmp_path / "empty"
        empty.mkdir()
        code = _main([str(PLUGINS_ROOT / "claude"), str(empty)])
        assert code == EXIT_NO_MANIFESTS
        err = capsys.readouterr().err
        assert f"FAIL {empty}: no foresight.plugin.json files found" in err
        assert "manifest(s) invalid" not in err

    def test_cli_empty_directory_only(self, tmp_path: Path, capsys):
        empty = tmp_path / "empty"
        empty.mkdir()
        code = _main([str(empty)])
        assert code == EXIT_NO_MANIFESTS
        err = capsys.readouterr().err
        assert "1 path(s) contained no manifests" in err

    def test_cli_invalid_manifest_exits_1(self, tmp_path: Path):
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / PLUGIN_MANIFEST_FILENAME).write_text(json.dumps(_minimal_manifest(version="nope")))
        code = _main([str(bad)])
        assert code == EXIT_INVALID_MANIFEST

    def test_cli_validation_outranks_discovery_failures(self, tmp_path: Path):
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / PLUGIN_MANIFEST_FILENAME).write_text(json.dumps(_minimal_manifest(version="nope")))
        code = _main([str(bad), str(tmp_path / "does-not-exist")])
        assert code == EXIT_INVALID_MANIFEST

    def test_load_manifest_reports_read_error(self, tmp_path: Path):
        plugin = tmp_path / "unreadable"
        plugin.mkdir()
        (plugin / "foresight.plugin.json").mkdir()
        with pytest.raises(PluginManifestReadError, match="cannot read"):
            load_plugin_manifest(plugin)

    def test_load_manifest_missing_file_raises_read_error(self, tmp_path: Path):
        plugin = tmp_path / "empty"
        plugin.mkdir()
        with pytest.raises(PluginManifestReadError, match="file not found"):
            load_plugin_manifest(plugin)

    def test_cli_unreadable_manifest_exits_2(self, tmp_path: Path):
        plugin = tmp_path / "unreadable"
        plugin.mkdir()
        (plugin / "foresight.plugin.json").mkdir()
        assert _main([str(plugin)]) == EXIT_UNREADABLE_PATH

    def test_cli_invalid_outranks_unreadable_manifest(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "foresight.plugin.json").write_text("{", encoding="utf-8")
        unreadable = tmp_path / "unreadable"
        unreadable.mkdir()
        (unreadable / "foresight.plugin.json").mkdir()
        code = _main([str(broken), str(unreadable)])
        captured = capsys.readouterr()
        assert code == EXIT_INVALID_MANIFEST
        assert "1 path(s) could not be read" in captured.err
        assert "1/2 manifest(s) invalid" in captured.err

    def test_cli_unreadable_path_outranks_empty_directory(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        code = _main([str(empty), str(tmp_path / "does-not-exist")])
        assert code == EXIT_UNREADABLE_PATH

    def test_cli_every_requested_path_has_manifests(self, capsys):
        code = _main([str(PLUGINS_ROOT / "claude"), str(PLUGINS_ROOT / "hermes")])
        assert code == EXIT_OK
        err = capsys.readouterr().err
        assert "no manifests" not in err

    def test_cli_usage_error_exits_64(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            _main(["--not-a-flag"])
        assert excinfo.value.code == EXIT_USAGE
        assert "usage:" in capsys.readouterr().err

    def test_cli_missing_paths_exit_64(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            _main([])
        assert excinfo.value.code == EXIT_USAGE
        assert "at least one path is required" in capsys.readouterr().err


class TestArtifactSource:
    def test_source_rejects_windows_absolute_and_home_paths(self):
        for bad in ("C:/x.py", "C:x.py", "\\\\server\\x.py", "~/x.py", "~alice/x.py"):
            data = _minimal_manifest(artifacts=[{"type": "source", "path": "hooks/x.py", "source": bad}])
            with pytest.raises(PluginManifestError):
                parse_plugin_manifest(data)

    def test_source_rejects_absolute_and_parent_paths(self):
        for bad in ("/etc/passwd", "../sdk/python/x.py"):
            data = _minimal_manifest(artifacts=[{"type": "source", "path": "hooks/x.py", "source": bad}])
            with pytest.raises(PluginManifestError):
                parse_plugin_manifest(data)

    def test_source_accepts_a_repo_relative_path(self):
        data = _minimal_manifest(
            artifacts=[
                {
                    "type": "source",
                    "path": "hooks/x.py",
                    "source": "plugins/sdk/python/x.py",
                }
            ]
        )
        manifest = parse_plugin_manifest(data)
        assert manifest.artifacts[0].source == "plugins/sdk/python/x.py"


class TestSchemaSync:
    def test_checked_in_schema_matches_model(self):
        schema_path = REPO_ROOT / PLUGIN_MANIFEST_SCHEMA_PATH
        assert schema_path.exists(), f"{PLUGIN_MANIFEST_SCHEMA_PATH} missing — regenerate with --write-schema"
        on_disk = json.loads(schema_path.read_text())
        assert on_disk == manifest_json_schema()

    @pytest.mark.parametrize(
        "version_value",
        ["0.1.0", "1.2.3", "1.2.3-rc.1+build.5", "latest", "v1.0.0", "1.0", "1.0.0-", "1.0.0-01", "1.0.0-a..b"],
    )
    def test_schema_version_matches_loader(self, version_value: str):
        assert _version_schema_accepts(version_value) == _version_accepts(version_value), version_value

    def test_schema_requires_spec_version(self):
        assert "specVersion" in manifest_json_schema()["required"]

    def test_schema_spec_version_is_const_one(self):
        assert manifest_json_schema()["properties"]["specVersion"]["const"] == 1

    def test_schema_install_branches_are_not_shared(self):
        schema = manifest_json_schema()
        branch = schema["$defs"]["PluginInstall"]["anyOf"][0]
        branch["required"].append("injected")
        branch["properties"]["command"]["type"] = "number"
        fresh = manifest_json_schema()["$defs"]["PluginInstall"]["anyOf"][0]
        assert fresh["required"] == ["command"]
        assert fresh["properties"]["command"]["type"] == "string"

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
            "~alice/hook.py",
            "assets/~backup/hook.py",
            "assets\\~backup\\hook.py",
        ],
    )
    def test_schema_artifact_path_matches_loader(self, path_value: str):
        assert _schema_accepts(path_value) == _artifact_accepts(path_value), path_value

    @pytest.mark.parametrize(
        "source_value",
        [
            "plugins/sdk/python/foresight_plugin_sdk.py",
            "plugins/sdk/python/nested/dir/x.py",
            "",
            "..",
            "../outside.py",
            "a/../b",
            "/etc/passwd",
            "~/x",
            "~alice/x",
            "C:/x",
            "C:x",
            "\\Windows\\x",
        ],
    )
    def test_schema_artifact_source_matches_loader(self, source_value: str):
        assert _source_schema_accepts(source_value) == _source_accepts(source_value), source_value

    @pytest.mark.parametrize(
        "install",
        [
            {"command": "bash install.sh"},
            {"notes": "Install via /plugins UI"},
            {"command": "bash install.sh", "notes": "or the UI"},
            {"dryRunFlag": "--dry-run"},
            {"command": None, "notes": None},
            {"command": ""},
        ],
    )
    def test_schema_install_matches_loader(self, install: dict):
        assert _install_schema_accepts(install) == _install_accepts(install), install


class TestDumpRoundTrip:
    def test_model_dump_emits_aliases(self):
        manifest = parse_plugin_manifest(_minimal_manifest(install={"notes": "manual"}))
        dumped = manifest.model_dump()
        assert dumped["specVersion"] == 1
        assert "spec_version" not in dumped
        assert parse_plugin_manifest(dumped).install is not None

    def test_model_dump_emits_nested_aliases(self):
        manifest = parse_plugin_manifest(
            _minimal_manifest(
                credentials={"envFile": "~/.config/foresight/credentials.env"},
                install={"command": "bash install.sh", "dryRunFlag": "--dry-run"},
            )
        )
        dumped = manifest.model_dump()
        assert dumped["credentials"]["envFile"] == "~/.config/foresight/credentials.env"
        assert "env_file" not in dumped["credentials"]
        assert dumped["install"]["dryRunFlag"] == "--dry-run"
        assert "dry_run_flag" not in dumped["install"]

    def test_model_dump_json_round_trips(self):
        manifest = parse_plugin_manifest(_minimal_manifest(version="1.2.3"))
        assert parse_plugin_manifest(json.loads(manifest.model_dump_json())).version == "1.2.3"

    def test_model_dump_python_names_on_request(self):
        manifest = parse_plugin_manifest(_minimal_manifest())
        assert manifest.model_dump(by_alias=False)["spec_version"] == 1


class TestRepoManifests:
    @pytest.mark.parametrize("plugin_id", ["claude", "hermes", "mastracode"])
    def test_existing_plugins_have_valid_manifests(self, plugin_id: str):
        manifest = load_plugin_manifest(PLUGINS_ROOT / plugin_id)
        assert manifest.id == plugin_id
        # Every declared artifact path must resolve to a real file in the plugin directory.
        for artifact in manifest.artifacts:
            if artifact.source is not None:
                located = REPO_ROOT / artifact.source
            else:
                located = PLUGINS_ROOT / plugin_id / artifact.path
            assert located.is_file(), f"{plugin_id}: artifact {artifact.path} is not a file"

    def test_claude_manifest_declares_the_shared_python_sdk(self):
        manifest = load_plugin_manifest(PLUGINS_ROOT / "claude")
        sources = {artifact.source for artifact in manifest.artifacts}
        assert "plugins/sdk/python/foresight_plugin_sdk.py" in sources
