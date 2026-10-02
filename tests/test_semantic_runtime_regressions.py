"""Regressions found by the v0.4.1 semantic runtime audit."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from BN1_1.Pacote.cache import Pacote
from Transformer_Core.semantic.indexes import Index
from Transformer_Core.semantic.model import Ledger, SemanticError
from Transformer_Core.semantic.pipeline import Memory


class SemanticRuntimeRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = Memory(self.root / "memory")

    def ingest(self, text):
        source = self.root / "source.txt"
        source.write_text(text, encoding="utf-8")
        runtime = self.root / "runtime"
        pacote = Pacote(runtime)
        pacote.open()
        pacote.add([source])
        return self.memory.ingest(runtime)

    def test_reader_can_query_but_cannot_save_output(self):
        self.ingest("Ana mora em Recife.\n")
        reader_uid = os.getuid() + 100
        self.memory.policy(readers=[reader_uid])
        destination = self.memory.store.root / "Output_Storage" / "output.json"
        with patch("Transformer_Core.semantic.storage.os.getuid", return_value=reader_uid):
            self.assertTrue(self.memory.query("Ana")['claims'])
            with self.assertRaises(SemanticError):
                self.memory.query("Ana", save=True)
        self.assertFalse(destination.exists())
        self.assertTrue(self.memory.query("Ana", save=True)["claims"])
        self.assertTrue(destination.exists())

    def test_non_object_index_manifest_rebuilds_without_touching_canon(self):
        self.ingest("Ana mora em Recife.\n")
        original = self.memory.query("Ana")
        canonical = self.memory.store.manifest.read_bytes()
        manifest = next(self.memory.store.indexes.rglob("manifest.json"))
        for malformed in ([], None, "not a manifest", 7):
            with self.subTest(malformed=malformed):
                manifest.write_text(json.dumps(malformed), encoding="utf-8")
                result = self.memory.query("Ana")
                self.assertEqual(result["hits"], original["hits"])
                self.assertEqual(self.memory.store.manifest.read_bytes(), canonical)

    def test_lexical_ranking_filters_transaction_time_before_limit(self):
        # A later, shorter passage wins BM25 globally. It must not consume a
        # historical query's only candidate slot before being filtered out.
        rows = [
            {"id": "past", "text": "meteorologia " + "arquivo " * 100,
             "evidence": {}, "transaction_time": "2020-01-01T00:00:00.000000+00:00",
             "assertion_ids": [], "content_id": "past"},
            {"id": "future", "text": "meteorologia",
             "evidence": {}, "transaction_time": "2030-01-01T00:00:00.000000+00:00",
             "assertion_ids": [], "content_id": "future"},
        ]
        with self.memory.store.locked(), patch("Transformer_Core.semantic.indexes.passages", return_value=rows):
            index = Index(self.memory.store, {"generation": "0" * 32, "fingerprint": "0" * 64}, Ledger())
        current = index.candidates("meteorologia", k=1)
        historical = index.candidates("meteorologia", k=1, as_of="2025-01-01T00:00:00.000000+00:00")
        self.assertEqual(current[1], ["future"])
        self.assertEqual(historical[1], ["past"])
        self.assertNotIn("future", historical[0])

    def test_invalid_date_in_source_does_not_abort_other_grounded_claims(self):
        self.ingest("Ana nasceu em 2024-02-31.\nBruno mora em Recife.\n")
        result = self.memory.query("Bruno")
        self.assertEqual([claim["object"] for claim in result["claims"]], ["recife"])
        self.assertEqual(len(self.memory.inspect()), 1)
        self.assertTrue(self.memory.verify()["verified"])
        invalid_source = self.memory.query("Ana")
        self.assertTrue(any("2024-02-31" in hit["citation"]["quote"] for hit in invalid_source["hits"]))

    def test_reflection_with_expired_dependency_is_not_current_context(self):
        self.ingest("Bob trabalha na Contoso entre 2019 e 2022.\nBob trabalha na Fabrikam desde 2023.\n")
        self.memory.consolidate(max_items=2)
        current = self.memory.query("Bob", valid_at="2024")
        self.assertEqual([claim["object"] for claim in current["claims"]], ["fabrikam"])
        self.assertEqual(current["context"]["derived_reflections"], [])
        history = self.memory.query("Bob", history=True)
        self.assertTrue(history["context"]["derived_reflections"])


if __name__ == "__main__":
    unittest.main()
