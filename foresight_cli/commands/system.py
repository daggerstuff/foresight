"""System commands: status, init, doctor, stats, config, history."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import redis as _redis
from pydantic import BaseModel, ConfigDict, Field

from foresight.config import raw_retention_enabled
from foresight.context_cache import get_context_cache
from foresight.server import (
    GetSystemStatusParams,
    MaintenanceAction,
    QueryMemoriesTemporalParams,
    TemporalWindow,
    _initialize_backend,
    _parse_as_of,
    get_backend,
    get_decay_events,
    get_system_status,
    init_db,
    query_memories_temporal,
    reprocess_raw_events,
    run_maintenance,
    switch_tenant,
)
from foresight.telemetry import get_telemetry_store
from foresight_cli.parser import CommandSpec
from foresight_cli.utils import config as cfg, output as out


class SystemStatusParams(BaseModel):
    """Arguments for :func:`status`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    user_id: str | None = Field(default=None, description="User ID override")


class SystemInitParams(BaseModel):
    """Arguments for :func:`init`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    force: bool = Field(default=False, description="Overwrite existing config")
    user_id: str | None = Field(default=None, description="Default user ID")
    bank_id: str | None = Field(default=None, description="Default bank ID")


class SystemDoctorParams(BaseModel):
    """Arguments for :func:`doctor`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    user_id: str | None = Field(default=None, description="User ID override")


class SystemStatsParams(BaseModel):
    """Arguments for :func:`stats`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    user_id: str | None = Field(default=None, description="User ID override")


class SystemTemporalParams(BaseModel):
    """Arguments for :func:`temporal`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    window: str = Field(default="week", description="Time window: today, week, month, year")
    trend: str | None = Field(default=None, description="Trend filter: stable, strengthening, weakening, stale")
    category: str | None = Field(default=None, description="Category filter")
    limit: int = Field(default=50, description="Max results")
    as_of: str | None = Field(default=None, description="ISO-8601 instant for bitemporal reconstruction")
    user_id: str | None = Field(default=None, description="User ID override")


class SystemConfigParams(BaseModel):
    """Arguments for :func:`config`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    key: str | None = Field(default=None, description="Config key to view/set (e.g. user_id, db_url, bank_id)")
    value: str | None = Field(default=None, description="Value to set (omit to view current)")
    reset: bool = Field(default=False, description="Reset config to defaults")
    user_id: str | None = Field(default=None, description="User ID override")


class SystemHistoryParams(BaseModel):
    """Arguments for :func:`history`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    limit: int = Field(default=10, description="Number of events")
    user_id: str | None = Field(default=None, description="User ID override")


class SystemMaintenanceParams(BaseModel):
    """Arguments for :func:`maintenance`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    mode: str = Field(
        default="all",
        description="Comma-separated modes (consolidate,contradict,archive_stale,synthesize,reprocess) or 'all'",
    )
    batch_size: int = Field(default=200, description="Max memories to process per mode")
    max_runtime: float = Field(default=300, description="Wall-clock budget in seconds")
    reprocess_window_start: str | None = Field(default=None, description="ISO-8601 start of reprocess window")
    reprocess_window_end: str | None = Field(default=None, description="ISO-8601 end of reprocess window")
    user_id: str | None = Field(default=None, description="User ID override")


class SystemReprocessParams(BaseModel):
    """Arguments for :func:`reprocess`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    window_start: str | None = Field(default=None, description="ISO-8601 start of the reprocess window (inclusive)")
    window_end: str | None = Field(default=None, description="ISO-8601 end of the reprocess window (inclusive)")
    user_id: str | None = Field(default=None, description="User ID override")


