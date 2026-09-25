"""
Semantic Vector Search for Memories (MEM-5).

Privacy-first embedding pipeline. Stores vectors in a `memory_embeddings`
table keyed by memory_id and computes cosine similarity for retrieval.
The default provider is a local ONNX sentence-embedding model, so real
semantic similarity is available with no query-time network egress; the
zero-dependency feature-hashing embedder remains as a deterministic
fallback for offline installs and tests.

Providers:
- ``fastembed``: local ONNX sentence-embedding models (default is
  ``BAAI/bge-small-en-v1.5``, 384-dim). Real semantic similarity with
  **no network egress at query time** — the ONNX weights are downloaded
  once by fastembed, then inference is local. Preferred default whenever
  the optional ``fastembed`` dependency is installed.
- ``local-hash``: deterministic 384-dim feature-hashing embedder. No
  external dependencies, no model downloads. Lexical (a bag-of-words
  projection), not semantic. Retained as the zero-dependency fallback and
  for deterministic tests.
- ``openai``: remote API embeddings (default ``text-embedding-3-small``,
  1536-dim). Opt-in only — it breaks the no-egress guarantee.

Provider selection
------------------
``FORESIGHT_EMBEDDING_PROVIDER`` selects a provider explicitly
(``fastembed`` | ``local-hash`` | ``openai``). The default value ``auto``
resolves to ``fastembed`` when that dependency is importable and falls
back to ``local-hash`` otherwise, so installing the extra upgrades
retrieval quality with no configuration while a bare install keeps
working unchanged.

Both defaults are 384-dimensional, so switching between ``local-hash``
and ``fastembed`` needs no schema migration. The ``provider`` column is
part of the ``memory_embeddings`` primary key, so vectors from different
providers are stored and queried independently and never compared
against each other.

Embeddings are dimension-validated against `embedding_validation.py` so
that swapping in a real model (e.g. bge-large-en-v1.5) is a drop-in change.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import struct
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .config import DB_PATH, DB_URL
from .connection_pool import get_pool
from .embedding_validation import (
    EMBEDDING_DIMENSIONS,
    EmbeddingDimensionError,
    validate_embedding_dimension,
)
from .tenant_context import get_current_account_id

logger = logging.getLogger("foresight_semantic_search")

LOCAL_HASH_DIM = 384
DEFAULT_PROVIDER = "local-hash"

# Local real-semantics provider (optional dependency; no query-time egress).
FASTEMBED_PROVIDER = "fastembed"
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"

# Remote provider, opt-in only — breaks the no-egress guarantee.
OPENAI_PROVIDER = "openai"
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

# Sentinel meaning "choose the best available provider".
AUTO_PROVIDER = "auto"

VALID_PROVIDERS: frozenset[str] = frozenset({DEFAULT_PROVIDER, FASTEMBED_PROVIDER, OPENAI_PROVIDER})

MAX_TEXT_LENGTH = 100_000
MAX_USER_ID_LENGTH = 128
MAX_TENANT_ID_LENGTH = 64
MAX_MEMORY_ID_LENGTH = 128

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class SemanticSearchError(ValueError):
    """Raised on invalid input or constraint violations."""


class Embedder(Protocol):
    """Protocol for embedding providers."""

    provider_name: str
    dimension: int

    def embed(self, text: str) -> list[float]: ...


class LocalHashEmbedder:
    """Deterministic 384-dim feature-hashing embedder.

    Tokenizes text into lowercase alnum tokens, applies signed feature
    hashing with murmurhash3-style mixing, and L2-normalizes the result.
    No external dependencies, no network calls, no model downloads.

    Quality: discriminative for short clinical/factual text; not a
    replacement for a learned model. Intended as a privacy-first default
    that can be swapped for a real model via the Embedder protocol.
    """

    provider_name = DEFAULT_PROVIDER
    dimension = LOCAL_HASH_DIM

    _NORM_EPS = 1e-12

    def embed(self, text: str) -> list[float]:
        """Produce a 384-dim unit vector for the given text."""
        if not isinstance(text, str):
            raise SemanticSearchError("text must be a string")
        if len(text) > MAX_TEXT_LENGTH:
            raise SemanticSearchError(f"text exceeds {MAX_TEXT_LENGTH} chars")

        vec = [0.0] * self.dimension
        for token, count in self._token_counts(text).items():
            h1, h2 = self._hash_token(token)
            idx = h1 % self.dimension
            sign = 1.0 if (h2 & 1) == 0 else -1.0
            vec[idx] += sign * (1.0 + math.log1p(count - 1))

        return self._l2_normalize(vec)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts (sequential; this provider is cheap)."""
        return [self.embed(text) for text in texts]

    @staticmethod
    def _token_counts(text: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for token in _TOKEN_RE.findall(text.lower()):
            counts[token] = counts.get(token, 0) + 1
        return counts

    @staticmethod
    def _hash_token(token: str) -> tuple[int, int]:
        """Two independent 32-bit hashes for index and sign."""
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        a, b = struct.unpack("<II", digest)
        return a, b

    @staticmethod
    def _l2_normalize(vec: list[float]) -> list[float]:
        norm = math.sqrt(sum(v * v for v in vec))
        if norm < LocalHashEmbedder._NORM_EPS:
            return vec
        return [v / norm for v in vec]


def _env(name: str, default: str = "") -> str:
    """Read a stripped environment variable."""
    return os.environ.get(name, default).strip()


def _validate_embed_text(text: str) -> None:
    """Shared input validation for embedding providers."""
    if not isinstance(text, str):
        raise SemanticSearchError("text must be a string")
    if not text.strip():
        raise SemanticSearchError("text must be a non-empty string")
    if len(text) > MAX_TEXT_LENGTH:
        raise SemanticSearchError(f"text exceeds {MAX_TEXT_LENGTH} chars")


def _fastembed_available() -> bool:
    """True when the optional fastembed dependency can be imported."""
    try:
        import fastembed  # noqa: F401
    except Exception:
        return False
    return True


def resolve_provider(requested: str | None = None) -> str:
    """Resolve a requested provider name to a concrete, available provider.

    Resolution order: the explicit ``requested`` argument, then
    ``FORESIGHT_EMBEDDING_PROVIDER``, then ``auto``.

    ``auto`` selects ``fastembed`` when the optional dependency is
    importable and ``local-hash`` otherwise, so installing the extra
    upgrades retrieval quality with no configuration while a bare install
    keeps working unchanged.

    Under pytest, ``auto`` always resolves to ``local-hash`` so the suite
    stays hermetic, fast, and offline — otherwise fastembed would download
    ONNX weights on first use. Set ``FORESIGHT_EMBEDDING_PROVIDER``
    explicitly to exercise a real provider under test.
    """
    name = (requested or "").strip().lower() or _env("FORESIGHT_EMBEDDING_PROVIDER") or AUTO_PROVIDER
    if name == AUTO_PROVIDER:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return DEFAULT_PROVIDER
        return FASTEMBED_PROVIDER if _fastembed_available() else DEFAULT_PROVIDER
    if name not in VALID_PROVIDERS:
        raise SemanticSearchError(
            f"unknown embedder provider {name!r}; valid: {sorted(VALID_PROVIDERS | {AUTO_PROVIDER})}"
        )
    return name


class FastEmbedEmbedder:
    """Local ONNX sentence embedder via the optional ``fastembed`` package.

    Real semantic similarity with **no query-time network egress**: model
    weights are fetched once and cached on disk by fastembed, after which
    all inference runs locally in-process.

    Defaults to ``BAAI/bge-small-en-v1.5`` (384-dim), which is
    dimension-compatible with :class:`LocalHashEmbedder`, so enabling it
    needs no schema migration. Because ``provider`` participates in the
    ``memory_embeddings`` primary key, previously stored hashed vectors are
    neither compared against nor overwritten.
    """

    provider_name = FASTEMBED_PROVIDER
    _NORM_EPS = 1e-12

    def __init__(self, model_name: str | None = None, cache_dir: str | None = None) -> None:
        try:
            from fastembed import TextEmbedding
        except Exception as exc:  # pragma: no cover - depends on install
            raise SemanticSearchError(
                "embedder provider 'fastembed' requires an optional dependency; "
                "install it with: pip install 'foresight[embeddings]'"
            ) from exc

        self.model_name = model_name or _env("FORESIGHT_EMBEDDING_MODEL") or DEFAULT_FASTEMBED_MODEL
        self.cache_dir = cache_dir or _env("FORESIGHT_EMBEDDING_CACHE") or None

        kwargs: dict[str, Any] = {}
        if self.cache_dir:
            kwargs["cache_dir"] = self.cache_dir
        try:
            self._model = TextEmbedding(model_name=self.model_name, **kwargs)
        except Exception as exc:
            raise SemanticSearchError(f"failed to load fastembed model {self.model_name!r}: {exc}") from exc

        self.dimension = self._detect_dimension()

    def _detect_dimension(self) -> int:
        """Output width of the model.

        Prefers the static dimension table (no inference required) and
        probes the model only for unknown or renamed checkpoints.
        """
        known = EMBEDDING_DIMENSIONS.get(self.model_name.rsplit("/", 1)[-1])
        if known:
            return known
        return len(next(iter(self._model.embed(["dimension probe"]))))

    def embed(self, text: str) -> list[float]:
        """Produce a unit-length embedding vector for the given text."""
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts in a single forward pass."""
        if not texts:
            return []
        for text in texts:
            _validate_embed_text(text)
        vectors = [[float(v) for v in vec] for vec in self._model.embed(list(texts))]
        return [self._l2_normalize(vec) for vec in vectors]

    @staticmethod
    def _l2_normalize(vec: list[float]) -> list[float]:
        norm = math.sqrt(sum(v * v for v in vec))
        if norm < FastEmbedEmbedder._NORM_EPS:
            return vec
        return [v / norm for v in vec]


class OpenAIEmbedder:
    """Remote embeddings via an OpenAI-compatible ``/embeddings`` endpoint.

    Opt-in only: memory text leaves the process, which breaks Foresight's
    default no-egress guarantee. Retained for deployments that knowingly
    trade privacy for the higher retrieval quality of a hosted model.
    """

    provider_name = OPENAI_PROVIDER
    _NORM_EPS = 1e-12
    _TIMEOUT_SECONDS = 30.0
    _MAX_BATCH = 128

    def __init__(
        self,
        model_name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model_name = model_name or _env("FORESIGHT_EMBEDDING_MODEL") or DEFAULT_OPENAI_EMBEDDING_MODEL
        self.base_url = (base_url or _env("FORESIGHT_EMBEDDING_BASE_URL") or DEFAULT_OPENAI_BASE_URL).rstrip("/")
        self.api_key = api_key or _env("OPENAI_API_KEY")
        if not self.api_key:
            raise SemanticSearchError(
                "embedder provider 'openai' requires OPENAI_API_KEY; use 'fastembed' to keep inference local"
            )
        self.dimension = len(self.embed("dimension probe"))

    def embed(self, text: str) -> list[float]:
        """Produce a unit-length embedding vector for the given text."""
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, chunked to the provider's batch limit."""
        if not texts:
            return []
        for text in texts:
            _validate_embed_text(text)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._MAX_BATCH):
            vectors.extend(self._request(texts[start : start + self._MAX_BATCH]))
        return [self._l2_normalize(vec) for vec in vectors]

    def _request(self, texts: list[str]) -> list[list[float]]:
        """POST one batch to the embeddings endpoint."""
        import httpx

        try:
            response = httpx.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model_name, "input": texts},
                timeout=self._TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise SemanticSearchError(f"embedding request failed: {exc}") from exc
        if response.status_code != 200:
            raise SemanticSearchError(f"embedding request failed: HTTP {response.status_code}: {response.text[:200]}")
        try:
            rows = sorted(response.json()["data"], key=lambda row: row.get("index", 0))
            return [[float(x) for x in row["embedding"]] for row in rows]
        except Exception as exc:
            raise SemanticSearchError(f"malformed embedding response: {exc}") from exc

    @staticmethod
    def _l2_normalize(vec: list[float]) -> list[float]:
        norm = math.sqrt(sum(v * v for v in vec))
        if norm < OpenAIEmbedder._NORM_EPS:
            return vec
        return [v / norm for v in vec]


