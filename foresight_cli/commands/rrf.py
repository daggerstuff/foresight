"""RRF weight tuning commands: show, set, save, reset."""

from __future__ import annotations

import json
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


@app.command()
def show(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
):
    """Show current RRF weights (from config file or defaults)."""
    config = get_rrf_config(config_path)
    data = config.to_dict()

    if out.get_settings().mode == "agent":
        out.print_json({"rrf_config": data, "config_path": str(DEFAULT_CONFIG_PATH)})
    else:
        out.result_block(data, title="RRF Configuration")


@app.command()
def set(
    key: str = typer.Argument(..., help="Weight key (e.g. keyword, tfidf_cosine, graph, temporal, rrf_k)"),
    value: float = typer.Argument(..., help="Weight value"),
    config_path: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
):
    """Set a single RRF weight and save to config file."""
    config = get_rrf_config(config_path)

    key_map = {
        "keyword": "keyword_weight",
        "tfidf_cosine": "tfidf_cosine_weight",
        "semantic": "tfidf_cosine_weight",
        "graph": "graph_weight",
        "temporal": "temporal_weight",
        "entity": "entity_weight",
        "rrf_k": "rrf_k",
    }

    attr = key_map.get(key, key)
    if not hasattr(config, attr):
        raise typer.BadParameter(f"Unknown key: {key}. Valid: {', '.join(key_map.keys())}")

    setattr(config, attr, value)
    save_rrf_config(config, config_path)

    if out.get_settings().mode == "agent":
        out.print_json({"set": {key: value}, "config_path": str(DEFAULT_CONFIG_PATH)})
    else:
        out.info(f"Set {key} = {value}")
        out.info(f"Saved to {DEFAULT_CONFIG_PATH}")
        out.info("Restart foresight service to apply.")


@app.command()
def save(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
):
    """Save current defaults to config file (creates file with default values)."""
    config = RRFConfig()
    save_rrf_config(config, config_path)

    if out.get_settings().mode == "agent":
        out.print_json({"saved": True, "config_path": str(DEFAULT_CONFIG_PATH)})
    else:
        out.info(f"Saved defaults to {DEFAULT_CONFIG_PATH}")


@app.command()
def reset(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Reset config file to defaults."""
    if not yes:
        confirm = typer.confirm("Reset RRF config to defaults?")
        if not confirm:
            raise typer.Abort()

    config = RRFConfig()
    save_rrf_config(config, config_path)

    if out.get_settings().mode == "agent":
        out.print_json({"reset": True, "config_path": str(DEFAULT_CONFIG_PATH)})
    else:
        out.info(f"Reset to defaults at {DEFAULT_CONFIG_PATH}")