class SystemTenantParams(BaseModel):
    """Arguments for :func:`tenant`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    tenant_id: str = Field(description="Tenant ID to switch to")


def _init_backend() -> None:
    """Initialize DB schema and backend (ensures Postgres routing works in CLI)."""
    config = cfg.ensure_config()
    if not os.environ.get("FORESIGHT_DB_URL") and config.db_url:
        os.environ["FORESIGHT_DB_URL"] = config.db_url
    if not os.environ.get("FORESIGHT_USER_ID") and config.user_id:
        os.environ["FORESIGHT_USER_ID"] = config.user_id
    if not os.environ.get("FORESIGHT_BANK_ID") and config.bank_id:
        os.environ["FORESIGHT_BANK_ID"] = config.bank_id
    init_db()
    _initialize_backend()


def _emit_status_json(result: Any) -> None:
    """Print the status payload as JSON, or raw text when it is not JSON."""
    if isinstance(result, str):
        try:
            out.print_json(json.loads(result))
        except json.JSONDecodeError:
            out.stdout(result)
    else:
        out.print_json(result)


def _status_payload(result: Any) -> dict[str, Any] | None:
    """Normalise the status payload to a dict, printing it raw when it is not JSON."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            out.stdout(result)
            return None
    if isinstance(result, dict):
        return result
    out.stdout(str(result))
    return None


def _status_pairs(result: dict[str, Any], resolved_uid: str | None) -> list[tuple[str, str]]:
    """Build the headline key/value rows for the status table."""
    pairs = [
        ("Status", result.get("status", result.get("health", "unknown"))),
        ("Database", result.get("database", result.get("db_url", "?"))),
        ("Bank ID", result.get("bank_id", "?")),
        ("User ID", result.get("user_id", resolved_uid or "?")),
        ("Memory Count", str(result.get("memory_count", result.get("count", 0)))),
        ("Crisis Signals", str(result.get("crisis_signals", 0))),
    ]
    stale_count = result.get("stale_count")
    if stale_count is not None:
        pairs.append(("Stale Memories", str(stale_count)))

    maint = result.get("maintenance_stats")
    if isinstance(maint, dict):
        pairs.append(("Maintenance Events", str(maint.get("total_events", 0))))
        last_run = maint.get("last_maintenance_run", "")
        if last_run:
            pairs.append(("Last Maintenance", str(last_run)[:19]))

    lct = result.get("last_capture_time")
    if lct:
        pairs.append(("Last Capture", str(lct)[:19]))

    return pairs


def _emit_counter_bullets(source: Any, title: str) -> None:
    """Print a ``key: value`` bullet list for a mapping payload section."""
    if isinstance(source, dict) and source:
        out.bullet_list([f"{k}: {v}" for k, v in source.items()], title=title)


def _emit_bullet_items(items: list[str], title: str) -> None:
    """Print a bullet list when there is at least one item."""
    if items:
        out.bullet_list(items, title=title)


def _maintenance_detail_items(maint: dict[str, Any]) -> list[str]:
    """Format the per-outcome maintenance counters that are present."""
    items = []
    for key in (
        "duplicates_auto_consolidated",
        "duplicates_flagged_review",
        "contradictions_flagged_review",
        "stale_archived",
        "insights_generated",
        "errors",
        "reviews_flagged",
    ):
        value = maint.get(key)
        if value is not None:
            items.append(f"{key.replace('_', ' ').title()}: {value}")
    return items


def _payload_budget_items(budget: Any) -> list[str]:
    """Format the injection payload-budget section."""
    if not isinstance(budget, dict):
        return []
    items = [f"Min Lane Chars: {budget.get('min_lane_chars', '?')}"]
    weights = budget.get("default_lane_weights", {})
    if isinstance(weights, dict):
        for lane_name, pct in sorted(weights.items()):
            items.append(f"{lane_name}: {pct * 100:.0f}%")
    return items


def _last_injection_items(inj: Any) -> list[str]:
    """Format the last-injection section."""
    if not isinstance(inj, dict):
        return []
    items = [
        f"Last Run: {str(inj.get('last_run_at', '?'))[:19]}",
        f"Latency: {inj.get('latency_ms', '?')}ms",
        f"Fetched: {inj.get('memories_fetched', '?')}",
        f"Returned: {inj.get('memories_returned', '?')}",
    ]
    fps = inj.get("fast_path")
    if fps is not None:
        items.append(f"Fast Path: {fps}")
    mc = inj.get("max_chars")
    if mc is not None:
        items.append(f"Budget: {mc} chars")
    return items


