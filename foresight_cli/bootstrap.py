"""
Bootstrap the foresight package from .pyc bytecode.

The foresight source tree has been compiled to .pyc bytecode in
__pycache__ directories but the corresponding .py source files were removed.
This module pre-loads all necessary foresight modules from bytecode
so CLI imports resolve correctly.

Usage:
    from foresight_cli.bootstrap import ensure_loaded
    ensure_loaded()
"""

from __future__ import annotations

import importlib._bootstrap_external
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import types
import warnings
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_MCP_DIR = _HERE.parent / "foresight"
_PYCACHE = _MCP_DIR / "__pycache__"
_BACKEND_PYCACHE = _MCP_DIR / "backend" / "__pycache__"
_LLM_PYCACHE = _MCP_DIR / "llm_providers" / "__pycache__"

EXCLUDED_MODULES = frozenset(
    {
        "__main__",
        "eval_harness",
        "eval",
    }
)

_loaded = False


def _discover_pyc_files() -> dict[str, Path]:
    """Map qualified module names to their .pyc file paths."""
    result: dict[str, Path] = {}

    def scan(pyc_dir: Path, prefix: str) -> None:
        if pyc_dir.exists():
            for p in sorted(pyc_dir.glob("*.cpython-313.pyc")):
                name = p.stem.split(".")[0]
                if name not in EXCLUDED_MODULES:
                    result[f"{prefix}.{name}"] = p

    scan(_PYCACHE, "foresight")
    scan(_BACKEND_PYCACHE, "foresight.backend")
    scan(_LLM_PYCACHE, "foresight.llm_providers")
    scan(_MCP_DIR / "websocket" / "__pycache__", "foresight.websocket")
    scan(_MCP_DIR / "migrations" / "__pycache__", "foresight.migrations")

    return result


class _PycLoader(importlib.abc.Loader):
    """Sourceless loader that loads from the correct .pyc path."""

    def __init__(self, fullname: str, pyc_path: Path) -> None:
        self.fullname = fullname
        self.pyc_path = pyc_path
        self._inner = importlib._bootstrap_external.SourcelessFileLoader(fullname, str(pyc_path))

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType | None:
        return self._inner.create_module(spec)

    def exec_module(self, module: types.ModuleType) -> None:
        self._inner.exec_module(module)


class _PycFinder(importlib.abc.MetaPathFinder):
    """Meta path finder that resolves foresight modules from .pyc files."""

    def __init__(self) -> None:
        self._pyc_map: dict[str, Path] = {}

    def update_map(self, pyc_map: dict[str, Path]) -> None:
        self._pyc_map = pyc_map

    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname not in self._pyc_map:
            return None
        pyc_path = self._pyc_map[fullname]
        loader = _PycLoader(fullname, pyc_path)  # type: ignore[arg-type]
        spec = importlib.util.spec_from_loader(fullname, loader, origin=str(pyc_path))
        if spec is None:
            return None
        return spec


_finder = _PycFinder()

# Also monkeypatch sys.meta_path to allow server.py imports to resolve
_original_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__


def _bootstrap_import(name, *args, **kwargs):
    """Custom __import__ that loads .pyc files when .py is missing."""
    try:
        return _original_import(name, *args, **kwargs)
    except ModuleNotFoundError:
        pass
    # Check if we have the .pyc for this module
    mod_name = str(name)
    if mod_name in _finder._pyc_map and mod_name not in sys.modules:
        _finder.find_spec(mod_name)
        # Import again now that we've set up the spec
        return _original_import(name, *args, **kwargs)
    raise


