# Implementation Plan: SQL Injection Fix (foresight-mcp-m62)

## Issue
F-string interpolation in SQL queries with user-provided data in `graph_store.py:479-520` and `server.py`.

## Files Requiring Changes
- `foresight_mcp/graph_store.py` - Entity/relationship queries
- `foresight_mcp/server.py` - All SQL queries with user input
- `foresight_mcp/temporal_service.py` - Temporal decay queries
- `foresight_mcp/hybrid_retriever.py` - Retrieval queries

## Implementation Steps

### Step 1: Create Parameterized Query Helper
```python
# In graph_store.py or utils/sql_helpers.py
def safe_query_params(table: str, column: str) -> str:
    """Validate table and column names to prevent injection in identifiers."""
    valid_tables = {'memories', 'entities', 'relationships', 'memory_entity_links'}
    valid_columns = {'id', 'content', 'user_id', 'tenant_id', 'entity_id', 'memory_id'}
    
    if table not in valid_tables:
        raise ValueError(f"Invalid table: {table}")
    if column not in valid_columns:
        raise ValueError(f"Invalid column: {column}")
    
    return f"{table}.{column}"


def build_parameterized_query(base: str, params: dict) -> tuple[str, list]:
    """Convert f-string style query to parameterized query."""
    placeholders = []
    values = []
    for key, value in params.items():
        placeholders.append(f":{key}")
        values.append(value)
    return base, values
```

### Step 2: Fix graph_store.py Entity Queries

**Before (line ~479-520):**
```python
query = f"""
    SELECT {column} FROM entities 
    WHERE entity_type = '{entity_type}' 
    AND tenant_id = '{tenant_id}'
"""
cursor.execute(query)
```

**After:**
```python
cursor.execute("""
    SELECT entity_name FROM entities 
    WHERE entity_type = ? AND tenant_id = ?
""", (entity_type, tenant_id))
```

### Step 3: Fix server.py Memory Queries

**Before:**
```python
cursor.execute(f"SELECT * FROM memories WHERE user_id = '{user_id}'")
```

**After:**
```python
cursor.execute("SELECT * FROM memories WHERE user_id = ?", (user_id,))
```

### Step 4: Add Input Validation Layer
```python
# At API boundary (server.py routes)
def validate_memory_id(memory_id: str) -> bool:
    """Validate memory_id format before use in SQL."""
    if not isinstance(memory_id, str):
        return False
    if len(memory_id) > 255:  # Reasonable max
        return False
    if not re.match(r'^[a-zA-Z0-9_-]+$', memory_id):
        return False
    return True
```

## Verification Commands
```bash
# Run security audit
./scripts/security-scan.sh

# Run tests
pytest tests/ -v

# SQL injection test suite
pytest tests/test_sql_injection.py -v
```

## Acceptance Criteria
- [ ] Zero f-string SQL interpolation in codebase
- [ ] All queries use parameterized statements (`?` placeholders)
- [ ] Test suite includes SQL injection attempts
- [ ] Security audit passes

## Related
- Issue: foresight-mcp-m62
- Audit finding: SQL Injection Risk (graph_store.py:479-520, server.py)