def _injection_health_items(ih: Any) -> list[str]:
    """Format the injection-health section."""
    if not isinstance(ih, dict):
        return []
    items = []
    tr = ih.get("total_runs")
    if tr is not None:
        items.append(f"Total Runs: {tr}")
    r24 = ih.get("runs_24h")
    if r24 is not None:
        items.append(f"Runs 24h: {r24}")
    lat = ih.get("avg_latency_ms")
    if lat is not None:
        items.append(f"Avg Latency: {lat}ms")
    ret = ih.get("avg_memories_returned")
    if ret is not None:
        items.append(f"Avg Returned: {ret}")
    fet = ih.get("avg_memories_fetched")
    if fet is not None:
        items.append(f"Avg Fetched: {fet}")
    fpr = ih.get("fast_path_rate_pct")
    if fpr is not None:
        items.append(f"Fast Path Rate: {fpr:.0f}%")
    lra = ih.get("last_run_at")
    if lra is not None:
        items.append(f"Last Run: {str(lra)[:19]}")
    return items


def _retrieval_debug_items(cache: Any) -> list[str]:
    """Format the retrieval-debug counters carried by the cache metrics."""
    if not isinstance(cache, dict):
        return []
    debug = cache.get("retrieval_debug", {})
    if not isinstance(debug, dict):
        return []
    return [f"{k}: {v}" for k, v in debug.items()]


def _emit_status_sections(result: dict[str, Any]) -> None:
    """Print the optional status sections in their reporting order."""
    maint = result.get("maintenance_stats")
    _emit_counter_bullets(result.get("by_scope"), "Memories by Scope")
    _emit_counter_bullets(result.get("by_category"), "Memories by Category")
    if isinstance(maint, dict):
        _emit_bullet_items(_maintenance_detail_items(maint), "Maintenance Details")
        _emit_counter_bullets(maint.get("by_type"), "Maintenance by Type")
    _emit_bullet_items(_payload_budget_items(result.get("payload_budget")), "Payload Budget")
    _emit_bullet_items(_last_injection_items(result.get("last_injection")), "Last Injection")
    _emit_bullet_items(_injection_health_items(result.get("injection_health")), "Injection Health")
    _emit_bullet_items(_retrieval_debug_items(result.get("cache_metrics")), "Retrieval Debug")


def status(params: SystemStatusParams) -> None:
    """Get Foresight system status and health."""
    _init_backend()
    resolved_uid = cfg.get_user_id(params.user_id)
    result = get_system_status(GetSystemStatusParams(user_id=resolved_uid))

    if out.get_settings().mode in ("agent", "json"):
        _emit_status_json(result)
        return

    payload = _status_payload(result)
    if payload is None:
        return

    out.kv_table(_status_pairs(payload, resolved_uid), title="Foresight Status")
    _emit_status_sections(payload)


def init(params: SystemInitParams) -> None:
    """Initialize Foresight (config, DB, directories)."""
    config_dir = cfg.CONFIG_DIR

    if config_dir.exists() and not params.force:
        out.info(f"Foresight config directory exists at {config_dir}")
        out.info("Use --force to reinitialize")
    else:
        config_dir.mkdir(parents=True, exist_ok=True)
        cfg.chmod_best_effort(config_dir, 0o700)
        out.done(f"Created config directory: {config_dir}")

    config = cfg.CliConfig.load()
    if params.user_id:
        config.user_id = params.user_id
    if params.bank_id:
        config.bank_id = params.bank_id
    config.save()
    out.done(f"Config saved: {cfg.CONFIG_PATH}")

    try:
        _init_backend()
        out.done("Database initialized")
    except Exception as e:
        out.warn(f"Database init issue: {e}")

    health = get_system_status(GetSystemStatusParams(user_id=config.user_id))
    out.info(f"Health check: {health[:100] if isinstance(health, str) else 'OK'}")

    out.panel(
        "Foresight is ready. Try:\n"
        "  foresight status          # Check health\n"
        "  foresight store 'hello'   # Store a memory\n"
        "  foresight list            # List memories\n"
        "  foresight tui             # Launch the TUI",
        title="Setup Complete",
    )


