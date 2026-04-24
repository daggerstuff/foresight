# Implementation Plan: Circuit Breaker Pattern (foresight-mcp-s41)

## Issue
`hooks.py` only implements backoff retries, no circuit breaker - causes cascading failures on sustained outages.

## Files Requiring Changes
- `foresight_mcp/hooks.py` - Primary circuit breaker location
- `foresight_mcp/external_services.py` (create)

## Implementation Steps

### Step 1: Create Circuit Breaker Class

```python
# circuit_breaker.py
import time
import threading
from enum import Enum
from typing import Callable, Any, Optional
from dataclasses import dataclass
from functools import wraps

class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open" # Testing recovery

@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5          # Failures before opening
    recovery_timeout: float = 30.0      # Seconds before half-open
    half_open_max_calls: int = 3        # Test calls in half-open
    expected_exceptions: tuple = (Exception,)

class CircuitBreaker:
    def __init__(self, config: CircuitBreakerConfig = None):
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = threading.RLock()
        
        # Metrics
        self.total_requests = 0
        self.total_failures = 0
        self.total_rejected = 0
    
    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._check_state_transition()
    
    def _check_state_transition(self) -> CircuitState:
        """Check if state should transition (timeout expired)."""
        if self._state == CircuitState.OPEN and self._last_failure_time:
            elapsed = time.time() - self._last_failure_time
            if elapsed >= self.config.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._failure_count = 0
        return self._state
    
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with circuit breaker protection."""
        with self._lock:
            current_state = self._check_state_transition()
            self.total_requests += 1
            
            if current_state == CircuitState.OPEN:
                self.total_rejected += 1
                raise CircuitBreakerOpenError(f"Circuit breaker is OPEN")
            
            if current_state == CircuitState.HALF_OPEN:
                if self._success_count >= self.config.half_open_max_calls:
                    # Still testing, use spare call
                    pass  # Continue but track separately
        
        # Execute outside lock
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except self.config.expected_exceptions as e:
            self._on_failure()
            raise
    
    def _on_success(self):
        with self._lock:
            self._success_count += 1
            
            if self._state == CircuitState.HALF_OPEN:
                if self._success_count >= self.config.half_open_max_calls:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
            elif self._state == CircuitState.CLOSED:
                self._failure_count = max(0, self._failure_count - 1)
    
    def _on_failure(self):
        with self._lock:
            self._failure_count += 1
            self.total_failures += 1
            self._last_failure_time = time.time()
            
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._success_count = 0
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.config.failure_threshold:
                    self._state = CircuitState.OPEN
    
    def reset(self):
        """Reset circuit breaker to initial state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None

class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass

def with_circuit_breaker(config: CircuitBreakerConfig = None):
    """Decorator for circuit breaker protection."""
    breaker = CircuitBreaker(config)
    
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            return breaker.call(func, *args, **kwargs)
        wrapper.circuit_breaker = breaker
        return wrapper
    return decorator
```

### Step 2: Apply to hooks.py

```python
# hooks.py
from .circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerOpenError

# External service config
external_service_config = CircuitBreakerConfig(
    failure_threshold=5,
    recovery_timeout=30.0,
    half_open_max_calls=3,
    expected_exceptions=(ConnectionError, TimeoutError)
)

external_breaker = CircuitBreaker(external_service_config)

# In LLM call hooks
def call_llm_service(prompt: str):
    try:
        return external_breaker.call(_actual_llm_call, prompt)
    except CircuitBreakerOpenError:
        # Fallback: cached response or degraded mode
        logger.warning("Circuit breaker open, using fallback")
        return get_cached_response(prompt)
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        raise
```

### Step 3: Add Metrics Export

```python
# metrics.py (or integrate with existing)
from prometheus_client import Counter, Gauge, Histogram

CIRCUIT_BREAKER_STATE = Gauge(
    'circuit_breaker_state',
    'Circuit breaker state (0=closed, 1=open, 2=half_open)'
)

CIRCUIT_BREAKER_FAILURES = Counter(
    'circuit_breaker_failures_total',
    'Total circuit breaker failures'
)

CIRCUIT_BREAKER_REQUESTS = Counter(
    'circuit_breaker_requests_total',
    'Total circuit breaker requests'
)
```

## Verification Commands
```bash
# Test circuit breaker behavior
pytest tests/test_circuit_breaker.py -v

# Test cascading failure prevention
pytest tests/test_cascading_failure.py -v
```

## Acceptance Criteria
- [ ] Failures trigger circuit open after threshold
- [ ] Automatic recovery via half-open state
- [ ] Dashboard shows circuit state
- [ ] Fallback behavior on open circuit

## Related
- Issue: foresight-mcp-s41
- Audit finding: No circuit breaker (only backoff)
