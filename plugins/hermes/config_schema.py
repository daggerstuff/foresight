"""Declarative config schema for the Foresight memory provider plugin.

Used by Hermes Agent's Web UI and Desktop Capabilities panel:
``GET/PUT /api/memory/providers/foresight/config``.
"""

from __future__ import annotations

from typing import Any

try:
    from plugins.memory.config_schema import (
        KIND_BOOL,
        KIND_NUMBER,
        KIND_SECRET,
        KIND_TEXT,
        ProviderConfigSchema,
        ProviderField,
    )
except ImportError:
    KIND_BOOL, KIND_NUMBER, KIND_SECRET, KIND_TEXT = ("bool", "number", "secret", "text")

    class ProviderConfigSchema:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class ProviderField:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)


CONFIG_SCHEMA = ProviderConfigSchema(
    name="foresight",
    label="Foresight Persistent Memory",
    docs_url="https://github.com/pixelated/foresight",
    fields=(
        ProviderField(
            key="url",
            label="Server URL",
            kind=KIND_TEXT,
            default="http://127.0.0.1:8764",
            placeholder="https://foresight.pixelated.love",
            description="Base URL of the Foresight REST server (local service or shared fleet endpoint)",
            env_key="FORESIGHT_URL",
            inline=True,
        ),
        ProviderField(
            key="api_key",
            label="API Key",
            kind=KIND_SECRET,
            description="Bearer API key for the Foresight server (required on remote fleet endpoints)",
            env_key="FORESIGHT_API_KEY",
            inline=True,
        ),
        ProviderField(
            key="user_id",
            label="Identity Lane (X-User-Id)",
            kind=KIND_TEXT,
            default="",
            placeholder="chad",
            description="Tenant/identity lane for scoping memories (defaults to current user profile)",
            env_key="FORESIGHT_USER_ID",
            inline=True,
        ),
        ProviderField(
            key="max_memories",
            label="Recall Limit",
            kind=KIND_NUMBER,
            default="5",
            description="Max number of relevant memories surfaced per turn",
            inline=False,
        ),
        ProviderField(
            key="auto_distill",
            label="Continuous Distillation",
            kind=KIND_BOOL,
            default="true",
            description="Automatically distill turns and session transcripts into long-term memory",
            inline=False,
        ),
    ),
)
