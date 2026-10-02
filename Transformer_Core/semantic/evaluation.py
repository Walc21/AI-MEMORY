"""Offline subsystem metrics; adapters for local LongMemEval/LoCoMo datasets."""

import json
import math
from pathlib import Path
import statistics
import tempfile
import time

from .model import SemanticError, normalized
from .pipeline import Memory

FIXTURE = {
    "episodes": ["Alice works at Acme in 2020.", "Acme lives in Paris.", "Maria nasceu em 1992.",
                 "Maria nasceu em 1993.", "Bob trabalha na Contoso entre 2019 e 2022.",
                 "Dan works at OldCo.", "Dan works at NewCo."],
    "updates": [{"old": "Dan works at OldCo.", "new": "Dan works at NewCo."}],
    "expected_assertions": [["alice", "works_at", "acme"], ["acme", "lives_in", "paris"],
        ["maria", "born_in", "1992"], ["maria", "born_in", "1993"], ["bob", "works_at", "contoso"],
        ["dan", "works_at", "oldco"], ["dan", "works_at", "newco"]],
    "queries": [
        {"question": "Onde Alice trabalha?", "expected": ["Alice works at Acme"], "category": "retrieval"},
        {"question": "Onde fica a empresa de Alice?", "expected": ["Alice works at Acme", "Acme lives in Paris"], "category": "multi_hop"},
        {"question": "Quando Maria nasceu?", "expected": ["1992", "1993"], "conflict": True, "category": "contradiction"},
        {"question": "Onde Bob trabalha?", "at": "2020", "expected": ["Contoso"], "category": "temporal"},
        {"question": "Where Dan works?", "expected": ["NewCo"], "excluded": ["OldCo"], "category": "update"},
        {"question": "Qual é a senha secreta do satélite?", "expected": [], "abstain": True, "category": "abstention"},
    ],
}


def adapt(value):
    if isinstance(value, dict) and set(value) >= {"episodes", "queries"}:
        return value
    samples = value if isinstance(value, list) else [value]
    episodes, queries = [], []
    for sample in samples:
        if "haystack_sessions" in sample:
            for session in sample["haystack_sessions"]:
                episodes.extend(message["content"] for message in session if isinstance(message.get("content"), str))
            queries.append({"question": sample["question"], "expected": [str(sample["answer"])],
                            "history": True, "category": "longmemeval_answer_coverage"})
        elif "conversation" in sample and "qa" in sample:
            for key, session in sample["conversation"].items():
                if isinstance(session, list):
                    episodes.extend(message.get("text", message.get("content", "")) for message in session)
            queries.extend({"question": item["question"], "expected": [str(item["answer"])],
                            "history": True, "category": "locomo_answer_coverage"} for item in sample["qa"])
        else:
            raise SemanticError("Dataset deve ser fixture Mimir, LongMemEval ou LoCoMo JSON.")
    return {"episodes": episodes, "queries": queries}


