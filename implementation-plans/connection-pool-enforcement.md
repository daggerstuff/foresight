# Implementation Plan: Connection Pool Enforcement (foresight-mcp-iej)

## Issue
Multiple modules bypass connection pool with direct `sqlite3.connect()` calls, causing potential connection exhaustion.

## Files Requiring Changes
- `foresight_mcp/graph_store.py`
- `foresight_mcp/temporal_service.py`
- `foresight_mcp/hybrid_retriever.py`
- `foresight_mcp/server.py`

## Implementation Steps

### Step 1: Audit Direct connect() Calls
```bash
# Find all direct sqlite3.connect() calls
rg "sqlite3\.connect\(" --type python
rg "sqlite3\.connect" --type python
```

### Step 2: Centralize Pool Access
Create singleton connection pool:
```python
# connection_pool.py
import sqlite3
from contextlib import contextmanager
from threading import Lock

class ConnectionPool:
    _instance = None
    _lock = Lock()
    
    def __new__(cls, db_path: str = None, max_connections: int = 10):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, db_path: str = None, max_connections: int = 10):
        if self._initialized:
            return
        self.db_path = db_path or os.environ.get('DATABASE_PATH', 'foresight.db')
        self.max_connections = max_connections
        self._pool = []
        self._in_use = 0
        self._lock = Lock()
        self._initialized = True
    
    @contextmanager
    def get_connection(self):
        """Get connection from pool with automatic cleanup."""
        conn = None
        try:
            with self._lock:
                if self._in_use >= self.max_connections:
                    raise RuntimeError("Connection pool exhausted")
                self._in_use += 1
            
            if self._pool:
                conn = self._pool.pop()
            else:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
            
            yield conn
        finally:
            if conn:
                with self._lock:
                    self._in_use -= 1
                    self._pool.append(conn)
    
    def execute(self, query: str, params: tuple = None):
        """Execute query with auto-managed connection."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            return cursor
```

### Step 3: Replace Direct Calls

**Before:**
```python
# In graph_store.py
conn = sqlite3.connect('database.db')
try:
    cursor = conn.cursor()
    cursor.execute("SELECT ...")
finally:
    conn.close()
```

**After:**
```python
from .connection_pool import pool

pool = ConnectionPool.get_instance()

# In graph_store.py
with pool.get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT ...", params)
```

### Step 4: Add Pool Metrics
```python
# Add to connection_pool.py
from prometheus_client import Counter, Gauge

POOL_REQUESTS = Counter('db_pool_requests_total', 'Total pool requests')
POOL_EXHAUSTED = Counter('db_pool_exhausted_total', 'Times pool exhausted')
POOL_SIZE = Gauge('db_pool_size', 'Current pool size')
```

## Verification Commands
```bash
# Ensure no direct connect calls
rg "sqlite3\.connect\(" --type python

# Load test connection handling
pytest tests/test_connection_pool.py::test_pool_exhaustion -v
```

## Acceptance Criteria
- [ ] Zero direct sqlite3.connect() outside connection_pool.py
- [ ] All connections tracked by pool
- [ ] Pool exhaustion alerts configured
- [ ] Metrics exported to Prometheus

## Related
- Issue: foresight-mcp-iej
- Audit finding: Connection Pool Bypass