def _mask_db_url(url: str) -> str:
    """Mask credentials in a database URL for safe display (no secrets in CLI output)."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        netloc = f"***@{host}" if host else "***"
        return parsed._replace(netloc=netloc).geturl()
    except Exception:
        if "://" in url and "@" in url:
            scheme, _, rest = url.partition("://")
            _, _, tail = rest.partition("@")
            return f"{scheme}://***@{tail}"
        return "***"


@dataclass
class _DoctorReport:
    """Running tally of diagnostics results."""

    passed: int = 0
    failed: int = 0
    warnings: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        """Record one check and echo its result outside agent mode."""
        if ok:
            self.passed += 1
            if out.get_settings().mode != "agent":
                out.stdout(f"  ✓ {name}", style="green")
        else:
            self.failed += 1
            if out.get_settings().mode != "agent":
                out.stdout(f"  ✗ {name}: {detail}", style="red")


def _doctor_check_environment(report: _DoctorReport, config: Any) -> None:
    """Check the interpreter, config files, database URL, identities, and env overrides."""
    report.check("Python 3.11+", sys.version_info >= (3, 11), f"Found {sys.version}")
    report.check("Config dir exists", cfg.CONFIG_DIR.exists(), str(cfg.CONFIG_DIR))
    report.check("Config file exists", cfg.CONFIG_PATH.exists(), str(cfg.CONFIG_PATH))

    db_url = cfg.get_db_url()
    if db_url:
        report.check("Database URL configured", True, _mask_db_url(db_url))
    else:
        report.check("Database URL configured", False, "Set FORESIGHT_DB_URL env var")

    if config.user_id:
        report.check("User ID configured", True, config.user_id)
    if config.bank_id:
        report.check("Bank ID configured", True, config.bank_id)

    for env_var in ["FORESIGHT_DB_URL", "FORESIGHT_USER_ID", "FORESIGHT_BANK_ID"]:
        if os.environ.get(env_var):
            val = os.environ[env_var]
            if env_var == "FORESIGHT_DB_URL":
                val = _mask_db_url(val)
            report.warnings.append(f"{env_var}={val}")


def _doctor_check_database(report: _DoctorReport, resolved_uid: str | None) -> None:
    """Check that the memory store answers a status query."""
    try:
        health = get_system_status(GetSystemStatusParams(user_id=resolved_uid))
        if isinstance(health, str):
            health = json.loads(health) if health.startswith("{") else {"raw": health}
        if isinstance(health, dict):
            mem_count = health.get("memory_count", health.get("count", "?"))
            report.check("Database responsive", True, f"Memories: {mem_count}")
        else:
            report.check("Database responsive", True)
    except Exception as e:
        report.check("Database responsive", False, str(e))


def _doctor_check_llm(report: _DoctorReport) -> None:
    """Check the LLM provider configuration is internally consistent."""
    llm_provider = os.environ.get("FORESIGHT_LLM_PROVIDER", "")
    llm_api_key = os.environ.get("FORESIGHT_LLM_API_KEY", "")
    llm_model = os.environ.get("FORESIGHT_LLM_MODEL", "")
    supported_providers = {"openai", "anthropic", "gemini", "ollama", "vllm", "lmstudio", "none"}
    if llm_provider:
        if llm_provider.lower() not in supported_providers:
            report.check("LLM provider configured", False, f"unsupported provider: {llm_provider}")
        elif not llm_api_key and llm_provider.lower() not in {"ollama", "vllm", "lmstudio", "none"}:
            report.check("LLM provider configured", False, f"{llm_provider} set but FORESIGHT_LLM_API_KEY missing")
        else:
            report.check("LLM provider configured", True, f"{llm_provider}/{llm_model or 'default'}")
    elif llm_api_key or llm_model:
        report.check("LLM provider configured", False, "partial (some vars set, FORESIGHT_LLM_PROVIDER missing)")
    else:
        report.check("LLM provider configured", True, "not set (synthesis/reflection disabled)")


def _doctor_check_redis(report: _DoctorReport) -> None:
    """Check the optional Redis cache is reachable when configured."""
    redis_url = os.environ.get("FORESIGHT_REDIS_URL", "")
    if not redis_url:
        report.check("Redis cache", True, "not set (in-process cache fallback)")
        return
    try:
        _r = _redis.from_url(redis_url, socket_connect_timeout=0.5)
        _r.ping()
        report.check("Redis cache", True, "reachable")
    except Exception as exc:
        report.check("Redis cache", False, f"configured but unreachable: {exc!s}")


def _doctor_check_mcp_port(report: _DoctorReport) -> None:
    """Check whether the MCP HTTP server is listening."""
    mcp_port = int(os.environ.get("FORESIGHT_PORT", "8764"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(("127.0.0.1", mcp_port))
        report.check("MCP HTTP server", True, f"listening on :{mcp_port}")
    except (TimeoutError, ConnectionRefusedError, OSError):
        report.check("MCP HTTP server", False, f"not running on :{mcp_port} (start with: foresight-server)")
    finally:
        sock.close()


def _doctor_check_schema(report: _DoctorReport) -> None:
    """Check the applied schema migration version."""
    try:
        backend = get_backend()
        if backend is None:
            report.check("Schema version", True, "backend not initialized")
        elif hasattr(backend, "fetch"):
            row = backend.fetch("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1")
            if row:
                version = row[0][0] if isinstance(row[0], (list, tuple)) else row[0].get("version", "?")
                report.check("Schema version", True, f"v{version}")
            else:
                report.check("Schema version", True, "no migrations applied")
        else:
            report.check("Schema version", True, "backend supports migrations")
    except Exception as exc:
        report.check("Schema version", False, f"check failed: {exc!s}")


def _doctor_print_summary(report: _DoctorReport) -> None:
    """Print the pass/fail tally and any active env overrides."""
    if out.get_settings().mode != "agent":
        out.stderr("")
        overrides = f" ({len(report.warnings)} env overrides)" if report.warnings else ""
        if report.failed == 0:
            out.done(f"All {report.passed} checks passed{overrides}")
        else:
            out.warn(f"{report.passed} passed, {report.failed} failed{overrides}")

    if report.warnings and out.get_settings().mode != "agent":
        out.info("Active env overrides:")
        for w in report.warnings:
            out.info(f"  {w}")


def doctor(params: SystemDoctorParams) -> None:
    """Run comprehensive diagnostics."""
    _init_backend()
    resolved_uid = cfg.get_user_id(params.user_id)
    config = cfg.ensure_config()
    report = _DoctorReport()

    if out.get_settings().mode != "agent":
        out.stderr("Foresight Diagnostics", style="bold underline")
        out.stderr("")

    _doctor_check_environment(report, config)
    _doctor_check_database(report, resolved_uid)
    _doctor_check_llm(report)
    _doctor_check_redis(report)
    _doctor_check_mcp_port(report)
    _doctor_check_schema(report)
    _doctor_print_summary(report)

    if report.failed > 0:
        raise SystemExit(1)


def _stats_temporal() -> Any:
    """Fetch the month-long temporal trend, or None when the query fails."""
    try:
        return query_memories_temporal(QueryMemoriesTemporalParams(options=TemporalWindow(window="month", limit=100)))
    except Exception:
        return None


def _stats_health(resolved_uid: str | None) -> dict[str, Any]:
    """Fetch the system status payload as a dict."""
    health_raw = get_system_status(GetSystemStatusParams(user_id=resolved_uid))
    if isinstance(health_raw, str):
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(health_raw)
            if isinstance(parsed, dict):
                return parsed
        return {}
    if isinstance(health_raw, dict):
        return health_raw
    return {}


def _stats_telemetry() -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch the telemetry summary and cache stats, degrading to empty maps."""
    try:
        return get_telemetry_store().get_summary(), get_context_cache().get_stats()
    except Exception:
        return {}, {}


