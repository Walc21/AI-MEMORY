"""Independent regression oracles from the v0.5.0 audit."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from BN1_1.Pacote.cache import Pacote
from mimir import Memory
from mimir.io import Channels


class SemanticAuditContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = Memory(self.root / "memory")
        self.count = 0

    def ingest(self, data, **settings):
        self.count += 1
        source = self.root / f"source-{self.count}.json"
        source.write_text(json.dumps(data), encoding="utf-8")
        runtime = self.root / f"runtime-{self.count}"
        p = Pacote(runtime)
        p.open()
        p.add([source])
        self.memory.ingest(runtime, **settings)

    def absent(self, query, **settings):
        result = self.memory.query(query, **settings)
        self.assertTrue(result["abstained"], result["answer"])
        for key in ("claims", "answer_evidence"):
            self.assertEqual(result[key], [])
        self.assertEqual(result["context"]["untrusted_evidence"], [])
        self.assertEqual(result["context"]["derived_reflections"], [])
        return result

    def test_ordered_names_and_organizations_in_relations_attributes_and_records(self):
        self.memory.episode("Maria Clara trabalha na Acme.\nMaria Clara favorite color is blue.\nAlice works at North South.")
        self.ingest({"name": "Maria Clara", "salary": 4200})
        for q in ["Onde Clara Maria trabalha?", "What is Clara Maria favorite color?", "What is the salary of Clara Maria?", "Does Alice work at South North?", "Who works at South North?"]:
            with self.subTest(q=q):
                self.absent(q)
        for q in ["Onde Maria Clara trabalha?", "What is Maria Clara favorite color?", "What is the salary of Maria Clara?", "Does Alice work at North South?", "Who works at North South?"]:
            self.assertFalse(self.memory.query(q)["abstained"], q)

    def test_name_particles_and_repetitions_are_not_dropped(self):
        self.memory.episode("Ana de Souza works at Acme.\nJohn John works at Beta.")
        self.absent("Where does Ana Souza work?")
        self.absent("Where does John work?")
        self.assertFalse(self.memory.query("Where does Ana de Souza work?")["abstained"])
        self.assertFalse(self.memory.query("Where does John John work?")["abstained"])

    def test_nested_entities_keep_field_ownership_after_restart_and_publish(self):
        self.ingest({"person_a": {"name": "Alice", "salary": 100}, "person_b": {"name": "Bob", "password": "SECRET"}})
        self.memory = Memory(self.root / "memory")
        for q in ["What is the password of Alice?", "What is the salary of Bob?"]:
            self.absent(q)
            result = Channels(self.memory).publish_query(q)
            self.assertTrue(result["result"]["abstained"])
        for q, value in [("What is the salary of Alice?", "100"), ("What is the password of Bob?", "SECRET")]:
            result = self.memory.query(q)
            self.assertFalse(result["abstained"])
            self.assertIn(value, result["answer"])
        self.assertTrue(self.memory.verify()["verified"])

    def test_deep_sibling_records_and_nested_attributes(self):
        self.ingest({"group": {"a": {"name": "Alice", "salary": 10}, "b": {"name": "Bob", "password": "SECRET"}}, "sensor": "Orion", "spec": {"serial": "SN-42"}})
        self.absent("What is the password of Alice?")
        self.assertIn("SN-42", self.memory.query("What is the serial of sensor Orion?")["answer"])

    def test_literal_dotted_key_and_nested_path_are_distinct(self):
        for data in [{"sensor": "Orion", "spec.serial": "LITERAL", "spec": {"serial": "NESTED"}}, {"sensor": "Orion", "spec": {"serial": "NESTED"}, "spec.serial": "LITERAL"}]:
            self.ingest(data)
        for q, value, excluded in [("What is the /spec.serial of sensor Orion?", "LITERAL", "NESTED"), ("What is the /spec/serial of sensor Orion?", "NESTED", "LITERAL"), ("What is the spec.serial of sensor Orion?", "LITERAL", "NESTED")]:
            result = self.memory.query(q)
            self.assertFalse(result["abstained"])
            self.assertIn(value, result["answer"])
            self.assertNotIn(excluded, result["answer"])
        self.assertTrue(self.memory.verify()["verified"])

    def test_past_year_does_not_establish_present_state(self):
        self.memory.episode("João trabalhava na Acme em 2020.\nBob worked at Beta.\nAlice works at Cedar since 2020.\nMaria nasceu em 1992.")
        self.absent("Onde João trabalha?", valid_at="2026")
        self.absent("Where does Bob work?", valid_at="2026")
        for q, settings in [("Onde João trabalhava em 2020?", {"valid_at": "2020"}), ("Onde João trabalhava?", {}), ("Where did Bob work?", {}), ("Where does Alice work?", {"valid_at": "2026"}), ("Quando Maria nasceu?", {"valid_at": "2026"}), ("Onde João trabalha?", {"history": True})]:
            self.assertFalse(self.memory.query(q, **settings)["abstained"], q)

    def test_isolated_day_and_explicit_interval_have_different_coverage(self):
        self.memory.episode("Alice works at Acme in 2020-02-01.\nBob works at Beta between 2019 and 2022.")
        self.absent("Where does Alice work?", valid_at="2020-02-02")
        self.assertFalse(self.memory.query("Where does Alice work?", valid_at="2020-02-01")["abstained"])
        self.assertFalse(self.memory.query("Where does Bob work?", valid_at="2021")["abstained"])
        self.absent("Where does Bob work?", valid_at="2026")

    def test_past_continuity_and_unparsed_attributes_do_not_bypass_time(self):
        self.memory.episode("João trabalhava na Acme desde 2020.\nAlice favorite color was blue in 2020.")
        self.absent("Onde João trabalha?", valid_at="2026")
        self.absent("What is Alice favorite color?", valid_at="2026")
        self.assertFalse(self.memory.query("What was Alice favorite color in 2020?")["abstained"])

    def test_v1_projection_evidence_remains_verifiable_without_answer_authority(self):
        from Transformer_Core.semantic.evidence import texts
        from Transformer_Core.semantic.views_v1 import DocumentViews as LegacyViews
        def old_texts(node, views=None):
            yield from texts(node, LegacyViews(views.document) if views else None)
        with patch("Transformer_Core.semantic.pipeline.texts", side_effect=old_texts):
            self.ingest({"a": {"name": "Alice", "salary": 10}, "b": {"name": "Bob", "password": "SECRET"}}, episode={"type": "interaction"})
        self.assertTrue(self.memory.verify()["verified"])
        self.assertTrue(any(ev["property"].startswith("projection:record-v1") for row in self.memory.inspect("episodes") for ev in row["evidence"]))
        self.memory.consolidate()
        self.absent("What is the password of Alice?")
        self.assertIn("SECRET", self.memory.query("What is the password of Bob?")["answer"])
        context = self.memory.query("What is the salary of Alice?")["context"]
        self.assertNotIn("SECRET", json.dumps(context))

    def test_keys_in_sibling_branches_cannot_impersonate_an_identifier(self):
        self.ingest({"alice": {"salary": 100}, "bob": {"password": "SECRET"}})
        self.absent("What is Alice password?")
        self.absent("What is Bob salary?")
