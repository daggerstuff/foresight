"""RRF weight tuning commands: show, set, save, reset."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from foresight.rrf_tuning import (
    DEFAULT_CONFIG_PATH,
    RRFConfig,
    get_rrf_config,
    save_rrf_config,
)
from foresight_cli.parser import CommandSpec, UsageError
from foresight_cli.utils import output as out

KEY_MAP = {
    "keyword": "keyword_weight",
    "tfidf_cosine": "tfidf_cosine_weight",
    "semantic": "tfidf_cosine_weight",
    "graph": "graph_weight",
    "temporal": "temporal_weight",
    "entity": "entity_weight",
    "rrf_k": "rrf_k",
    "trend_mod_strengthening": "trend_mod_strengthening",
    "trend_mod_stable": "trend_mod_stable",
    "trend_mod_weakening": "trend_mod_weakening",
    "trend_mod_stale": "trend_mod_stale",
    "category_mult_session": "category_mult_session",
    "category_mult_fact": "category_mult_fact",
    "category_mult_preference": "category_mult_preference",
    "category_mult_trait": "category_mult_trait",
}

VALID_KEYS = set(KEY_MAP.keys()) | {f for f in RRFConfig.__dataclass_fields__ if not f.startswith("_")}


class RrfShowParams(BaseModel):
    """Arguments for :func:`show`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    config_path: str | None = Field(default=None, description="Config file path")


class RrfSetParams(BaseModel):
    """Arguments for :func:`set_weight`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    key: str = Field(description="Weight key (e.g. keyword, tfidf_cosine, graph, temporal, rrf_k)")
    value: float = Field(description="Weight value")
    config_path: str | None = Field(default=None, description="Config file path")


class RrfSaveParams(BaseModel):
    """Arguments for :func:`save`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    config_path: str | None = Field(default=None, description="Config file path")


class RrfResetParams(BaseModel):
    """Arguments for :func:`reset`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    config_path: str | None = Field(default=None, description="Config file path")
    yes: bool = Field(default=False, description="Skip confirmation")


def _resolve_path(config_path: str | None) -> Path:
    return Path(config_path) if config_path else DEFAULT_CONFIG_PATH


def _emit_agent(payload: dict) -> None:
    sys.stdout.write(f"[JSON] {json.dumps(payload)}\n")
    sys.stdout.flush()


def _confirm(message: str) -> bool:
    try:
        answer = input(f"{message} [y/N]: ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _validate_weight(key: str, value: float) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise UsageError(f"Value for {key} must be a finite number, got: {value!r}")
    if key == "rrf_k" and value <= 0:
        raise UsageError(f"rrf_k must be > 0 (got {value}); non-positive values cause ZeroDivisionError at runtime")
    if key != "rrf_k" and value < 0:
        raise UsageError(f"Weight {key} must be >= 0 (got {value}); negative weights invert ranking")


def show(params: RrfShowParams) -> None:
    """Show current RRF weights (from config file or defaults)."""
    resolved = _resolve_path(params.config_path)
    config = get_rrf_config(params.config_path)
    data = config.to_dict()
    agent_mode = out.get_settings().mode == "agent"
    if agent_mode:
        _emit_agent({"rrf_config": data, "config_path": str(resolved)})
    elif out.get_settings().mode == "json":
        sys.stdout.write(json.dumps({"rrf_config": data, "config_path": str(resolved)}) + "\n")
        sys.stdout.flush()
    else:
        out.result_block(data, title="RRF Configuration")
        out.info(f"Config path: {resolved}")


def set_weight(params: RrfSetParams) -> None:
    """Set a single RRF weight and save to config file."""
    resolved = _resolve_path(params.config_path)
    if params.key not in VALID_KEYS:
        raise UsageError(f"Unknown key: {params.key}. Valid keys: {', '.join(sorted(KEY_MAP.keys()))}")
    _validate_weight(params.key, params.value)

    config = get_rrf_config(params.config_path)
    attr = KEY_MAP.get(params.key, params.key)
    if attr not in RRFConfig.__dataclass_fields__:
        raise UsageError(f"Unknown attribute: {attr}")
    setattr(config, attr, params.value)
    save_rrf_config(config, params.config_path)

    if out.get_settings().mode == "agent":
        _emit_agent({"set": {params.key: params.value}, "config_path": str(resolved)})
    elif out.get_settings().mode == "json":
        sys.stdout.write(json.dumps({"set": {params.key: params.value}, "config_path": str(resolved)}) + "\n")
        sys.stdout.flush()
    else:
        out.info(f"Set {params.key} = {params.value}")
        out.info(f"Saved to {resolved}")
        out.info("Restart foresight service to apply.")


def save(params: RrfSaveParams) -> None:
    """Save current defaults to config file (creates file with default values)."""
    resolved = _resolve_path(params.config_path)
    config = RRFConfig()
    save_rrf_config(config, params.config_path)

    if out.get_settings().mode == "agent":
        _emit_agent({"saved": True, "config_path": str(resolved)})
    elif out.get_settings().mode == "json":
        sys.stdout.write(json.dumps({"saved": True, "config_path": str(resolved)}) + "\n")
        sys.stdout.flush()
    else:
        out.info(f"Saved defaults to {resolved}")


def reset(params: RrfResetParams) -> None:
    """Reset config file to defaults."""
    resolved = _resolve_path(params.config_path)
    if (
        not params.yes
        and out.get_settings().mode not in ("agent", "json")
        and not _confirm("Reset RRF config to defaults?")
    ):
        sys.stderr.write("Aborted.\n")
        raise SystemExit(1)

    config = RRFConfig()
    save_rrf_config(config, params.config_path)

    if out.get_settings().mode == "agent":
        _emit_agent({"reset": True, "config_path": str(resolved)})
    elif out.get_settings().mode == "json":
        sys.stdout.write(json.dumps({"reset": True, "config_path": str(resolved)}) + "\n")
        sys.stdout.flush()
    else:
        out.info(f"Reset to defaults at {resolved}")


def _config_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", "-c", dest="config_path", default=None, help="Config file path")


def _set_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("key", help="Weight key (e.g. keyword, tfidf_cosine, graph, temporal, rrf_k)")
    parser.add_argument("value", type=float, help="Weight value")
    parser.add_argument("--config", "-c", dest="config_path", default=None, help="Config file path")


def _reset_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", "-c", dest="config_path", default=None, help="Config file path")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")


COMMANDS = {
    "show": CommandSpec(show, RrfShowParams, _config_parser),
    "set": CommandSpec(set_weight, RrfSetParams, _set_parser),
    "save": CommandSpec(save, RrfSaveParams, _config_parser),
    "reset": CommandSpec(reset, RrfResetParams, _reset_parser),
}
