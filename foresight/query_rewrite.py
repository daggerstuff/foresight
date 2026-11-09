"""Deterministic question-to-statement query rewriting for retrieval.

Supermemory ships ``rewriteQuery`` (LLM merges multiple rewrites at query
time) and mem0 relies on neural embeddings to bridge the question-vs-memory
vocabulary gap. Foresight's form is deliberately cheaper and local:

- variant 0 is always the original query (exact matches never lose);
- variant 1 strips the question framing ("What is ...?", "Do you remember
  ...?") into statement form, so lexical signals meet stored declarative
  facts on shared vocabulary;
- variant 2 drops stopwords down to the content-word core, which is what
  the keyword/TF-IDF signals score best on;
- an optional LLM paraphrase (``FORESIGHT_QUERY_REWRITE=llm``) appends one
  more variant. It is strictly additive and silently skipped when the
  provider is unavailable, so search never breaks when the model does.

This is the multi-query RRF argument from RAG literature (several
independent phrasings converging on one document is stronger evidence than
any single phrasing): per-signal rankings from each variant are merged by
best rank, with the number of variants that matched breaking ties.

No network calls, no model downloads, no extra dependencies on the default
path — the no-egress guarantee is preserved.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("foresight_query_rewrite")

# Maximum variants produced for one query (original + statement + core).
# The retriever re-runs the lexical signals per variant, so this bounds the
# extra work at 3x on those signals only.
DEFAULT_MAX_VARIANTS = 3
MAX_VARIANTS_HARD_CAP = 5

# Leading question framings stripped to reach statement form. Ordered
# longest-first so "do you remember" wins over the bare "do".
_QUESTION_OPENERS = (
    r"do you remember",
    r"can you recall",
    r"can you remember",
    r"tell me about",
    r"remind me of",
    r"remind me about",
    r"tell me",
    r"remind me",
    r"what is",
    r"what are",
    r"what was",
    r"what were",
    r"what does",
    r"what did",
    r"when is",
    r"when are",
    r"when was",
    r"when were",
    r"when does",
    r"when did",
    r"where is",
    r"where are",
    r"where was",
    r"how do",
    r"how does",
    r"how did",
    r"how is",
    r"how are",
    r"why is",
    r"why are",
    r"who is",
    r"who are",
    r"who was",
    r"is there",
    r"are there",
    r"do i have",
    r"do you know",
    r"what",
    r"when",
    r"where",
    r"why",
    r"who",
    r"how",
    r"is",
    r"are",
    r"was",
    r"were",
    r"do",
    r"does",
    r"did",
)

_OPENER_RE = re.compile(r"^\s*(?:" + r"|".join(_QUESTION_OPENERS) + r")\b[\s,]*", re.IGNORECASE)

# Tokens that carry no retrieval signal once the framing is stripped.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "shall",
        "may",
        "might",
        "must",
        "i",
        "me",
        "my",
        "you",
        "your",
        "yours",
        "we",
        "our",
        "they",
        "their",
        "he",
        "she",
        "it",
        "his",
        "her",
        "its",
        "them",
        "us",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "about",
        "and",
        "or",
        "what",
        "when",
        "where",
        "why",
        "who",
        "how",
        "there",
        "here",
        "please",
        "tell",
        "remember",
        "recall",
        "remind",
        "know",
    }
)


def query_rewrite_enabled() -> bool:
    """True unless ``FORESIGHT_QUERY_REWRITE`` explicitly disables rewriting.

    Unset (or ``auto``/``1``/``true``/``on``/``yes``/``llm``) means the
    deterministic zero-cost stage runs. ``off``/``0``/``false``/``no``
    turns it off. Read at call time so tests and deployments can toggle
    without a process restart.
    """
    raw = os.environ.get("FORESIGHT_QUERY_REWRITE", "").strip().lower()
    return raw not in {"off", "0", "false", "no", "disable", "disabled"}


def llm_rewrite_enabled() -> bool:
    """True only when ``FORESIGHT_QUERY_REWRITE=llm`` opts into an LLM paraphrase."""
    return os.environ.get("FORESIGHT_QUERY_REWRITE", "").strip().lower() == "llm"


def _statement_form(query: str) -> str:
    """Strip question framing ("What is X?") into statement form ("X")."""
    text = query.strip()
    if text.endswith("?"):
        text = text[:-1].strip()
    return _OPENER_RE.sub("", text).strip()


def _content_core(text: str) -> str:
    """Reduce statement form to content words ("my dentist appointment" -> "dentist appointment")."""
    tokens = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS]
    return " ".join(tokens)


def _llm_paraphrase(query: str, user_id: str, tenant_id: str) -> str | None:
    """One LLM paraphrase of the query, or None when unavailable.

    Never raises: the paraphrase is a bonus variant, and search must keep
    working unchanged when the provider is missing or failing.
    """
    try:
        from .llm_client import default_llm_call
    except Exception as exc:
        logger.debug("LLM paraphrase unavailable (import): %s", exc)
        return None
    try:
        prompt = (
            "Rewrite this memory-search query as a short declarative statement "
            "with the same meaning. Reply with only the rewritten statement, "
            f"no quotes or explanation. Query: {query}"
        )
        paraphrase = default_llm_call(prompt, tenant_id, user_id)
    except Exception as exc:
        logger.warning("LLM paraphrase failed, continuing without it: %s", exc)
        return None
    cleaned = " ".join(str(paraphrase).strip().strip("\"'").split())
    if not cleaned or cleaned.lower() == query.strip().lower():
        return None
    return cleaned


def rewrite_variants(
    query: str,
    max_variants: int = DEFAULT_MAX_VARIANTS,
    llm_assist: bool = False,
    user_id: str = "",
    tenant_id: str = "default",
) -> list[str]:
    """Return up to ``max_variants`` query phrasings, original first.

    Deterministic and dependency-free unless ``llm_assist`` is set (in
    which case one best-effort LLM paraphrase is appended last and silently
    skipped on failure). Never returns an empty list.
    """
    cap = max(1, min(int(max_variants or 1), MAX_VARIANTS_HARD_CAP))
    variants = [query]
    statement = _statement_form(query)
    if statement and statement != query and statement.lower() != query.strip().lower():
        variants.append(statement)
    core = _content_core(statement)
    if core and all(core.lower() != v.strip().lower() for v in variants):
        variants.append(core)
    if llm_assist and len(variants) < cap:
        paraphrase = _llm_paraphrase(query, user_id, tenant_id)
        if paraphrase and all(paraphrase.lower() != v.strip().lower() for v in variants):
            variants.append(paraphrase)
    return variants[:cap]


def merge_variant_rankings(rankings: list[dict[str, int]]) -> dict[str, int]:
    """Merge per-variant ``{memory_id: rank}`` maps into one deterministic ranking.

    A memory's merged position is its best rank across variants; ties break
    by number of variants that matched (more independent phrasings agreeing
    is stronger evidence), then by memory id so the output is stable across
    runs. Returns a fresh ``{memory_id: 1-based rank}`` map.
    """
    best: dict[str, int] = {}
    hits: dict[str, int] = {}
    for ranking in rankings:
        for mid, rank in ranking.items():
            if mid not in best or rank < best[mid]:
                best[mid] = rank
            hits[mid] = hits.get(mid, 0) + 1
    ordered = sorted(best, key=lambda mid: (best[mid], -hits[mid], mid))
    return {mid: rank + 1 for rank, mid in enumerate(ordered)}
