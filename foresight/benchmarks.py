"""PIX-4705: Reproducible, self-hosted memory benchmarks (LoCoMo + LongMemEval).

This module measures a memory system's retrieval and answer quality against the
public LoCoMo and LongMemEval datasets using **deterministic, judge-independent**
metrics, so numbers published from it can be reproduced by anyone on their own
hardware with no external egress.

Pipeline (mirrors the community-standard ``INGEST -> SEARCH -> EVALUATE`` flow):

* :func:`load_dataset` parses the official JSON into a canonical schema.
* A :class:`MemoryBackend` ingests conversation history and answers queries.
* :func:`run_benchmark` drives the backend and scores every question.
* :func:`evaluate_predictions` scores an existing hypothesis file (no backend).

Two scoring families are reported:

* **Retrieval** (exact): evidence recall@k, MRR, and latency percentiles.
* **Answer** (deterministic proxy): SQuAD-style token F1, exact match, and
  containment. The official benchmarks judge answers with an LLM; those scores
  are not reproducible offline, so this harness reports the deterministic proxy
  and leaves LLM judging to an optional, explicitly-configured hook.

Provenance (dataset, revision, backend, git SHA, cutoffs, seed, timestamp) is
stamped into every report so results are auditable and reproducible.

Usage::

    # Evaluate an existing hypothesis file (fully offline, no backend).
    python -m foresight.benchmarks evaluate \\
        --dataset longmemeval --data longmemeval_s_cleaned.json \\
        --hypotheses predictions.jsonl

    # Run ingest + retrieval against a self-hosted Foresight backend.
    python -m foresight.benchmarks run \\
        --dataset longmemeval --data longmemeval_s_cleaned.json --top-k 200
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_DATASETS = ("longmemeval", "locomo")
DEFAULT_CUTOFFS = (10, 20, 50, 200)
EVIDENCE_OVERLAP_F1 = 0.5
_ABSTENTION_MARKERS = (
    "don't know",
    "do not know",
    "cannot answer",
    "can't answer",
    "i don't know",
    "no answer",
    "not enough information",
    "unanswerable",
    "i'm not sure",
    "i am not sure",
)

_PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


# ---------------------------------------------------------------------------
# Canonical schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    """A single utterance in a session."""

    role: str
    text: str


@dataclass(frozen=True)
class Session:
    """A timestamped session made of ordered turns."""

    session_id: str
    timestamp: str | None
    turns: tuple[Turn, ...]


@dataclass(frozen=True)
class Sample:
    """One question + its answer(s) + the conversation history that precedes it.

    ``history_id`` groups samples that share a conversation history so a backend
    can ingest that history once and reuse it for every question in it.
    """

    sample_id: str
    history_id: str
    sessions: tuple[Session, ...]
    question_id: str
    question: str
    answers: tuple[str, ...]
    category: str
    is_abstention: bool
    evidence_texts: tuple[str, ...]


@dataclass(frozen=True)
class Dataset:
    name: str
    revision: str
    samples: tuple[Sample, ...]

    @property
    def history_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for s in self.samples:
            if s.history_id not in seen:
                seen.append(s.history_id)
        return tuple(seen)


# ---------------------------------------------------------------------------
# Text normalisation and deterministic answer metrics
# ---------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for ch in text:
        out.append(ch.lower() if ch not in _PUNCT else " ")
    return "".join(out).split()


def token_f1(prediction: str, gold: str) -> float:
    """SQuAD-style token F1 between a prediction and a single gold answer."""
    pt = _tokens(prediction)
    gt = _tokens(gold)
    if not gt:
        return 1.0 if not pt else 0.0
    if not pt:
        return 0.0
    pc = Counter(pt)
    gc = Counter(gt)
    common = sum((pc & gc).values())
    precision = common / len(pt)
    recall = common / len(gt)
    denom = precision + recall
    return (2 * precision * recall / denom) if denom else 0.0


def exact_match(prediction: str, gold: str) -> float:
    return 1.0 if _tokens(prediction) == _tokens(gold) else 0.0


def containment(prediction: str, gold: str) -> float:
    """1.0 when every gold token appears in the prediction (order-free)."""
    gt = _tokens(gold)
    if not gt:
        return 1.0
    pt = _tokens(prediction)
    if not pt:
        return 0.0
    missing = sum((Counter(gt) - Counter(pt)).values())
    return 1.0 - (missing / len(gt))


def score_answer(prediction: str, golds: Sequence[str]) -> dict[str, float]:
    """Best score across a sample's reference answers."""
    best_em = 0.0
    best_f1 = 0.0
    best_cont = 0.0
    for gold in golds:
        best_em = max(best_em, exact_match(prediction, gold))
        best_f1 = max(best_f1, token_f1(prediction, gold))
        best_cont = max(best_cont, containment(prediction, gold))
    return {"em": best_em, "f1": best_f1, "containment": best_cont}


