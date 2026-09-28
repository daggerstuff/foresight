"""Tests for the PIX-4705 self-hosted benchmark harness (foresight.benchmarks)."""

from __future__ import annotations

import json

from foresight.benchmarks import (
    Dataset,
    RetrievedMemory,
    Sample,
    Session,
    Turn,
    _evidence_hit,
    containment,
    evaluate_predictions,
    exact_match,
    is_refusal,
    load_dataset,
    load_locomo,
    load_longmemeval,
    run_benchmark,
    score_answer,
    token_f1,
)


def _mini_longmemeval() -> list[dict]:
    return [
        {
            "question_id": "1",
            "question_type": "single-session-user",
            "question": "Where does Alice live?",
            "answer": "Alice lives in Paris.",
            "question_date": "2024-03-01",
            "haystack_session_ids": ["1"],
            "haystack_dates": ["2024-01-01"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "Hi, what's up?"},
                    {
                        "role": "assistant",
                        "content": "Not much. I moved to Paris last year.",
                        "has_answer": True,
                    },
                ]
            ],
            "answer_session_ids": ["1"],
        },
        {
            "question_id": "2_abs",
            "question_type": "single-session-user",
            "question": "What is my dog's favorite color?",
            "answer": "I don't know.",
            "question_date": "2024-03-02",
            "haystack_session_ids": ["1"],
            "haystack_dates": ["2024-01-01"],
            "haystack_sessions": [[{"role": "user", "content": "hello"}]],
            "answer_session_ids": ["1"],
        },
    ]


def _mini_locomo() -> list[dict]:
    return [
        {
            "conversation": {
                "session_1": [
                    {"speaker": "A", "text": "I started a new job in Berlin.", "dia_id": "d1"},
                    {"speaker": "B", "text": "Congratulations!", "dia_id": "d2"},
                ]
            },
            "qa": [
                {
                    "question": "Where did the person start a new job?",
                    "answer": "Berlin",
                    "evidence": ["d1"],
                    "category": 4,
                }
            ],
        }
    ]


def test_load_longmemeval(tmp_path):
    data = tmp_path / "lme.json"
    data.write_text(json.dumps(_mini_longmemeval()), encoding="utf-8")
    ds = load_longmemeval(data)
    assert ds.name == "longmemeval"
    assert len(ds.samples) == 2
    first = ds.samples[0]
    assert first.question_id == "1"
    assert first.is_abstention is False
    assert first.category == "single-session-user"
    assert first.evidence_texts == ("Not much. I moved to Paris last year.",)
    assert ds.samples[1].is_abstention is True


def test_load_locomo(tmp_path):
    data = tmp_path / "locomo.json"
    data.write_text(json.dumps(_mini_locomo()), encoding="utf-8")
    ds = load_locomo(data)
    assert ds.name == "locomo"
    assert len(ds.samples) == 1
    sample = ds.samples[0]
    assert sample.category == "single-hop"
    assert sample.answers == ("Berlin",)
    assert sample.evidence_texts == ("I started a new job in Berlin.",)


def test_load_dataset_dispatch(tmp_path):
    data = tmp_path / "lme.json"
    data.write_text(json.dumps(_mini_longmemeval()), encoding="utf-8")
    assert load_dataset("longmemeval", data).name == "longmemeval"
    assert load_dataset("LongMemEval", data).name == "longmemeval"


def test_answer_metrics():
    assert exact_match("Paris", "paris") == 1.0
    assert exact_match("Paris France", "paris") == 0.0
    assert token_f1("alice lives in paris", "Alice lives in Paris") == 1.0
    assert containment("alice lives in paris with her cat", "alice lives in paris") == 1.0
    scores = score_answer("alice lives in paris", ("Alice lives in Paris",))
    assert scores["f1"] == 1.0
    assert scores["em"] == 1.0


def test_is_refusal():
    assert is_refusal("") is True
    assert is_refusal("I don't know.") is True
    assert is_refusal("I cannot answer that question.") is True
    assert is_refusal("Alice lives in Paris.") is False


def test_evidence_hit():
    assert _evidence_hit(["I moved to Paris last year"], ["I moved to Paris last year"]) is True
    assert _evidence_hit(["the sky is blue"], ["I moved to Paris last year"]) is False
    assert _evidence_hit([], ["x"]) is False
    assert _evidence_hit(["x"], []) is False