def _maintenance_stats_row(maint: dict[str, Any]) -> list[str]:
    """Format the single maintenance-stats table row."""
    return [
        str(maint.get("total_events", 0)),
        str(maint.get("duplicates_auto_consolidated", 0)),
        str(maint.get("duplicates_flagged_review", 0)),
        str(maint.get("contradictions_flagged_review", 0)),
        str(maint.get("stale_archived", 0)),
        str(maint.get("insights_generated", 0)),
    ]


def _emit_stats_json(
    health: dict[str, Any],
    temporal: Any,
    telemetry: dict[str, Any],
    cache_stats: dict[str, Any],
) -> None:
    """Print the stats payload as JSON for agent mode."""
    maint = health.get("maintenance_stats")
    lct = health.get("last_capture_time")
    budget = health.get("payload_budget")

    stats_data: dict[str, Any] = {
        "memory_count": health.get("memory_count", health.get("count", 0)),
        "crisis_signals": health.get("crisis_signals", 0),
        "by_scope": health.get("by_scope", {}),
        "temporal": temporal if isinstance(temporal, dict) else None,
        "telemetry": telemetry,
        "cache": cache_stats,
    }
    if isinstance(maint, dict):
        stats_data["maintenance_stats"] = maint
    if lct:
        stats_data["last_capture_time"] = str(lct)[:19]
    if isinstance(budget, dict):
        stats_data["payload_budget"] = budget
    out.print_json(stats_data)