def is_refusal(prediction: str) -> bool:
    tokens = _tokens(prediction)
    if not tokens:
        return True
    norm = " ".join(tokens)
    return any(" ".join(_tokens(marker)) in norm for marker in _ABSTENTION_MARKERS)


def _evidence_hit(retrieved: Sequence[str], evidence: Sequence[str]) -> bool:
    """True when any retrieved memory token-overlaps any evidence utterance."""
    if not evidence:
        return False
    for mem in retrieved:
        for ev in evidence:
            if token_f1(mem, ev) >= EVIDENCE_OVERLAP_F1:
                return True
    return False


# ---------------------------------------------------------------------------
# Dataset loaders
# ---------------------------------------------------------------------------


class DatasetError(ValueError):
    """Raised when a dataset file is missing or malformed."""


def _require_file(path: str | os.PathLike[str]) -> Path:
    p = Path(path)
    if not p.is_file():
        raise DatasetError(
            f"dataset file not found: {p}. Download it first (see the LoCoMo / "
            "LongMemEval repositories) or point --data at a local copy."
        )
    return p


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DatasetError(f"could not parse {path} as JSON: {exc}") from exc


def load_longmemeval(path: str | os.PathLike[str], revision: str = "") -> Dataset:
    """Parse a LongMemEval JSON file into the canonical schema.

    Accepts either a JSON array of instances or an object mapping question IDs
    to instances. Each instance uses the official keys: ``question_id``,
    ``question_type``, ``question``, ``answer``, ``question_date``,
    ``haystack_session_ids``, ``haystack_dates``, ``haystack_sessions``,
    and ``answer_session_ids``. Abstention instances are identified by a
    ``question_id`` ending in ``_abs``.
    """
    raw = _load_json(_require_file(path))
    if isinstance(raw, dict):
        items = list(raw.values())
    elif isinstance(raw, list):
        items = raw
    else:
        raise DatasetError("LongMemEval file must be a JSON array or object")

    samples: list[Sample] = []
    for inst in items:
        if not isinstance(inst, dict):
            continue
        qid = str(inst.get("question_id", ""))
        history_id = f"longmemeval-{qid}"
        sessions = _parse_longmemeval_sessions(inst)
        evidence = _longmemeval_evidence(inst)
        answers = _answers(inst.get("answer"))
        samples.append(
            Sample(
                sample_id=f"longmemeval-{qid}",
                history_id=history_id,
                sessions=sessions,
                question_id=qid,
                question=str(inst.get("question", "")),
                answers=answers,
                category=str(inst.get("question_type", "unknown")),
                is_abstention=qid.endswith("_abs"),
                evidence_texts=evidence,
            )
        )
    if not samples:
        raise DatasetError("LongMemEval file contained no parseable instances")
    return Dataset(name="longmemeval", revision=revision, samples=tuple(samples))


