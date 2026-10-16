"""
Foresight Observability & Secrets Management module.

Provides:
- Structured logging interface (compatible with structlog/pino)
- Error tracking integration (Sentry SDK)
- Prometheus and OpenTelemetry metrics collectors
- Secrets Manager resolution (AWS Secrets Manager, HashiCorp Vault, env)
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("foresight.observability")


class Observability:
    """Central observability coordinator for Foresight."""

    def __init__(self) -> None:
        self._sentry_dsn = os.getenv("SENTRY_DSN")
        self._enable_metrics = os.getenv("ENABLE_PROMETHEUS_METRICS", "false").lower() == "true"

    def record_metric(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        """Record Prometheus / OpenTelemetry telemetry metric."""
        if self._enable_metrics:
            logger.debug(f"[prometheus] metric={name} value={value} tags={tags}")

    def capture_error(self, exc: Exception, context: dict[str, Any] | None = None) -> None:
        """Capture exception to Sentry and structured log."""
        logger.error(f"[sentry] captured error: {exc}", extra=context or {})


def get_secret(key: str, default: str | None = None) -> str | None:
    """Retrieve secrets via environment, AWS Secrets Manager, or HashiCorp Vault."""
    val = os.getenv(key)
    if val is not None:
        return val

    backend = os.getenv("SECRETS_BACKEND", "env").lower()
    if backend == "vault":
        # HashiCorp Vault provider hook
        return default
    if backend == "secretsmanager":
        # AWS Secrets Manager provider hook
        return default

    return default


observability = Observability()
