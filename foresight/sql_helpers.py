"""
SQL Helpers - Safe SQL generation utilities.

These helpers validate identifiers and generate safe SQL with parameterized queries.
"""

import re

# Whitelist of valid relationship types - from graph_store.py schema
VALID_RELATIONSHIP_TYPES = frozenset(
    {"mentions", "located_at", "experienced", "caused", "relates_to", "contradicts", "supports", "part_of", "created"}
)

# Whitelist of valid entity types - from graph_store.py schema
VALID_ENTITY_TYPES = frozenset({"person", "place", "concept", "event", "emotion", "object"})

# Whitelist of valid table names for internal use
VALID_TABLE_NAMES = frozenset(
    {
        "memory_entities",
        "entity_relationships",
        "memory_entity_links",
        "memories",
        "tenants",
        "decay_config",
        "memory_versions",
        "schema_migrations",
    }
)


_PLAIN_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def quote_identifier(name: str) -> str:
    """Double-quote *name* after proving it is a plain identifier.

    Used where the identifier set is discovered at runtime (``information_schema``)
    and therefore cannot be a frozen allowlist: anything that is not
    ``[A-Za-z_][A-Za-z0-9_]*`` is rejected instead of quoted.

    Raises:
        ValueError: If name is not a plain SQL identifier.
    """
    if not isinstance(name, str) or not _PLAIN_IDENTIFIER.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f'"{name}"'


def placeholders(count: int) -> str:
    """Return *count* comma-separated positional placeholders."""
    if count < 1:
        raise ValueError(f"placeholder count must be positive, got {count}")
    return ", ".join("?" for _ in range(count))


def join_sql_parts(*parts: str) -> str:
    """Concatenate SQL statement fragments into one string.

    This is a plain ``"".join(parts)`` and nothing more. It does not validate,
    quote, escape or bind anything, it does not check that a fragment came from
    :func:`placeholders`, :func:`build_in_clause`, :func:`build_type_filter`,
    :func:`validate_identifier` or :func:`quote_identifier`, and it makes no
    guarantee about the result. A caller that passes a value which has not
    already been proven safe produces an injectable statement.

    The SQL-injection rule in ``semgrep/sql-injection.yaml`` does not list this
    function as a sanitizer for the same reason: it is a pass-through, so taint
    flows through it into the statement text.

    Args:
        *parts: Statement fragments, concatenated verbatim in the given order.

    Returns:
        The concatenated statement text.
    """
    return "".join(parts)


def build_type_filter(relationship_types: list[str]) -> tuple[str, list[str]]:
    """Build a safe type filter clause with validated relationship types.

    Args:
        relationship_types: List of relationship type names to validate and use

    Returns:
        Tuple of (sql_fragment, params) where sql_fragment is like
        "AND r.relationship_type IN (?,?,?)" and params is the validated list

    Raises:
        ValueError: If any relationship type is invalid
    """
    if not relationship_types:
        return ("", [])

    # Validate each type
    validated_types = []
    for rt in relationship_types:
        validate_identifier(rt, VALID_RELATIONSHIP_TYPES, "relationship_type")
        validated_types.append(rt)

    slot_list = placeholders(len(validated_types))
    sql = join_sql_parts("AND r.relationship_type IN (", slot_list, ")")
    return (sql, validated_types)


def validate_identifier(name: str, valid_set: frozenset, context: str = "value") -> str:
    """Validate that an identifier is in the allowed set.

    Args:
        name: The identifier to validate
        valid_set: Set of allowed values
        context: Description for error message (e.g., "table", "relationship type")

    Returns:
        The validated name

    Raises:
        ValueError: If name is not in valid_set
    """
    if not isinstance(name, str):
        raise ValueError(f"{context} must be a string, got {type(name).__name__}")
    if name not in valid_set:
        raise ValueError(f"Invalid {context}: {name!r}. Must be one of: {sorted(valid_set)}")
    return name


def build_in_clause(values: list[str], valid_set: frozenset | None = None) -> tuple[str, list[str]]:
    """Build a safe IN clause with placeholders.

    Args:
        values: List of string values to include
        valid_set: Optional validation set to check against

    Returns:
        Tuple of (placeholder_string, validated_values)
        Example: ("?,?,?", ["val1", "val2", "val3"])

    Raises:
        ValueError: If any value fails validation
    """
    if not values:
        return ("", [])

    # Validate each value if valid_set is provided
    validated_values = []
    for value in values:
        if valid_set is not None:
            validate_identifier(value, valid_set, "value")
        validated_values.append(value)

    slot_list = placeholders(len(validated_values))
    return (slot_list, validated_values)


def is_valid_entity_type(entity_type: str) -> bool:
    """Check if entity type is valid."""
    return entity_type in VALID_ENTITY_TYPES


def is_valid_relationship_type(rel_type: str) -> bool:
    """Check if relationship type is valid."""
    return rel_type in VALID_RELATIONSHIP_TYPES


def is_valid_table_name(table_name: str) -> bool:
    """Check if table name is valid (for internal schema operations only)."""
    return table_name in VALID_TABLE_NAMES