def _parse_longmemeval_sessions(inst: dict[str, Any]) -> tuple[Session, ...]:
    sessions: list[Session] = []
    session_ids = inst.get("haystack_session_ids") or []
    dates = inst.get("haystack_dates") or []
    history = inst.get("haystack_sessions") or []
    for idx, turns_raw in enumerate(history):
        sid = str(session_ids[idx]) if idx < len(session_ids) else f"session_{idx}"
        ts = str(dates[idx]) if idx < len(dates) else None
        turns: list[Turn] = []
        for t in turns_raw if isinstance(turns_raw, list) else []:
            if not isinstance(t, dict):
                continue
            turns.append(Turn(role=str(t.get("role", "")), text=str(t.get("content", ""))))
        sessions.append(Session(session_id=sid, timestamp=ts, turns=tuple(turns)))
    return tuple(sessions)


def _longmemeval_evidence(inst: dict[str, Any]) -> tuple[str, ...]:
    evidence: list[str] = []
    answer_ids = {str(x) for x in (inst.get("answer_session_ids") or [])}
    session_ids = inst.get("haystack_session_ids") or []
    history = inst.get("haystack_sessions") or []
    for idx, turns_raw in enumerate(history):
        sid = str(session_ids[idx]) if idx < len(session_ids) else ""
        if answer_ids and sid not in answer_ids:
            continue
        for t in turns_raw if isinstance(turns_raw, list) else []:
            if isinstance(t, dict) and t.get("has_answer"):
                evidence.append(str(t.get("content", "")))
    return tuple(evidence)


def load_locomo(path: str | os.PathLike[str], revision: str = "") -> Dataset:
    """Parse a LoCoMo ``locomo10.json`` file into the canonical schema.

    The file is a JSON array of conversations. Each conversation has a
    ``conversation`` mapping (session name -> list of ``{speaker, text, dia_id}``
    utterances) and a ``qa`` list (``{question, answer, evidence, category}``).
    Categories: 1=multi-hop, 2=temporal, 3=open-domain, 4=single-hop,
    5=adversarial.
    """
    raw = _load_json(_require_file(path))
    if not isinstance(raw, list):
        raise DatasetError("LoCoMo file must be a JSON array of conversations")

    samples: list[Sample] = []
    for conv_idx, conv in enumerate(raw):
        if not isinstance(conv, dict):
            continue
        sessions, dia_to_text = _parse_locomo_conversation(conv)
        history_id = f"locomo-{conv_idx}"
        for qa in conv.get("qa") or []:
            if not isinstance(qa, dict):
                continue
            category = _locomo_category(qa.get("category"))
            evidence_refs = qa.get("evidence") or []
            evidence = tuple(dia_to_text[str(d)] for d in evidence_refs if str(d) in dia_to_text)
            qid = str(qa.get("question", ""))[:24]
            samples.append(
                Sample(
                    sample_id=f"locomo-{conv_idx}-{len(samples)}",
                    history_id=history_id,
                    sessions=sessions,
                    question_id=qid,
                    question=str(qa.get("question", "")),
                    answers=_answers(qa.get("answer")),
                    category=category,
                    is_abstention=False,
                    evidence_texts=evidence,
                )
            )
    if not samples:
        raise DatasetError("LoCoMo file contained no parseable conversations")
    return Dataset(name="locomo", revision=revision, samples=tuple(samples))


def _parse_locomo_conversation(
    conv: dict[str, Any],
) -> tuple[tuple[Session, ...], dict[str, str]]:
    conversation = conv.get("conversation") or {}
    sessions: list[Session] = []
    dia_to_text: dict[str, str] = {}
    if isinstance(conversation, dict):
        for name in sorted(conversation):
            sessions.append(_locomo_session(name, conversation[name], dia_to_text))
    elif isinstance(conversation, list):
        for idx, raw_session in enumerate(conversation):
            sessions.append(_locomo_session(f"session_{idx}", raw_session, dia_to_text))
    return tuple(sessions), dia_to_text


