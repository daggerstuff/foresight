# Benchmarks

Reproducible, self-hosted memory benchmarks for Foresight's retrieval
pipeline. Results are judge-independent where possible: evidence recall is
computed deterministically against gold evidence spans, and answer metrics
(EM/F1/containment) use exact/normalized string scoring unless an LLM judge
is explicitly configured.

## Methodology

The harness lives in `foresight/benchmarks.py` and implements the
ingest → retrieve → score loop against a `MemoryBackend`:

1. **Ingest** — each dataset history's sessions are written through the
   backend's normal store path.
2. **Retrieve** — each question is searched at a fixed `--top-k` budget
   (default 200; reported cutoffs 10/20/50/200).
3. **Score** — `evidence_recall@k` is 1.0 when any retrieved memory at
   cutoff `k` contains a gold evidence span; EM/F1/containment score the
   prediction against gold answers when an answer function is supplied.
   Latency is measured per query (p50/p95/p99).

Datasets are the public **LongMemEval** and **LoCoMo** benchmarks in their
canonical JSON formats. Foresight ships no dataset weights; download the
upstream files and point `--data` at a local copy.

## Commands

```bash
# List supported datasets
uv run python -m foresight.benchmarks list

# Run LongMemEval
uv run python -m foresight.benchmarks run \
    --dataset longmemeval --data /path/to/longmemeval.json \
    --revision <git-sha-or-tag> --top-k 200 \
    --report benchmarks/longmemeval.json

# Run LoCoMo
uv run python -m foresight.benchmarks run \
    --dataset locomo --data /path/to/locomo10.json \
    --revision <git-sha-or-tag> --report benchmarks/locomo.json

# Score an existing hypothesis file (no backend needed)
uv run python -m foresight.benchmarks evaluate \
    --dataset longmemeval --data /path/to/lme.json \
    --hypotheses hypotheses.jsonl --report benchmarks/eval.json
```

The JSON report records dataset name/revision, backend, git SHA,
timestamp, per-category metrics, evidence recall at each cutoff, and
latency percentiles — everything needed to reproduce or compare runs.

## Results

### LongMemEval (mini smoke subset, `lme-mini-2024`)

Smoke run against the vendored mini fixture (`.agent/lme_mini.json`,
1 sample) to validate the pipeline end-to-end:

| metric | value |
|---|---|
| samples | 1 |
| evidence_recall@10 | 1.000 |
| evidence_recall@20 | 1.000 |
| evidence_recall@50 | 1.000 |
| evidence_recall@200 | 1.000 |
| latency p50 (ms) | 2.3 |

Full-suite numbers require the upstream LongMemEval/LoCoMo files; run the
commands above and commit the generated reports here as they land.