def _emit_stats_human(
    health: dict[str, Any],
    temporal: Any,
    telemetry: dict[str, Any],
    cache_stats: dict[str, Any],
) -> None:
    """Print the human-readable statistics report."""
    mem_count = health.get("memory_count", health.get("count", 0))
    crisis = health.get("crisis_signals", 0)
    lct = health.get("last_capture_time")
    maint = health.get("maintenance_stats")

    if telemetry:
        out.panel(
            f"[bold cyan]Lifetime Turns Augmented:[/] {telemetry.get('total_turns_augmented', 0):,}\n"
            f"[bold green]Prompt Tokens Saved:[/]     {telemetry.get('total_tokens_saved', 0):,} tokens "
            f"({telemetry.get('token_reduction_pct', 88.5):.1f}% reduction)\n"
            f"[bold yellow]Est. API Cost Saved:[/]     ${telemetry.get('total_cost_saved_usd', 0.0):.2f}\n"
            f"[bold magenta]In-Memory Cache Hit Rate:[/] {cache_stats.get('hit_rate_pct', 0.0):.1f}% "
            f"({cache_stats.get('total_hits', 0)} hits / {cache_stats.get('total_lookups', 0)} lookups)",
            title="⚡ Foresight Lifetime Telemetry & Economics",
        )

    out.stderr(f"Memory Count: {mem_count}", style="bold")
    out.stderr(f"Crisis Signals: {crisis}")

    if lct:
        out.stderr(f"Last Capture: {str(lct)[:19]}")

    by_scope = health.get("by_scope", {})
    if isinstance(by_scope, dict) and by_scope:
        rows = [[k, str(v)] for k, v in by_scope.items()]
        out.print_table(["Scope", "Count"], rows, title="By Scope")

    if isinstance(maint, dict):
        out.print_table(
            ["Total", "Consolidated", "Flagged", "Contradictions", "Archived", "Insights"],
            [_maintenance_stats_row(maint)],
            title="Maintenance Stats",
        )

    if temporal:
        out.result_block(temporal if isinstance(temporal, dict) else {"raw": str(temporal)}, title="Temporal Trends")

    if mem_count == 0:
        out.info("No memories yet. Store one with: foresight store 'your first memory'")


def stats(params: SystemStatsParams) -> None:
    """Show memory statistics and trends."""
    _init_backend()
    resolved_uid = cfg.get_user_id(params.user_id)
    temporal = _stats_temporal()
    health = _stats_health(resolved_uid)
    telemetry, cache_stats = _stats_telemetry()

    if out.get_settings().mode == "agent":
        _emit_stats_json(health, temporal, telemetry, cache_stats)
        return

    _emit_stats_human(health, temporal, telemetry, cache_stats)


