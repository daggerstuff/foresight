"""Declarative retention policy and reversible memory expiry.

Memories carry a reversible expiry stamp (``expired_at`` / ``expired_by``):
expired rows are excluded from default retrieval but recallable via
``include_expired``, and restoring clears the stamp. ``purge_expired_after``
is the *only* destructive path and defaults to ``"never"``.

Policy shape (per ``tenant_id`` + ``user_id``, stored as JSON in
``memory_policies``)::

    {
      "retention": {"<category>": "<duration>"},
      "rules": [
        {"name": "...", "match": {"category": "...", "tags": ["..."],
                                  "source_tool": "...", "confidence_below": 0.3},
         "expire_after": "<duration>"}
      ],
      "purge_expired_after": "<duration>"
    }

Semantics (docs.memanto.ai/reference/memory-lifecycle.md):

- Rules are evaluated in order; the **first match wins**. A rule whose
  ``expire_after`` is ``"never"`` pins the memory (no expiry, and the
  category-level ``retention`` fallback is *not* consulted for it).
- When no rule matches, ``retention[category]`` applies if present.
- ``expire_after`` durations: ``30m``, ``12h``, ``7d``, ``2w``, ``3mo``
  (90 days), ``1y``, ``never``.
- Age is measured from ``updated_at`` (falling back to ``created_at``), so
  edits reset the clock.
- ``expired_by`` records the cause: ``policy:<rule name>`` for rule matches,
  ``retention:<category>`` for category retention, ``manual`` /
  ``conflict-resolution`` for explicit calls.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger("foresight.retention_policy")

# Duration vocabulary shared by expire_after and purge_expired_after.
# "never" maps to None (pin / no purge). 3mo is defined as 90 days.
DURATION_SECONDS: dict[str, float | None] = {
    "30m": 30 * 60,
    "12h": 12 * 3600,
    "7d": 7 * 86400,
    "2w": 14 * 86400,
    "3mo": 90 * 86400,
    "1y": 365 * 86400,
    "never": None,
}

VALID_DURATIONS = tuple(DURATION_SECONDS)

# Conservative = almost nothing expires; balanced = short-lived operational
# categories roll off; aggressive = aggressive roll-off plus a purge window.
POLICY_PRESETS: dict[str, dict[str, Any]] = {
    "conservative": {
        "retention": {},
        "rules": [
            {
                "name": "very-low-confidence",
                "match": {"confidence_below": 0.2},
                "expire_after": "3mo",
            }
        ],
        "purge_expired_after": "never",
    },
    "balanced": {
        "retention": {"pending": "3mo", "episodic": "3mo"},
        "rules": [
            {
                "name": "low-confidence",
                "match": {"confidence_below": 0.3},
                "expire_after": "1y",
            }
        ],
        "purge_expired_after": "never",
    },
    "aggressive": {
        "retention": {"pending": "7d", "episodic": "3mo", "curation": "3mo"},
        "rules": [
            {
                "name": "low-confidence",
                "match": {"confidence_below": 0.5},
                "expire_after": "3mo",
            },
            {
                "name": "session-transient",
                "match": {"category": "session"},
                "expire_after": "7d",
            },
        ],
        "purge_expired_after": "3mo",
    },
}


class PolicyError(ValueError):
    """Raised when a policy document is structurally invalid."""


@dataclass(frozen=True)
class ExpiryRule:
    """One ordered expiry rule."""

    name: str
    expire_after: str
    category: str | None = None
    tags: tuple[str, ...] = ()
    source_tool: str | None = None
    confidence_below: float | None = None


@dataclass(frozen=True)
class RetentionConfig:
    """Parsed retention policy for one (tenant, user)."""

    retention: dict[str, str] = field(default_factory=dict)
    rules: tuple[ExpiryRule, ...] = ()
    purge_expired_after: str = "never"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetentionConfig:
        """Parse + validate a policy document; raises :class:`PolicyError`."""
        if not isinstance(data, dict):
            raise PolicyError("policy must be a JSON object")
        unknown_top_keys = set(data) - {"retention", "rules", "purge_expired_after"}
        if unknown_top_keys:
            raise PolicyError(f"policy has unknown keys: {sorted(unknown_top_keys, key=repr)}")
        # Only a missing/None section is an empty section; a supplied falsy
        # value (``""``, ``[]``, ``0``) is malformed and must not silently
        # disable enforcement.
        retention = data.get("retention")
        if retention is None:
            retention = {}
        if not isinstance(retention, dict):
            raise PolicyError("retention must be an object mapping category -> duration")
        for cat, dur in retention.items():
            _parse_duration(dur, f"retention[{cat!r}]")

        rules: list[ExpiryRule] = []
        raw_rules = data.get("rules")
        if raw_rules is None:
            raw_rules = []
        if not isinstance(raw_rules, list):
            raise PolicyError("rules must be a list")
        for i, raw in enumerate(raw_rules):
            rules.append(_parse_rule(raw, i))

        purge = data.get("purge_expired_after", "never")
        _parse_duration(purge, "purge_expired_after")
        return cls(
            retention={str(k): str(v) for k, v in retention.items()},
            rules=tuple(rules),
            purge_expired_after=str(purge),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "retention": dict(self.retention),
            "rules": [
                {
                    "name": r.name,
                    "expire_after": r.expire_after,
                    **({"match": _rule_match_dict(r)} if _rule_match_dict(r) else {}),
                }
                for r in self.rules
            ],
            "purge_expired_after": self.purge_expired_after,
        }


def _rule_match_dict(rule: ExpiryRule) -> dict[str, Any]:
    match: dict[str, Any] = {}
    if rule.category is not None:
        match["category"] = rule.category
    if rule.tags:
        match["tags"] = list(rule.tags)
    if rule.source_tool is not None:
        match["source_tool"] = rule.source_tool
    if rule.confidence_below is not None:
        match["confidence_below"] = rule.confidence_below
    return match


def _parse_duration(value: Any, where: str) -> float | None:
    """Validate a duration token; returns seconds or None for 'never'."""
    if not isinstance(value, str) or value not in DURATION_SECONDS:
        raise PolicyError(f"{where}: invalid duration {value!r} (expected one of {', '.join(VALID_DURATIONS)})")
    return DURATION_SECONDS[value]


def _parse_rule(raw: Any, index: int) -> ExpiryRule:
    if not isinstance(raw, dict):
        raise PolicyError(f"rules[{index}] must be an object")
    unknown_rule_keys = set(raw) - {"name", "expire_after", "match"}
    if unknown_rule_keys:
        # A misspelled ``match`` would otherwise leave every criterion unset.
        raise PolicyError(f"rules[{index}] has unknown keys: {sorted(unknown_rule_keys, key=repr)}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PolicyError(f"rules[{index}].name is required")
    expire_after = raw.get("expire_after")
    _parse_duration(expire_after, f"rules[{index}].expire_after")
    match = raw.get("match")
    if match is None:
        match = {}
    if not isinstance(match, dict):
        raise PolicyError(f"rules[{index}].match must be an object")
    unknown = set(match) - {"category", "tags", "source_tool", "confidence_below"}
    if unknown:
        # A typo would otherwise leave every criterion unset and match all rows.
        raise PolicyError(f"rules[{index}].match has unknown keys: {sorted(unknown, key=repr)}")
    for key in ("category", "source_tool"):
        if match.get(key) is not None and not isinstance(match[key], str):
            raise PolicyError(f"rules[{index}].match.{key} must be a string")
    tags = match.get("tags")
    if tags is None:
        tags = []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise PolicyError(f"rules[{index}].match.tags must be a list of strings")
    confidence = match.get("confidence_below")
    if confidence is not None and not isinstance(confidence, (int, float)):
        raise PolicyError(f"rules[{index}].match.confidence_below must be a number")
    return ExpiryRule(
        name=name.strip(),
        expire_after=str(expire_after),
        category=match.get("category"),
        tags=tuple(tags),
        source_tool=match.get("source_tool"),
        confidence_below=float(confidence) if confidence is not None else None,
    )


def preset_policy(name: str) -> dict[str, Any]:
    """Return a copy of a named preset (conservative/balanced/aggressive)."""
    if name not in POLICY_PRESETS:
        raise PolicyError(f"unknown preset {name!r}; expected one of {', '.join(sorted(POLICY_PRESETS))}")
    return json.loads(json.dumps(POLICY_PRESETS[name]))


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _memory_age_seconds(row: dict[str, Any], now: datetime) -> float | None:
    """Age in seconds from updated_at (falling back to created_at)."""
    base = _parse_ts(row.get("updated_at")) or _parse_ts(row.get("created_at"))
    if base is None:
        return None
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    return (now - base).total_seconds()


def _confidence(row: dict[str, Any]) -> float:
    """Effective confidence: decayed strength when present, else importance."""
    strength = row.get("current_strength")
    if strength is not None:
        return float(strength)
    return float(row.get("importance") or 0.0)


def _row_tags(row: dict[str, Any]) -> set[str]:
    raw = row.get("tags") or "[]"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return set()
    return {str(t) for t in raw} if isinstance(raw, list) else set()


def _rule_matches(rule: ExpiryRule, row: dict[str, Any]) -> bool:
    if rule.category is not None and row.get("category") != rule.category:
        return False
    if rule.tags and not (_row_tags(row) & set(rule.tags)):
        return False
    if rule.source_tool is not None and row.get("source_tool") != rule.source_tool:
        return False
    if rule.confidence_below is not None:
        return _confidence(row) < rule.confidence_below
    return True


def _aged_out(row: dict[str, Any], duration: str, now: datetime) -> bool:
    """True when the row is older than ``duration`` (None/'never' never ages out)."""
    seconds = DURATION_SECONDS[duration]
    if seconds is None:
        return False
    age = _memory_age_seconds(row, now)
    return age is not None and age > seconds


def policy_decision(config: RetentionConfig, row: dict[str, Any], now: datetime | None = None) -> str | None:
    """Return the ``expired_by`` reason when the row should be expired, else None.

    First matching rule wins; ``expire_after: "never"`` pins the row. With no
    rule match, the category-level ``retention`` duration applies. Expiry is
    time-based: the row must be older than the duration (from ``updated_at``,
    falling back to ``created_at``) to be stamped.
    """
    now = now or datetime.now(UTC)
    for rule in config.rules:
        if not _rule_matches(rule, row):
            continue
        if rule.expire_after == "never":
            return None  # pinned by rule
        # Matched but maybe not old enough — retention fallback is skipped.
        return f"policy:{rule.name}" if _aged_out(row, rule.expire_after, now) else None
    category = row.get("category")
    duration = config.retention.get(category) if category else None
    if duration is None or duration == "never":
        return None
    return f"retention:{category}" if _aged_out(row, duration, now) else None


def policy_governs(config: RetentionConfig, row: dict[str, Any]) -> bool:
    """True when a stored policy decides this row's lifecycle.

    A matching rule (including ``expire_after: "never"`` pins) or a
    category-level ``retention`` entry means the row is not the legacy TTL
    sweeps' business — the policy pass stamps it, pins it, or purges it.
    """
    for rule in config.rules:
        if _rule_matches(rule, row):
            return True
    category = row.get("category")
    return bool(category and config.retention.get(category))


def is_policy_expired(row: dict[str, Any]) -> bool:
    """True when the row already carries an expiry stamp."""
    return bool(row.get("expired_at"))


# ---------------------------------------------------------------------------
# Storage (memory_policies table) — connection-agnostic, best-effort
# ---------------------------------------------------------------------------


def load_policy_status(
    conn: Any,
    tenant_id: str,
    user_id: str,
) -> tuple[RetentionConfig | None, str | None]:
    """Load a stored policy and report why it is missing.

    Returns ``(config, None)`` when valid, ``(None, "missing")`` when no row
    exists, and ``(None, "invalid: ...")`` when the row cannot be parsed, so
    operators can tell a typo from an empty configuration.
    """
    try:
        row = conn.execute(
            "SELECT policy_json FROM memory_policies WHERE tenant_id = ? AND user_id = ?",
            (tenant_id, user_id),
        ).fetchone()
    except Exception:
        return None, "missing"
    if not row:
        return None, "missing"
    raw = row["policy_json"] if isinstance(row, dict) else row[0]
    try:
        return RetentionConfig.from_dict(json.loads(raw)), None
    except (PolicyError, TypeError, ValueError) as exc:
        logger.warning("invalid stored retention policy for %s/%s", tenant_id, user_id)
        return None, f"invalid: {exc}"


def load_policy(conn: Any, tenant_id: str, user_id: str) -> RetentionConfig | None:
    """Load the stored policy for (tenant, user); None when unset/invalid."""
    config, _error = load_policy_status(conn, tenant_id, user_id)
    return config


def save_policy(
    conn: Any,
    tenant_id: str,
    user_id: str,
    policy: dict[str, Any] | RetentionConfig,
    preset: str | None = None,
) -> RetentionConfig:
    """Validate and upsert a policy for (tenant, user)."""
    config = policy if isinstance(policy, RetentionConfig) else RetentionConfig.from_dict(policy)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO memory_policies (tenant_id, user_id, preset, policy_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (tenant_id, user_id) DO UPDATE SET "
        "preset = excluded.preset, policy_json = excluded.policy_json, updated_at = excluded.updated_at",
        (tenant_id, user_id, preset, json.dumps(config.to_dict()), now),
    )
    conn.commit()
    return config


def list_policies(
    conn: Any,
    tenant_id: str,
    *,
    strict: bool = False,
) -> list[tuple[str, RetentionConfig]]:
    """Return every (user_id, policy) stored for a tenant.

    ``strict`` re-raises a read failure instead of returning ``[]``: a caller
    that uses the result to decide what must not be deleted cannot treat an
    unreadable table as "no policies".
    """
    try:
        rows = conn.execute(
            "SELECT user_id, policy_json FROM memory_policies WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchall()
    except Exception:
        if strict:
            raise
        return []
    out: list[tuple[str, RetentionConfig]] = []
    for row in rows:
        uid = row["user_id"] if isinstance(row, dict) else row[0]
        raw = row["policy_json"] if isinstance(row, dict) else row[1]
        try:
            out.append((uid, RetentionConfig.from_dict(json.loads(raw))))
        except (PolicyError, TypeError, ValueError):
            logger.warning("skipping invalid stored policy for user %s", uid)
    return out


# ---------------------------------------------------------------------------
# Expire / restore primitives (shared by the MCP handlers and tests)
# ---------------------------------------------------------------------------


def expire_memory(
    conn: Any,
    memory_id: str,
    user_id: str,
    tenant_id: str,
    expired_by: str = "manual",
) -> bool:
    """Stamp a memory expired. Returns False when missing or already expired."""
    ts = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "UPDATE memories SET expired_at = ?, expired_by = ?, updated_at = ? "
        "WHERE id = ? AND user_id = ? AND tenant_id = ? AND expired_at IS NULL",
        (ts, expired_by, ts, memory_id, user_id, tenant_id),
    )
    return (cur.rowcount or 0) > 0


def restore_memory(conn: Any, memory_id: str, user_id: str, tenant_id: str) -> bool:
    """Clear the expiry stamp. Returns False when missing or not expired."""
    ts = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "UPDATE memories SET expired_at = NULL, expired_by = NULL, updated_at = ? "
        "WHERE id = ? AND user_id = ? AND tenant_id = ? AND expired_at IS NOT NULL",
        (ts, memory_id, user_id, tenant_id),
    )
    return (cur.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# Policy application (called by memory_gc)
# ---------------------------------------------------------------------------


def evaluate_expiry_policies(
    conn: Any,
    tenant_id: str,
    now: datetime | None = None,
) -> tuple[dict[str, int], set[str]]:
    """Apply stored policies once and report which rows they govern.

    Returns ``(stats, governed_ids)`` where stats is ``{"expired": n,
    "purged": n}``. The governed set covers rules and category retention
    (pins included) so the legacy TTL sweeps can skip those rows without a
    second scan. An unreadable ``memory_policies`` table raises (under strict=True)
    so memory_gc halts rather than deleting pinned rows under assumed missing policies;
    missing expiry columns degrade to a no-op.
    """
    now = now or datetime.now(UTC)
    stats = {"expired": 0, "purged": 0}
    governed: set[str] = set()
    for uid, config in list_policies(conn, tenant_id, strict=True):
        try:
            # Sensitive rows are never auto-archived (see the is_sensitive
            # contract in schema_ddl): policy expiry and its purge window
            # leave them untouched.
            rows = conn.execute(
                "SELECT id, category, tags, source_tool, importance, current_strength, "
                "updated_at, created_at FROM memories "
                "WHERE tenant_id = ? AND user_id = ? AND is_ghost = 0 AND expired_at IS NULL "
                "AND COALESCE(is_sensitive, 0) = 0",
                (tenant_id, uid),
            ).fetchall()
        except Exception:
            # Schema without expiry columns — nothing to do.
            return stats, governed
        for row in rows:
            r = dict(row)
            if policy_governs(config, r):
                governed.add(str(r["id"]))
            reason = policy_decision(config, r, now)
            if reason:
                conn.execute(
                    "UPDATE memories SET expired_at = ?, expired_by = ? WHERE id = ?",
                    (now.isoformat(), reason, r["id"]),
                )
                stats["expired"] += 1
        purge_seconds = DURATION_SECONDS[config.purge_expired_after]
        if purge_seconds is not None:
            cutoff = (now - timedelta(seconds=purge_seconds)).isoformat()
            cur = conn.execute(
                "DELETE FROM memories WHERE tenant_id = ? AND user_id = ? "
                "AND expired_at IS NOT NULL AND expired_at < ? "
                "AND COALESCE(is_sensitive, 0) = 0",
                (tenant_id, uid, cutoff),
            )
            stats["purged"] += cur.rowcount or 0
    return stats, governed


def apply_expiry_policies(conn: Any, tenant_id: str, now: datetime | None = None) -> dict[str, int]:
    """Expire policy-matched memories and purge per ``purge_expired_after``."""
    stats, _governed = evaluate_expiry_policies(conn, tenant_id, now)
    return stats