# Embedders are cached per (provider, model). Constructing a fastembed
# embedder loads ONNX weights, which must not happen on every call.
_EMBEDDER_CACHE: dict[tuple[str, str], Embedder] = {}
_EMBEDDER_CACHE_LOCK = threading.Lock()


def _build_embedder(provider: str) -> Embedder:
    """Instantiate a concrete embedder for an already-resolved provider."""
    if provider == DEFAULT_PROVIDER:
        return LocalHashEmbedder()
    if provider == FASTEMBED_PROVIDER:
        return FastEmbedEmbedder()
    if provider == OPENAI_PROVIDER:
        return OpenAIEmbedder()
    raise SemanticSearchError(f"unknown embedder provider {provider!r}; valid: {sorted(VALID_PROVIDERS)}")


def get_embedder(provider: str | None = None) -> Embedder:
    """Return a cached embedder for the given or auto-resolved provider."""
    name = resolve_provider(provider)
    model_hint = "" if name == DEFAULT_PROVIDER else _env("FORESIGHT_EMBEDDING_MODEL")
    key = (name, model_hint)
    with _EMBEDDER_CACHE_LOCK:
        cached = _EMBEDDER_CACHE.get(key)
        if cached is not None:
            return cached
        embedder = _build_embedder(name)
        _EMBEDDER_CACHE[key] = embedder
        return embedder