def ensure_loaded() -> None:
    """Load foresight modules from .pyc bytecode.

    Safe to call multiple times — only runs once.
    """
    global _loaded
    if _loaded:
        return

    pyc_map = _discover_pyc_files()
    _finder.update_map(pyc_map)

    # Install the meta-path finder at position 1 (before the default path-based finder)
    if _finder not in sys.meta_path:
        sys.meta_path.insert(1, _finder)

    # Register backend package
    backend_pkg_path = _MCP_DIR / "backend"
    if backend_pkg_path.exists() and "foresight.backend" not in sys.modules:
        backend_pkg = types.ModuleType("foresight.backend")
        backend_pkg.__path__ = [str(backend_pkg_path)]
        sys.modules["foresight.backend"] = backend_pkg

    # Register subpackages (needed for relative imports in server.py)
    subpackages = {
        "foresight.backend": _MCP_DIR / "backend",
        "foresight.llm_providers": _MCP_DIR / "llm_providers",
        "foresight.websocket": _MCP_DIR / "websocket",
        "foresight.migrations": _MCP_DIR / "migrations",
    }
    for pkg_name, pkg_path in subpackages.items():
        if pkg_path.exists() and pkg_name not in sys.modules:
            ns_pkg = types.ModuleType(pkg_name)
            ns_pkg.__path__ = [str(pkg_path)]
            sys.modules[pkg_name] = ns_pkg

    # Register the foresight package itself (needed for relative imports)
    if "foresight" not in sys.modules:
        mcp_pkg = types.ModuleType("foresight")
        mcp_pkg.__path__ = [str(_MCP_DIR)]
        sys.modules["foresight"] = mcp_pkg

    # Import leaf modules first (fewest dependencies)
    leaf_order = [
        "foresight.config",
        "foresight.schema",
        "foresight.connection_pool",
        "foresight.tenant_context",
        "foresight.sql_helpers",
        "foresight.rate_limiter",
        "foresight.tenant_middleware",
        "foresight.event_bus",
        "foresight.llm_errors",
        "foresight.auth",
        "foresight.decay_model",
        "foresight.graph_store",
        "foresight.embedding_validation",
        "foresight.hybrid_retriever",
        "foresight.injection_budget",
        "foresight.rrf_tuning",
        "foresight.circuit_breaker",
        "foresight.entity_extractor",
        "foresight.memory_types",
        "foresight.memory_components",
        "foresight.enhanced_synthesizer",
        "foresight.crisis_detection",
        "foresight.block_registry",
        "foresight.context_blocks",
        "foresight.document_layer",
        "foresight.reflection_engine",
        "foresight.reflection_narrative",
        "foresight.semantic_search",
        "foresight.stream_producer",
        "foresight.phrase_triggers",
        "foresight.memory_gc",
        "foresight.narrative_cache",
        "foresight.ghost_cleanup",
        "foresight.graph_edge_decay",
        "foresight.cluster_service",
        "foresight.memory_maintenance",
        "foresight.temporal_schema",
        "foresight.temporal_service",
        "foresight.maintenance_eval",
        "foresight.crdt",
        "foresight.consumer_group",
        "foresight.sync",
        "foresight.capture",
        "foresight.clustering",
        "foresight.temporal_queries",
        "foresight.llm_client",
        "foresight.profile_synthesizer",
        "foresight.hooks",
        "foresight.memory_relationships",
        "foresight.audit",
        "foresight.subconscious",
        "foresight.narrative_cache",
        "foresight.backend.__init__",
    ]

    for mod_name in leaf_order:
        if mod_name in sys.modules:
            continue
        try:
            __import__(mod_name)
        except ModuleNotFoundError:
            # Circular dep or truly missing — skip
            pass
        except Exception:
            warnings.warn(f"Could not load {mod_name}", stacklevel=2, source=None)

    # Sync __init__ sub-module attributes to parent packages.
    # Python's import system normally does this when loading from .py files,
    # but our SourcelessFileLoader doesn't trigger the standard attribute
    # propagation. We need to copy __init__ exports to the parent package.
    for pkg in [
        "foresight.backend",
        "foresight.llm_providers",
        "foresight.websocket",
        "foresight.migrations",
        "foresight",
    ]:
        _sync_init_exports(pkg)

    _loaded = True


def _sync_init_exports(package_name: str) -> None:
    """Copy attributes from ``<package>.__init__`` to ``<package>``.

    When a sub-module ``<package>.__init__`` is loaded from .pyc bytecode,
    its attrs are not automatically propagated to the package namespace
    module (``<package>``). This function manually syncs them.
    """
    pkg = sys.modules.get(package_name)
    init_mod = sys.modules.get(f"{package_name}.__init__")
    if pkg is None or init_mod is None:
        return

    # Ensure __path__ matches
    if hasattr(pkg, "__path__") and hasattr(init_mod, "__path__") and not pkg.__path__:
        pkg.__path__ = init_mod.__path__

    # Copy public symbols from __init__ to the package
    for attr_name in dir(init_mod):
        if attr_name.startswith("_"):
            continue
        if hasattr(pkg, attr_name):
            continue  # Don't overwrite
        setattr(pkg, attr_name, getattr(init_mod, attr_name))
