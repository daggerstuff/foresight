# Implementation Plan: Race Condition Fix (foresight-mcp-9h5)

## Issue
`temporal_service.py` and `hybrid_retriever.py` have potential double-initialization race conditions in global state initialization.

## Files Requiring Changes
- `foresight_mcp/temporal_service.py`
- `foresight_mcp/hybrid_retriever.py`
- `foresight_mcp/connection_pool.py`

## Implementation Steps

### Step 1: Identify Race Conditions

**In temporal_service.py (line ~26-50):**
```python
# Global state - NOT thread-safe
_decay_config = None
_temporal_index = {}

def initialize_temporal_service():
    global _decay_config
    if _decay_config is None:  # Race condition here!
        _decay_config = DecayConfig()  # Multiple threads can pass None check
```

**In hybrid_retriever.py (line ~45-60):**
```python
_keyword_index = None
def _build_keyword_index():
    global _keyword_index
    if _keyword_index is None:  # Same race condition
        _keyword_index = {}
```

### Step 2: Add Thread-Safe Singleton Pattern

```python
# temporal_service.py
import threading
from typing import Optional

class TemporalService:
    _instance: Optional['TemporalService'] = None
    _lock = threading.RLock()
    _initialized = False
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        # __init__ can be called multiple times, protect it
        if self._initialized:
            return
        
        with self._lock:
            # Double-check after acquiring lock
            if self._initialized:
                return
            
            self._decay_config = DecayConfig()
            self._temporal_index = {}
            self._initialized = True
    
    @classmethod
    def reset(cls):
        """Reset for testing."""
        with cls._lock:
            cls._instance = None
            cls._initialized = False
```

### Step 3: Fix hybrid_retriever.py

```python
# hybrid_retriever.py
class HybridRetriever:
    _instance: Optional['HybridRetriever'] = None
    _lock = threading.RLock()
    _initialized = False
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        with self._lock:
            if self._initialized:
                return
            
            self._keyword_index = {}
            self._inverted_index = {}
            self._initialized = True
```

### Step 4: Add Concurrency Tests

```python
# tests/test_concurrency.py
import threading
import pytest
from temporal_service import TemporalService
from hybrid_retriever import HybridRetriever

def test_temporal_service_concurrent_init():
    """Test that concurrent initialization doesn't cause issues."""
    errors = []
    instances = []
    
    def init_worker():
        try:
            service = TemporalService()
            instances.append(id(service))
        except Exception as e:
            errors.append(e)
    
    threads = [threading.Thread(target=init_worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    assert len(errors) == 0
    # All should reference same instance
    assert len(set(instances)) == 1

def test_hybrid_retriever_concurrent_init():
    """Test HybridRetriever thread safety."""
    errors = []
    
    def retrieve_worker():
        try:
            retriever = HybridRetriever()
            retriever.initialize()
        except Exception as e:
            errors.append(e)
    
    threads = [threading.Thread(target=retrieve_worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    assert len(errors) == 0
```

## Verification Commands
```bash
# Run concurrency tests
pytest tests/test_concurrency.py -v

# Run race condition stress test
pytest tests/test_concurrency.py::test_temporal_service_concurrent_init -v --count=10
```

## Acceptance Criteria
- [ ] Zero race conditions under concurrent load
- [ ] All shared state protected by RLock
- [ ] Concurrency test suite passes
- [ ] 100 concurrent initializes produce single instance

## Related
- Issue: foresight-mcp-9h5
- Audit finding: Race condition in global state init