class _FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.memories: list[str] = []

    def reset(self) -> None:
        self.memories = []

    def ingest(self, _history_id, sessions, _user_id, _tenant_id):
        for session in sessions:
            for turn in session.turns:
                text = f"{turn.role}: {turn.text}" if turn.role else turn.text
                self.memories.append(text)

    def retrieve(self, query, k, _user_id, _tenant_id):
        ranked = sorted(self.memories, key=lambda m: token_f1(m, query), reverse=True)
        return [RetrievedMemory(f"m{i}", m, 0.0) for i, m in enumerate(ranked[:k])]


def _sample() -> Sample:
    return Sample(
        sample_id="s1",
        history_id="h1",
        sessions=(
            Session(
                session_id="1",
                timestamp="2024-01-01",
                turns=(Turn(role="assistant", text="I moved to Paris last year."),),
            ),
        ),
        question_id="q1",
        question="Where does Alice live?",
        answers=("Alice lives in Paris.",),
        category="single-session-user",
        is_abstention=False,
        evidence_texts=("I moved to Paris last year.",),
    )


def test_run_benchmark_recall(tmp_path):
    dataset = Dataset(name="longmemeval", revision="", samples=(_sample(),))
    backend = _FakeBackend()
    report = run_benchmark(dataset, backend, cutoffs=(10, 20), seed=0)
    assert report.n_samples == 1
    assert report.overall["evidence_recall@10"] == 1.0
    assert report.overall["evidence_recall@20"] == 1.0
    assert report.per_category[0].category == "single-session-user"
    markdown = report.format_markdown()
    assert "longmemeval" in markdown
    assert "evidence_recall@10" in markdown  # flattened into the overall table


def test_evaluate_predictions(tmp_path):
    dataset = Dataset(
        name="longmemeval",
        revision="",
        samples=(_sample(),),
    )
    report = evaluate_predictions(dataset, {"q1": "Alice lives in Paris."}, seed=0)
    assert report.overall["em"] == 1.0
    assert report.overall["f1"] == 1.0
    assert report.backend == "hypotheses"


def test_foresight_backend_roundtrip(tmp_path):
    from foresight.benchmarks import ForesightBackend

    backend = ForesightBackend(db_path=str(tmp_path / "bench.db"))
    sample = _sample()
    stats = backend.ingest(sample.history_id, sample.sessions, "_u", "_t")
    assert stats.memories == 1
    retrieved = backend.retrieve("where does alice live", 5, "_u", "_t")
    assert isinstance(retrieved, list)
    backend.close()


def test_report_json_roundtrip(tmp_path):
    dataset = Dataset(name="locomo", revision="r1", samples=(_sample(),))
    report = run_benchmark(dataset, _FakeBackend(), cutoffs=(10,), seed=7)
    path = tmp_path / "report.json"
    report.save(path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["dataset"] == "locomo"
    assert loaded["revision"] == "r1"
    assert loaded["seed"] == 7


def test_read_hypotheses_single_record(tmp_path):
    from foresight.benchmarks import read_hypotheses

    path = tmp_path / "single.jsonl"
    path.write_text('{"question_id": "1", "hypothesis": "Alice lives in Paris."}\n')
    assert read_hypotheses(path) == {"1": "Alice lives in Paris."}


def test_read_hypotheses_multiline_and_map(tmp_path):
    from foresight.benchmarks import read_hypotheses

    multi = tmp_path / "multi.jsonl"
    multi.write_text('{"question_id": "1", "hypothesis": "A"}\n{"question_id": "2", "hypothesis": "B"}\n')
    assert read_hypotheses(multi) == {"1": "A", "2": "B"}

    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps({"1": "A", "2": "B"}))
    assert read_hypotheses(flat) == {"1": "A", "2": "B"}


def test_write_hypotheses_roundtrip(tmp_path):
    from foresight.benchmarks import read_hypotheses, write_hypotheses

    path = tmp_path / "out.jsonl"
    write_hypotheses({"1": "A", "2": "B"}, path)
    assert read_hypotheses(path) == {"1": "A", "2": "B"}
