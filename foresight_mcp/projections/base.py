"""
Base projection class for audit trail reports
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class BaseProjection(ABC):
    """
    Abstract base class for all projections.

    Projections are materialized views built from event data.
    Each projection serves a specific compliance use case.
    """

    name: str
    description: str

    @abstractmethod
    def build(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build projection from events."""
        pass

    @abstractmethod
    def to_csv(self, data: list[dict[str, Any]]) -> str:
        """Convert projection data to CSV."""
        pass

    def to_json(self, data: list[dict[str, Any]], indent: int = 2) -> str:
        """Convert projection data to JSON."""
        return json.dumps(data, indent=indent)

    def filter_by_date(
        self,
        data: list[dict[str, Any]],
        start: datetime | None = None,
        end: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Filter data by date range."""
        if not start and not end:
            return data

        result = []
        for item in data:
            ts = item.get("timestamp")
            if ts:
                item_date = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if start and item_date < start:
                    continue
                if end and item_date > end:
                    continue
            result.append(item)

        return result

    def filter_by_user(
        self,
        data: list[dict[str, Any]],
        user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Filter data by user ID."""
        if not user_id:
            return data

        return [item for item in data if item.get("user_id") == user_id]