def _locomo_session(name: str, raw_turns: Any, dia_to_text: dict[str, str]) -> Session:
    turns: list[Turn] = []
    for t in raw_turns if isinstance(raw_turns, list) else []:
        if not isinstance(t, dict):
            continue
        text = str(t.get("text", ""))
        dia_id = t.get("dia_id")
        if dia_id is not None:
            dia_to_text[str(dia_id)] = text
        speaker = str(t.get("speaker", ""))
        role = f"speaker-{speaker}" if speaker else ""
        turns.append(Turn(role=role, text=text))
    return Session(session_id=name, timestamp=None, turns=tuple(turns))


def _locomo_category(value: Any) -> str:
    try:
        num = int(value)
    except TypeError, ValueError:
        return str(value or "unknown")
    return {
        1: "multi-hop",
        2: "temporal",
        3: "open-domain",
        4: "single-hop",
        5: "adversarial",
    }.get(num, f"category-{num}")


def _answers(value: Any) -> tuple[str, ...]:
    if value is None:
        return ("",)
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    return (str(value),)


def load_dataset(name: str, path: str | os.PathLike[str], revision: str = "") -> Dataset:
    """Load a supported dataset by name (``longmemeval`` or ``locomo``)."""
    key = name.lower()
    if key == "longmemeval":
        return load_longmemeval(path, revision=revision)
    if key == "locomo":
        return load_locomo(path, revision=revision)
    raise DatasetError(f"unknown dataset {name!r}; supported: {SUPPORTED_DATASETS}")


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievedMemory:
    memory_id: str
    content: str
    score: float


@dataclass
class IngestStats:
    memories: int = 0
    sessions: int = 0


class MemoryBackend(Protocol):
    """The seam every benchmark backend implements."""

    name: str

    def reset(self) -> None: ...

    def ingest(self, history_id: str, sessions: Sequence[Session], user_id: str, tenant_id: str) -> IngestStats: ...

    def retrieve(self, query: str, k: int, user_id: str, tenant_id: str) -> list[RetrievedMemory]: ...