def evaluate(path=None):
    dataset = adapt(json.loads(Path(path).read_text())) if path else FIXTURE
    if not isinstance(dataset["episodes"], list) or not dataset["episodes"] or not isinstance(dataset["queries"], list) or not dataset["queries"]:
        raise SemanticError("Dataset requer episódios e consultas não vazios.")
    with tempfile.TemporaryDirectory(prefix="mimir-eval-") as temporary:
        memory = Memory(Path(temporary) / "memory")
        for episode in dataset["episodes"]:
            text = episode["text"] if isinstance(episode, dict) else episode
            memory.episode(text)
        for update in dataset.get("updates", []):
            matches = {key: [row["id"] for row in memory.inspect() if any(update[key] == ev["quote"] for ev in row["evidence"])] for key in ("new", "old")}
            if any(len(values) != 1 for values in matches.values()):
                raise SemanticError("Update de avaliação requer quotes de assertions únicas.")
            memory.relate(matches["new"][0], matches["old"][0])
        recalls, reciprocals, ndcgs, latencies = [], [], [], []
        categories = {}
        conflict_expected, conflict_correct = 0, 0
        abstention_expected, abstention_correct = 0, 0
        update_expected, update_correct = 0, 0
        for case in dataset["queries"]:
            started = time.perf_counter()
            result = memory.query(case["question"], valid_at=case.get("at"), as_of=case.get("as_of"), history=case.get("history", False))
            latencies.append(time.perf_counter() - started)
            expected = case.get("expected", [])
            quotes = [normalized(hit["citation"]["quote"]) for hit in result["hits"]]
            found = [any(normalized(value) in quote for quote in quotes) for value in expected]
            recall = sum(found) / len(found) if found else float(result["abstained"])
            recalls.append(recall)
            ranking = [any(normalized(value) in quote for value in expected) for quote in quotes]
            reciprocals.append(next((1 / (index + 1) for index, match in enumerate(ranking) if match), 0) if expected else float(result["abstained"]))
            dcg = sum(int(match) / math.log2(index + 2) for index, match in enumerate(ranking))
            ideal = sum(1 / math.log2(index + 2) for index in range(min(len(expected), len(ranking)))) or 1
            ndcgs.append(min(1, dcg / ideal) if expected else float(result["abstained"]))
            categories.setdefault(case.get("category", "retrieval"), []).append(recall)
            if "conflict" in case:
                conflict_expected += 1
                conflict_correct += bool(result["conflicts"]) == case["conflict"]
            if "abstain" in case:
                abstention_expected += 1
                abstention_correct += result["abstained"] == case["abstain"]
            if "excluded" in case:
                update_expected += 1
                update_correct += all(found) and not any(normalized(value) in quote for value in case["excluded"] for quote in quotes)
        before = memory.query(dataset["queries"][0]["question"]) if dataset["queries"] else {"hits": []}
        canonical = memory.verify()["fingerprint"]
        memory.gc(apply=True)
        after = memory.query(dataset["queries"][0]["question"]) if dataset["queries"] else {"hits": []}
        with memory.store.locked():
            manifest, ledger = memory.store.load()
            from .evidence import resolve
            references = [ev for assertion in ledger.rows("assertions") for ev in assertion["evidence"]]
            resolved = sum(bool(resolve(ledger, ev)) for ev in references)
            extraction = None
            if "expected_assertions" in dataset:
                actual = set()
                for assertion in ledger.rows("assertions"):
                    prop = ledger.get("propositions", assertion["proposition_id"])
                    obj = ledger.get("entities", prop["object"]["value"])["name"] if prop["object"]["kind"] == "entity" else normalized(prop["object"]["value"])
                    actual.add((ledger.get("entities", prop["subject"])["name"], prop["predicate"], obj))
                expected = {tuple(normalized(value) for value in row) for row in dataset["expected_assertions"]}
                correct = len(actual & expected)
                precision = correct / len(actual) if actual else 0
                recall = correct / len(expected) if expected else 0
                extraction = {"precision": precision, "recall": recall, "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0,
                              "method": "annotated subject/predicate/object set equality"}
        return {"schema": "mimir.evaluation.v1", "queries": len(recalls),
                "recall_at_8": statistics.mean(recalls) if recalls else 0,
                "mrr": statistics.mean(reciprocals) if reciprocals else 0,
                "ndcg_at_8": statistics.mean(ndcgs) if ndcgs else 0,
                "categories": {key: statistics.mean(values) for key, values in categories.items()},
                "conflict_accuracy": conflict_correct / conflict_expected if conflict_expected else None,
                "abstention_accuracy": abstention_correct / abstention_expected if abstention_expected else None,
                "update_accuracy": update_correct / update_expected if update_expected else None,
                "extraction": extraction,
                "assertion_evidence_resolution": resolved / len(references) if references else 1,
                "index_rebuild_equivalent": [hit["id"] for hit in before["hits"]] == [hit["id"] for hit in after["hits"]],
                "canonical_unchanged_by_index_rebuild": canonical == memory.verify()["fingerprint"],
                "latency_p50_seconds": statistics.median(latencies) if latencies else 0,
                "latency_p95_seconds": sorted(latencies)[max(0, math.ceil(len(latencies) * .95) - 1)] if latencies else 0,
                "llm_tokens": 0, "method": "substring-evidence relevance; local deterministic extractor",
                "dataset": str(path) if path else "mimir-builtin-v1"}