def temporal(params: SystemTemporalParams) -> None:
    """Query memories by time window, trend, or point in time."""
    _init_backend()

    try:
        if params.as_of is not None:
            _parse_as_of(params.as_of)
        options = TemporalWindow(
            window=params.window,
            trend=params.trend,
            category=params.category,
            limit=params.limit,
            as_of=params.as_of,
        )
    except (ValueError, TypeError) as exc:
        out.error(f"Invalid temporal query: {exc}")
        raise SystemExit(2) from exc

    result = query_memories_temporal(
        QueryMemoriesTemporalParams(options=options, user_id=cfg.get_user_id(params.user_id))
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
        return

    try:
        memories = json.loads(result)
    except json.JSONDecodeError:
        out.result_block({"raw": result}, title="Temporal Query")
        return

    if not memories:
        out.info("No memories matched.")
        return

    rows = [
        [
            str(m.get("memory_id", "?"))[:12],
            str(m.get("content", ""))[:60],
            str(m.get("category") or "-"),
            str(m.get("importance", "?")),
            str(m.get("strength_trend") or "-"),
            str(m.get("created_at", "?"))[:19],
        ]
        for m in memories
    ]
    title = f"Memories as of {params.as_of}" if params.as_of else f"Memories ({params.window})"
    out.print_table(["Memory", "Content", "Category", "Importance", "Trend", "Created"], rows, title=title)


def config(params: SystemConfigParams) -> None:
    """View or modify Foresight configuration."""
    cfg.ensure_config()

    if params.reset:
        c = cfg.CliConfig()
        c.save()
        out.done("Config reset to defaults")
        return

    c = cfg.CliConfig.load()

    if params.key and params.value:
        if params.key == "db_url":
            out.warn(
                "db_url is read-only in config; set the FORESIGHT_DB_URL "
                "environment variable to configure the database."
            )
            return
        setattr(c, params.key, params.value)
        c.save()
        out.done(f"Set {params.key} = {params.value}")
        return

    if params.key:
        val = getattr(c, params.key, None)
        if val is not None:
            out.data(params.key, val)
        else:
            out.error(f"Unknown config key: {params.key}")
        return

    pairs = [
        ("db_url", _mask_db_url(c.db_url)),
        ("user_id", c.user_id),
        ("bank_id", c.bank_id),
        ("theme", c.theme),
        ("timeout", str(c.timeout)),
    ]
    out.kv_table(pairs, title="Foresight Configuration")
    out.info(f"\nConfig file: {cfg.CONFIG_PATH}")


def history(params: SystemHistoryParams) -> None:
    """View recent memory decay and reinforcement events."""
    _init_backend()
    resolved_uid = cfg.get_user_id(params.user_id)

    try:
        raw = get_decay_events(user_id=resolved_uid, limit=params.limit)
        parsed: dict = json.loads(raw) if isinstance(raw, str) else raw
        events_list: list = parsed.get("events", []) if parsed.get("ok") else []
    except (json.JSONDecodeError, TypeError, OSError) as e:
        out.error(f"Failed to retrieve history: {e}")
        raise SystemExit(1) from e

    if out.get_settings().mode == "agent":
        out.print_json({"events": events_list})
        return

    if events_list:
        rows = []
        for ev in events_list:
            eid = ev.get("memory_id", "?")[:12]
            etype = ev.get("event_type", ev.get("type", "?"))
            strength = ev.get("old_strength", ev.get("strength", "?"))
            ts = ev.get("timestamp", ev.get("created_at", "?"))
            rows.append([eid, etype, str(strength), str(ts)[:19]])
        out.print_table(
            ["Memory", "Event", "Strength", "Timestamp"],
            rows,
            title=f"Recent Events (last {params.limit})",
        )
    else:
        out.info("No events found.")


def maintenance(params: SystemMaintenanceParams) -> None:
    """Run memory maintenance (consolidation, contradiction detection, stale archival, reprocess)."""
    _init_backend()

    modes_list = (
        ["consolidate", "contradict", "archive_stale", "synthesize"]
        if params.mode == "all"
        else [m.strip() for m in params.mode.split(",")]
    )
    result = run_maintenance(
        options=MaintenanceAction(
            modes=modes_list,
            batch_size=params.batch_size,
            max_runtime_seconds=params.max_runtime,
            reprocess_window_start=params.reprocess_window_start,
            reprocess_window_end=params.reprocess_window_end,
        ),
        user_id=params.user_id,
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Maintenance Results")


def reprocess(params: SystemReprocessParams) -> None:
    """Rebuild derived memories from the append-only raw-events layer.

    Idempotent by construction: replaying a window re-runs classification and
    extraction, and dedupe keeps duplicates from being created. Requires
    FORESIGHT_RAW_RETENTION — the flag is read per call, so the environment of
    this process decides whether there is anything to replay.
    """
    _init_backend()

    if not raw_retention_enabled():
        out.warn("FORESIGHT_RAW_RETENTION is off — the raw layer was never written, so this is a no-op.")

    result = reprocess_raw_events(
        window_start=params.window_start,
        window_end=params.window_end,
        user_id=cfg.get_user_id(params.user_id),
    )
    if out.get_settings().mode in ("agent", "json"):
        out.print_json(result)
    else:
        out.result_block(result, title="Raw-Event Reprocess")


def tenant(params: SystemTenantParams) -> None:
    """Switch current tenant context."""
    _init_backend()

    result = switch_tenant(tenant_id=params.tenant_id)
    out.done(f"Switched tenant: {result}")


def _user_id_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _init_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", "-f", action="store_true", help="Overwrite existing config")
    parser.add_argument("--user-id", "-u", default=None, help="Default user ID")
    parser.add_argument("--bank-id", "-b", default=None, help="Default bank ID")


def _temporal_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--window", "-w", default="week", help="Time window: today, week, month, year")
    parser.add_argument(
        "--trend",
        "-t",
        default=None,
        help="Trend filter: stable, strengthening, weakening, stale",
    )
    parser.add_argument("--category", "-c", default=None, help="Category filter")
    parser.add_argument("--limit", "-l", type=int, default=50, help="Max results")
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 instant. Reconstructs what memory was true at that point in time "
        "(bitemporal); overrides --window and --trend.",
    )
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _config_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "key",
        nargs="?",
        default=None,
        help="Config key to view/set (e.g. user_id, db_url, bank_id)",
    )
    parser.add_argument("value", nargs="?", default=None, help="Value to set (omit to view current)")
    parser.add_argument("--reset", action="store_true", help="Reset config to defaults")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _history_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", "-l", type=int, default=10, help="Number of events")
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _maintenance_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode",
        "-m",
        default="all",
        help="Comma-separated modes (consolidate,contradict,archive_stale,synthesize,reprocess) or 'all'",
    )
    parser.add_argument("--batch-size", type=int, default=200, help="Max memories to process per mode")
    parser.add_argument("--max-runtime", type=float, default=300, help="Wall-clock budget in seconds")
    parser.add_argument(
        "--reprocess-start",
        dest="reprocess_window_start",
        default=None,
        help="ISO-8601 start of reprocess window",
    )
    parser.add_argument(
        "--reprocess-end",
        dest="reprocess_window_end",
        default=None,
        help="ISO-8601 end of reprocess window",
    )
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _reprocess_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--start",
        dest="window_start",
        default=None,
        help="ISO-8601 start of the reprocess window (inclusive)",
    )
    parser.add_argument(
        "--end",
        dest="window_end",
        default=None,
        help="ISO-8601 end of the reprocess window (inclusive)",
    )
    parser.add_argument("--user-id", "-u", default=None, help="User ID override")


def _tenant_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("tenant_id", help="Tenant ID to switch to")


COMMANDS = {
    "status": CommandSpec(status, SystemStatusParams, _user_id_parser),
    "init": CommandSpec(init, SystemInitParams, _init_parser),
    "doctor": CommandSpec(doctor, SystemDoctorParams, _user_id_parser),
    "stats": CommandSpec(stats, SystemStatsParams, _user_id_parser),
    "temporal": CommandSpec(temporal, SystemTemporalParams, _temporal_parser),
    "config": CommandSpec(config, SystemConfigParams, _config_parser),
    "history": CommandSpec(history, SystemHistoryParams, _history_parser),
    "maintenance": CommandSpec(maintenance, SystemMaintenanceParams, _maintenance_parser),
    "reprocess": CommandSpec(reprocess, SystemReprocessParams, _reprocess_parser),
    "tenant": CommandSpec(tenant, SystemTenantParams, _tenant_parser),
}