class ForesightBackend:
    """Self-hosted backend that stores memories and queries the hybrid retriever.

    Uses an isolated SQLite database and mirrors the isolation pattern from
    :class:`foresight.eval_harness.EvalHarness` so runs never touch a real
    deployment.
    """

    name = "foresight"

    def __init__(
        self,
        db_path: str | None = None,
        user_id: str = "_bench_user_",
        tenant_id: str = "_bench_tenant_",
    ) -> None:
        self._db_path = db_path
        self.user_id = user_id
        self.tenant_id = tenant_id
        self._patches: list[tuple[Any, str, Any]] = []
        self._conn: Any = None

    def reset(self) -> None:
        self._apply_isolation()

    def _apply_isolation(self) -> None:
        import foresight.config as config_module
        import foresight.connection_pool as conn_pool_module
        from foresight.connection_pool import get_pool, reset_pool
        from foresight.hybrid_retriever import reset_hybrid_retriever
        from foresight.server import _SCHEMA_MIGRATIONS
        from foresight.tenant_context import set_current_account_id, set_current_user_id

        reset_pool()
        reset_hybrid_retriever()

        if self._db_path is None:
            import tempfile

            fd, resolved = tempfile.mkstemp(suffix=".db", prefix="foresight_bench_")
            os.close(fd)
            self._db_path = resolved

        for mod in (config_module, conn_pool_module):
            original = mod.DB_PATH
            mod.DB_PATH = self._db_path
            self._patches.append((mod, "DB_PATH", original))

        set_current_user_id(self.user_id)
        set_current_account_id(self.tenant_id)

        pool = get_pool(self._db_path)
        conn = pool.acquire()
        try:
            for version in sorted(_SCHEMA_MIGRATIONS):
                for stmt in _SCHEMA_MIGRATIONS[version]:
                    with contextlib.suppress(Exception):  # pragma: no cover
                        conn.execute(stmt)
            conn.commit()
        finally:
            pool.release(conn)

    def _connection(self) -> Any:
        from foresight.connection_pool import get_pool

        pool = get_pool(self._db_path)
        return pool.acquire()

    def ingest(self, history_id: str, sessions: Sequence[Session], user_id: str, tenant_id: str) -> IngestStats:
        self._apply_isolation()
        import hashlib

        conn = self._connection()
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        try:
            for session in sessions:
                for turn in session.turns:
                    text = f"{turn.role}: {turn.text}" if turn.role else turn.text
                    if not text.strip():
                        continue
                    memory_id = hashlib.sha256(
                        f"{history_id}:{session.session_id}:{count}:{text}".encode()
                    ).hexdigest()[:16]
                    content_hash = hashlib.sha256(text.encode()).hexdigest()
                    conn.execute(
                        """INSERT OR IGNORE INTO memories
                        (id, content, content_hash, tenant_id, user_id, scope,
                         retention, category, bank_id, created_at, updated_at, tags,
                         emotional_context, metrics, is_ghost, synthesized_from,
                         version, importance, current_strength, activation_count,
                         strength_trend)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            memory_id,
                            text,
                            content_hash,
                            tenant_id,
                            user_id,
                            "session",
                            "short_term",
                            "fact",
                            "default",
                            now,
                            now,
                            None,
                            None,
                            None,
                            0,
                            None,
                            1,
                            0.5,
                            0.5,
                            0,
                            0.0,
                        ),
                    )
                    count += 1
            conn.commit()
        finally:
            conn.close()
        return IngestStats(memories=count, sessions=len(list(sessions)))

    def retrieve(self, query: str, k: int, user_id: str, tenant_id: str) -> list[RetrievedMemory]:
        from foresight.hybrid_retriever import HybridSearchOptions, get_hybrid_retriever

        retriever = get_hybrid_retriever()
        result = retriever.search(
            query=query,
            user_id=user_id,
            options=HybridSearchOptions(
                tenant_id=tenant_id,
                limit=max(k * 2, 20),
                min_importance=0.0,
            ),
        )
        out: list[RetrievedMemory] = []
        for mem in result.results:
            data = mem.to_dict()
            out.append(
                RetrievedMemory(
                    memory_id=str(data.get("memory_id", "")),
                    content=str(data.get("content", "")),
                    score=float(data.get("combined_score", 0.0)),
                )
            )
        return out[:k]

    def close(self) -> None:
        from foresight.connection_pool import reset_pool
        from foresight.hybrid_retriever import reset_hybrid_retriever
        from foresight.tenant_context import reset_tenant_context

        reset_hybrid_retriever()
        reset_pool()
        for module, attr, original in self._patches:
            setattr(module, attr, original)
        self._patches.clear()
        reset_tenant_context()


class NoopBackend:
    """Backend that stores nothing and returns nothing; used by evaluate-only runs."""

    name = "noop"

    def reset(self) -> None:  # pragma: no cover - trivial
        return None

    def ingest(self, *_args: Any, **_kwargs: Any) -> IngestStats:  # pragma: no cover
        return IngestStats()

    def retrieve(self, *_args: Any, **_kwargs: Any) -> list[RetrievedMemory]:  # pragma: no cover
        return []


# ---------------------------------------------------------------------------
# Results and report
# ---------------------------------------------------------------------------


@dataclass
class SampleResult:
    sample_id: str
    question_id: str
    category: str
    is_abstention: bool
    prediction: str
    em: float | None
    f1: float | None
    containment: float | None
    evidence_recall: dict[str, float]
    mrr: float | None
    latency_ms: float | None


@dataclass
class CategoryStats:
    category: str
    n: int
    em: float | None
    f1: float | None
    containment: float | None
    abstention_accuracy: float | None
    evidence_recall: dict[str, float]


@dataclass
class BenchmarkReport:
    dataset: str
    revision: str
    backend: str
    top_k: int
    cutoffs: tuple[int, ...]
    seed: int
    git_sha: str
    embedding_provider: str
    timestamp: str
    n_samples: int
    n_abstention: int
    overall: dict[str, Any]
    per_category: list[CategoryStats]
    latency: dict[str, float]
    results: list[SampleResult]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def save(self, path: str | os.PathLike[str]) -> None:
        Path(path).write_text(self.to_json() + "\n", encoding="utf-8")

    def format_markdown(self) -> str:
        lines: list[str] = [
            f"# {self.dataset} benchmark ({self.revision or 'unpinned'})",
            "",
            f"- backend: `{self.backend}`",
            f"- top-k: {self.top_k} (cutoffs {', '.join(map(str, self.cutoffs))})",
            f"- samples: {self.n_samples} (abstention: {self.n_abstention})",
            f"- embedding provider: `{self.embedding_provider}`",
            f"- git: `{self.git_sha}`",
            f"- timestamp: {self.timestamp}",
            "",
            "## Overall",
            "",
            "| metric | value |",
            "|---|---|",
        ]
        for key, value in self.overall.items():
            lines.append(f"| {key} | {_fmt(value)} |")
        lines += [
            "",
            "## Per category",
            "",
            "| category | n | EM | F1 | containment | recall@10 |",
            "|---|---|---|---|---|---|",
        ]
        for cat in self.per_category:
            lines.append(
                f"| {cat.category} | {cat.n} | {_fmt(cat.em)} | {_fmt(cat.f1)} | "
                f"{_fmt(cat.containment)} | {_fmt(cat.evidence_recall.get('10'))} |"
            )
        lines += ["", "## Latency (ms)", "", "| p50 | p95 | p99 |", "|---|---|---|"]
        lines.append(
            f"| {_fmt(self.latency.get('p50'))} | {_fmt(self.latency.get('p95'))} | {_fmt(self.latency.get('p99'))} |"
        )
        return "\n".join(lines) + "\n"


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _pct(mean: float | None) -> float | None:
    return (mean * 100.0) if mean is not None else None


def _mean(values: Sequence[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _percentile(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    f = int(k)
    c = f + 1 if f + 1 < len(ordered) else f
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

AnswerFn = Callable[[list[RetrievedMemory], str], str]


def run_benchmark(
    dataset: Dataset,
    backend: MemoryBackend,
    *,
    user_id: str = "_bench_user_",
    tenant_id: str = "_bench_tenant_",
    cutoffs: Sequence[int] = DEFAULT_CUTOFFS,
    seed: int = 0,
    answer_fn: AnswerFn | None = None,
) -> BenchmarkReport:
    """Run ingest -> search -> score against a backend and return a report."""
    backend.reset()
    retrieve_limit = max(cutoffs) if cutoffs else 1
    ingested: set[str] = set()
    results: list[SampleResult] = []
    latencies: list[float] = []

    for sample in dataset.samples:
        if sample.history_id not in ingested:
            backend.ingest(sample.history_id, sample.sessions, user_id, tenant_id)
            ingested.add(sample.history_id)

        t0 = time.perf_counter()
        retrieved = backend.retrieve(sample.question, retrieve_limit, user_id, tenant_id)
        latencies.append((time.perf_counter() - t0) * 1000)

        prediction = answer_fn(retrieved, sample.question) if answer_fn else ""
        answer_scores = score_answer(prediction, sample.answers) if answer_fn else None
        em = answer_scores["em"] if answer_scores else None
        f1 = answer_scores["f1"] if answer_scores else None
        cont = answer_scores["containment"] if answer_scores else None

        recall: dict[str, float] = {}
        for cutoff in cutoffs:
            top = [m.content for m in retrieved[:cutoff]]
            recall[str(cutoff)] = 1.0 if _evidence_hit(top, sample.evidence_texts) else 0.0

        results.append(
            SampleResult(
                sample_id=sample.sample_id,
                question_id=sample.question_id,
                category=sample.category,
                is_abstention=sample.is_abstention,
                prediction=prediction,
                em=em,
                f1=f1,
                containment=cont,
                evidence_recall=recall,
                mrr=None,
                latency_ms=latencies[-1],
            )
        )

    return _build_report(
        dataset=dataset,
        backend=backend,
        results=results,
        latencies=latencies,
        cutoffs=tuple(cutoffs),
        seed=seed,
        has_answers=answer_fn is not None,
    )


def evaluate_predictions(
    dataset: Dataset,
    hypotheses: dict[str, str],
    *,
    backend_name: str = "hypotheses",
    cutoffs: Sequence[int] = DEFAULT_CUTOFFS,
    seed: int = 0,
) -> BenchmarkReport:
    """Score an existing hypothesis file against a dataset (no backend needed)."""
    results: list[SampleResult] = []
    for sample in dataset.samples:
        prediction = hypotheses.get(sample.question_id, "")
        answer_scores = score_answer(prediction, sample.answers)
        results.append(
            SampleResult(
                sample_id=sample.sample_id,
                question_id=sample.question_id,
                category=sample.category,
                is_abstention=sample.is_abstention,
                prediction=prediction,
                em=answer_scores["em"],
                f1=answer_scores["f1"],
                containment=answer_scores["containment"],
                evidence_recall={},
                mrr=None,
                latency_ms=None,
            )
        )
    return _build_report(
        dataset=dataset,
        backend=NoopBackend(),
        results=results,
        latencies=[],
        cutoffs=tuple(cutoffs),
        seed=seed,
        has_answers=True,
        backend_name=backend_name,
    )


def _build_report(
    *,
    dataset: Dataset,
    backend: MemoryBackend,
    results: list[SampleResult],
    latencies: list[float],
    cutoffs: tuple[int, ...],
    seed: int,
    has_answers: bool,
    backend_name: str | None = None,
) -> BenchmarkReport:
    non_abstention = [r for r in results if not r.is_abstention]
    abstention = [r for r in results if r.is_abstention]

    overall: dict[str, Any] = {
        "n": len(results),
        "n_abstention": len(abstention),
    }
    if has_answers:
        overall["em"] = _mean([r.em for r in non_abstention if r.em is not None])
        overall["f1"] = _mean([r.f1 for r in non_abstention if r.f1 is not None])
        overall["containment"] = _mean([r.containment for r in non_abstention if r.containment is not None])
    if abstention and has_answers:
        overall["abstention_accuracy"] = _mean([1.0 if is_refusal(r.prediction) else 0.0 for r in abstention])
    for cutoff in cutoffs:
        scores = [r.evidence_recall.get(str(cutoff), 0.0) for r in non_abstention]
        if scores:
            overall[f"evidence_recall@{cutoff}"] = _mean(scores)

    per_category: list[CategoryStats] = []
    by_category: dict[str, list[SampleResult]] = {}
    for r in non_abstention:
        by_category.setdefault(r.category, []).append(r)
    for category in sorted(by_category):
        rows = by_category[category]
        cat_recall: dict[str, float] = {}
        for cutoff in cutoffs:
            scores = [r.evidence_recall.get(str(cutoff), 0.0) for r in rows]
            if scores:
                cat_recall[str(cutoff)] = _mean(scores) or 0.0
        per_category.append(
            CategoryStats(
                category=category,
                n=len(rows),
                em=_mean([r.em for r in rows if r.em is not None]),
                f1=_mean([r.f1 for r in rows if r.f1 is not None]),
                containment=_mean([r.containment for r in rows if r.containment is not None]),
                abstention_accuracy=None,
                evidence_recall=cat_recall,
            )
        )

    return BenchmarkReport(
        dataset=dataset.name,
        revision=dataset.revision,
        backend=backend_name or backend.name,
        top_k=max(cutoffs) if cutoffs else 0,
        cutoffs=cutoffs,
        seed=seed,
        git_sha=_git_sha(),
        embedding_provider=os.environ.get("FORESIGHT_EMBEDDING_PROVIDER", "auto"),
        timestamp=datetime.now(timezone.utc).isoformat(),
        n_samples=len(results),
        n_abstention=len(abstention),
        overall=overall,
        per_category=per_category,
        latency={
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
        },
        results=results,
    )


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:  # pragma: no cover - environment dependent
        return "unknown"


# ---------------------------------------------------------------------------
# Hypothesis file I/O (LongMemEval-compatible JSONL)
# ---------------------------------------------------------------------------


def read_hypotheses(path: str | os.PathLike[str]) -> dict[str, str]:
    """Read a hypothesis file into a question_id -> prediction map.

    Accepts LongMemEval-style JSONL records (``{"question_id", "hypothesis"}``),
    a JSON array of those records, or a flat JSON object mapping question ids to
    answer strings.
    """
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return {}

    whole: Any
    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        whole = None

    if isinstance(whole, list):
        records = [r for r in whole if isinstance(r, dict)]
    elif isinstance(whole, dict):
        if any(k in whole for k in ("question_id", "hypothesis", "prediction")):
            records = [whole]
        else:
            return {str(k): str(v) for k, v in whole.items()}
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]

    result: dict[str, str] = {}
    for obj in records:
        qid = str(obj.get("question_id", ""))
        pred = str(obj.get("hypothesis", obj.get("prediction", "")))
        result[qid] = pred
    return result


def write_hypotheses(predictions: dict[str, str], path: str | os.PathLike[str]) -> None:
    """Write predictions as LongMemEval-style JSONL (``{question_id, hypothesis}``)."""
    with Path(path).open("w", encoding="utf-8") as fh:
        for qid, pred in predictions.items():
            fh.write(json.dumps({"question_id": qid, "hypothesis": pred}) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m foresight.benchmarks",
        description="Reproducible self-hosted memory benchmarks (LoCoMo, LongMemEval).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run ingest + retrieval against a backend")
    run.add_argument("--dataset", required=True, choices=SUPPORTED_DATASETS)
    run.add_argument("--data", required=True, help="path to the dataset JSON file")
    run.add_argument("--revision", default="", help="pinned dataset revision for provenance")
    run.add_argument("--top-k", type=int, default=200, help="retrieval limit (max cutoff)")
    run.add_argument("--cutoffs", default=",".join(map(str, DEFAULT_CUTOFFS)))
    run.add_argument("--db", default=None, help="SQLite path; temp file if omitted")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--report", default=None, help="path to write the JSON report")
    run.add_argument("--predictions-out", default=None, help="write retrieval-free predictions JSONL")

    ev = sub.add_parser("evaluate", help="score an existing hypothesis file")
    ev.add_argument("--dataset", required=True, choices=SUPPORTED_DATASETS)
    ev.add_argument("--data", required=True, help="path to the dataset JSON file")
    ev.add_argument("--revision", default="", help="pinned dataset revision for provenance")
    ev.add_argument("--hypotheses", required=True, help="JSONL hypothesis file")
    ev.add_argument("--seed", type=int, default=0)
    ev.add_argument("--report", default=None, help="path to write the JSON report")

    sub.add_parser("list", help="list supported datasets")
    return parser


def _parse_cutoffs(raw: str) -> tuple[int, ...]:
    return tuple(int(x.strip()) for x in raw.split(",") if x.strip())


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "list":
        sys.stdout.write("Supported datasets:\n")
        for name in SUPPORTED_DATASETS:
            sys.stdout.write(f"  - {name}\n")
        return 0

    dataset = load_dataset(args.dataset, args.data, revision=args.revision)

    if args.command == "evaluate":
        hypotheses = read_hypotheses(args.hypotheses)
        report = evaluate_predictions(dataset, hypotheses, seed=args.seed)
    else:
        cutoffs = _parse_cutoffs(args.cutoffs)
        backend = ForesightBackend(db_path=args.db)
        try:
            report = run_benchmark(
                dataset,
                backend,
                cutoffs=cutoffs,
                seed=args.seed,
            )
        finally:
            backend.close()
        if args.predictions_out:
            write_hypotheses(
                {r.question_id: r.prediction for r in report.results},
                args.predictions_out,
            )

    sys.stdout.write(report.format_markdown())
    if args.report:
        report.save(args.report)
        sys.stdout.write(f"Report written to {args.report}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
