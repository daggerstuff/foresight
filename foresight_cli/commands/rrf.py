"""RRF weight tuning commands: show, set, save, reset."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import typer

from foresight.rrf_tuning import (
    DEFAULT_CONFIG_PATH,
    RRFConfig,
    get_rrf_config,
    save_rrf_config,
)
from foresight_cli.utils import output as out

app = typer.Typer(help="View and tune RRF (Reciprocal Rank Fusion) weights.")

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


def _resolve_path(config_path: str | None) -> Path:
    return Path(config_path) if config_path else DEFAULT_CONFIG_PATH


def _emit_agent(payload: dict) -> None:
    sys.stdout.write(f"[JSON] {json.dumps(payload)}\n")
    sys.stdout.flush()


def _validate_weight(key: str, value: float) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise typer.BadParameter(f"Value for {key} must be a finite number, got: {value!r}")
    if key == "rrf_k" and value <= 0:
        raise typer.BadParameter(
            f"rrf_k must be > 0 (got {value}); non-positive values cause ZeroDivisionError at runtime"
        )
    if key != "rrf_k" and value < 0:
        raise typer.BadParameter(f"Weight {key} must be >= 0 (got {value}); negative weights invert ranking")


@app.command()
def show(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
):
    """Show current RRF weights (from config file or defaults)."""
    resolved = _resolve_path(config_path)
    config = get_rrf_config(config_path)
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


@app.command()
def set(
    key: str = typer.Argument(..., help="Weight key (e.g. keyword, tfidf_cosine, graph, temporal, rrf_k)"),
    value: float = typer.Argument(..., help="Weight value"),
    config_path: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
):
    """Set a single RRF weight and save to config file."""
    resolved = _resolve_path(config_path)
    if key not in VALID_KEYS:
        raise typer.BadParameter(f"Unknown key: {key}. Valid keys: {', '.join(sorted(KEY_MAP.keys()))}")
    _validate_weight(key, value)

    config = get_rrf_config(config_path)
    attr = KEY_MAP.get(key, key)
    if attr not in RRFConfig.__dataclass_fields__:
        raise typer.BadParameter(f"Unknown attribute: {attr}")
    setattr(config, attr, value)
    save_rrf_config(config, config_path)

    if out.get_settings().mode == "agent":
        _emit_agent({"set": {key: value}, "config_path": str(resolved)})
    elif out.get_settings().mode == "json":
        sys.stdout.write(json.dumps({"set": {key: value}, "config_path": str(resolved)}) + "\n")
        sys.stdout.flush()
    else:
        out.info(f"Set {key} = {value}")
        out.info(f"Saved to {resolved}")
        out.info("Restart foresight service to apply.")


@app.command()
def save(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
):
    """Save current defaults to config file (creates file with default values)."""
    resolved = _resolve_path(config_path)
    config = RRFConfig()
    save_rrf_config(config, config_path)

    if out.get_settings().mode == "agent":
        _emit_agent({"saved": True, "config_path": str(resolved)})
    elif out.get_settings().mode == "json":
        sys.stdout.write(json.dumps({"saved": True, "config_path": str(resolved)}) + "\n")
        sys.stdout.flush()
    else:
        out.info(f"Saved defaults to {resolved}")


@app.command()
def reset(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Reset config file to defaults."""
    resolved = _resolve_path(config_path)
    if not yes and out.get_settings().mode not in ("agent", "json"):
        confirm = typer.confirm("Reset RRF config to defaults?")
        if not confirm:
            raise typer.Abort()

    config = RRFConfig()
    save_rrf_config(config, config_path)

    if out.get_settings().mode == "agent":
        _emit_agent({"reset": True, "config_path": str(resolved)})
    elif out.get_settings().mode == "json":
        sys.stdout.write(json.dumps({"reset": True, "config_path": str(resolved)}) + "\n")
        sys.stdout.flush()
    else:
        out.info(f"Reset to defaults at {resolved}")