def reset_embedder_cache() -> None:
    """Drop cached embedder instances (test-only helper)."""
    with _EMBEDDER_CACHE_LOCK:
        _EMBEDDER_CACHE.clear()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity for two equal-length vectors."""
    if len(a) != len(b):
        raise SemanticSearchError(f"vector length mismatch: {len(a)} vs {len(b)}")
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def serialize_vector(vec: list[float]) -> bytes:
    """Pack a float vector into compact little-endian float32 bytes."""
    if len(vec) > 65_535:
        raise SemanticSearchError("vector too large to serialize")
    return struct.pack(f"<{len(vec)}f", *vec)


def deserialize_vector(blob: bytes, expected_dim: int) -> list[float]:
    """Unpack a float32 blob into a list, validating dimension."""
    if len(blob) != expected_dim * 4:
        raise SemanticSearchError(f"vector blob size {len(blob)} != expected {expected_dim * 4}")
    return list(struct.unpack(f"<{expected_dim}f", blob))


@dataclass
class SemanticMatch:
    """A single semantic search match."""

    memory_id: str
    score: float
    provider: str
    dimension: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "score": round(self.score, 6),
            "provider": self.provider,
            "dimension": self.dimension,
        }


@dataclass
class SemanticSearchResult:
    """Result of a semantic vector search."""

    query: str
    provider: str
    dimension: int
    matches: list[SemanticMatch] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "dimension": self.dimension,
            "matches": [m.to_dict() for m in self.matches],
        }


@dataclass
class SemanticSearchOptions:
    """Options for semantic search."""

    tenant_id: str | None = None
    limit: int = 10
    min_score: float = 0.0
    provider: str | None = None


def _validate_user_tenant(user_id: str, tenant_id: str) -> None:
    if not user_id or len(user_id) > MAX_USER_ID_LENGTH:
        raise SemanticSearchError(f"user_id must be 1-{MAX_USER_ID_LENGTH} chars")
    if not tenant_id or len(tenant_id) > MAX_TENANT_ID_LENGTH:
        raise SemanticSearchError(f"tenant_id must be 1-{MAX_TENANT_ID_LENGTH} chars")


def _validate_memory_id(memory_id: str) -> None:
    if not memory_id or len(memory_id) > MAX_MEMORY_ID_LENGTH:
        raise SemanticSearchError(f"memory_id must be 1-{MAX_MEMORY_ID_LENGTH} chars")


class SemanticSearch:
    """SQLite-backed semantic vector store with pluggable embedder."""

    def __init__(
        self,
        db_path: str,
        embedder: Embedder | None = None,
        provider: str | None = None,
    ) -> None:
        self.db_path = db_path
        # Resolve once, so self.provider always names the provider whose
        # vectors this store reads and writes (never the "auto" sentinel).
        self.provider = resolve_provider(provider)
        self.embedder: Embedder = embedder or get_embedder(self.provider)
        if self.embedder.provider_name != self.provider:
            raise SemanticSearchError(
                f"embedder.provider_name {self.embedder.provider_name!r} does not match requested provider {self.provider!r}"
            )
        self.dimension = self.embedder.dimension
        self._lock = threading.Lock()
        self._ensure_table()

    def _connect(self) -> Any:
        pool = get_pool(self.db_path)
        return pool.acquire()

    def _ensure_table(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    memory_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    user_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    model_version TEXT DEFAULT '1',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, user_id, memory_id, provider)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_user "
                "ON memory_embeddings(tenant_id, user_id, provider)"
            )
            conn.commit()
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()

    def index_memory(
        self,
        memory_id: str,
        text: str,
        user_id: str,
        tenant_id: str | None = None,
        provider: str | None = None,
    ) -> int:
        """Compute and store (or replace) the embedding for a memory."""
        _validate_memory_id(memory_id)
        if text is None or not text.strip():
            raise SemanticSearchError("text must be a non-empty string")
        tid = tenant_id or get_current_account_id()
        _validate_user_tenant(user_id, tid)
        prov = provider or self.provider
        embedder = self.embedder if prov == self.provider else get_embedder(prov)
        vec = embedder.embed(text)
        try:
            validate_embedding_dimension(vec, expected_dimension=embedder.dimension)
        except EmbeddingDimensionError as exc:
            raise SemanticSearchError(str(exc)) from exc

        blob = serialize_vector(vec)
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            with self._lock:
                conn.execute(
                    """
                    INSERT INTO memory_embeddings (
                        memory_id, tenant_id, user_id,
                        provider, dimension, vector,
                        model_version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, '1', ?, ?)
                    ON CONFLICT(tenant_id, user_id, memory_id, provider)
                    DO UPDATE SET
                        vector = excluded.vector,
                        dimension = excluded.dimension,
                        updated_at = excluded.updated_at
                    """,
                    (
                        memory_id,
                        tid,
                        user_id,
                        prov,
                        embedder.dimension,
                        blob,
                        now,
                        now,
                    ),
                )
                conn.commit()
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()

        return embedder.dimension

    def delete_memory_embedding(
        self,
        memory_id: str,
        user_id: str,
        tenant_id: str | None = None,
        provider: str | None = None,
    ) -> int:
        """Remove the embedding for a memory. Returns rows deleted."""
        _validate_memory_id(memory_id)
        tid = tenant_id or get_current_account_id()
        _validate_user_tenant(user_id, tid)
        prov = provider or self.provider
        conn = self._connect()
        try:
            with self._lock:
                cur = conn.execute(
                    """
                    DELETE FROM memory_embeddings
                    WHERE tenant_id = ? AND user_id = ?
                      AND memory_id = ? AND provider = ?
                    """,
                    (tid, user_id, memory_id, prov),
                )
                conn.commit()
                return cur.rowcount
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()

    def search(
        self,
        query: str,
        user_id: str,
        *,
        options: SemanticSearchOptions | None = None,
        **overrides,
    ) -> SemanticSearchResult:
        """Semantic search by cosine similarity over stored vectors."""
        # Use options if provided, otherwise fall back to individual parameters
        if options is None:
            options = SemanticSearchOptions()

        # Apply overrides
        options_dict = {
            "tenant_id": options.tenant_id,
            "limit": options.limit,
            "min_score": options.min_score,
            "provider": options.provider,
        }
        options_dict.update(overrides)

        tenant_id = options_dict["tenant_id"]
        limit = options_dict["limit"]
        min_score = options_dict["min_score"]
        provider = options_dict["provider"]

        if not query or not query.strip():
            raise SemanticSearchError("query must be a non-empty string")
        if limit < 1 or limit > 1000:
            raise SemanticSearchError("limit must be in [1, 1000]")
        if min_score < -1.0 or min_score > 1.0:
            raise SemanticSearchError("min_score must be in [-1.0, 1.0]")
        tid = tenant_id or get_current_account_id()
        _validate_user_tenant(user_id, tid)
        prov = provider or self.provider
        embedder = self.embedder if prov == self.provider else get_embedder(prov)

        query_vec = embedder.embed(query)
        try:
            validate_embedding_dimension(query_vec, expected_dimension=embedder.dimension)
        except EmbeddingDimensionError as exc:
            raise SemanticSearchError(str(exc)) from exc

        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT memory_id, vector, dimension
                FROM memory_embeddings
                WHERE tenant_id = ? AND user_id = ? AND provider = ?
                """,
                (tid, user_id, prov),
            ).fetchall()
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()

        matches: list[SemanticMatch] = []
        for r in rows:
            dim = int(r["dimension"])
            if dim != embedder.dimension:
                logger.warning(
                    "Skipping memory %s: dim %d != embedder dim %d",
                    r["memory_id"],
                    dim,
                    embedder.dimension,
                )
                continue
            vec = deserialize_vector(bytes(r["vector"]), dim)
            score = cosine_similarity(query_vec, vec)
            if score >= min_score:
                matches.append(
                    SemanticMatch(
                        memory_id=r["memory_id"],
                        score=score,
                        provider=prov,
                        dimension=dim,
                    )
                )

        matches.sort(key=lambda m: m.score, reverse=True)
        return SemanticSearchResult(
            query=query,
            provider=prov,
            dimension=embedder.dimension,
            matches=matches[:limit],
        )


class _SemanticSearchSingleton:
    """Module-level singleton for SemanticSearch."""

    _instance: SemanticSearch | None = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, provider: str | None = None) -> SemanticSearch:
        """Return the process-singleton SemanticSearch, initializing lazily."""
        resolved = resolve_provider(provider)
        with cls._lock:
            if cls._instance is None or cls._instance.provider != resolved:
                db_ref = DB_PATH or DB_URL
                if not db_ref:
                    raise SemanticSearchError(
                        "Cannot initialize SemanticSearch: neither DB_PATH nor DB_URL is set. "
                        "Set FORESIGHT_DB_URL for Postgres or FORESIGHT_DB_PATH for SQLite."
                    )
                cls._instance = SemanticSearch(db_ref, provider=resolved)
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (test-only helper)."""
        with cls._lock:
            cls._instance = None


def get_semantic_search(provider: str | None = None) -> SemanticSearch:
    """Return the process-singleton SemanticSearch, initializing lazily."""
    return _SemanticSearchSingleton.get_instance(provider)


def reset_semantic_search() -> None:
    """Reset the singleton (test-only helper)."""
    _SemanticSearchSingleton.reset()
