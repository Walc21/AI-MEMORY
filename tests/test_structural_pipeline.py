"""Real formats, persistence, recovery, CLI and concurrent downstream handoff."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from BN1_1.Pacote.cache import CacheError, Pacote
from BN1_2.buffer import BBN1_2, write_atomic
from examples.hot_hub_four_formats import create_examples
from Transformer_Core.Hot_Hub.hub import HotHub, HubError
from Transformer_Core.structural.model import Limits, ProtocolError, StructuralError
from Transformer_Core.structural.pipeline import StructuralPipeline

ROOT = Path(__file__).resolve().parents[1]


class StructuralPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.pacote = Pacote(self.runtime)
        self.pacote.open()
        self.buffer = BBN1_2(self.runtime)

    def add(self, name="input.txt", data=b"one\ntwo\n"):
        source = self.root / name
        source.write_bytes(data)
        self.pacote.add([source])
        return source

    def test_real_pdf_audio_video_spreadsheet_follow_the_byte_hub(self):
        sources = create_examples(self.root / "inputs")
        self.pacote.add(sources)
        manifest = self.pacote.transform(strict=True)
        self.assertEqual(manifest["summary"], {"files": 4, "nodes": 83, "edges": 235, "complete": 4, "opaque": 0})
        docs = {item["protocol"]["route"]: item for _, item in StructuralPipeline(self.runtime).documents(json.loads(HotHub(self.runtime).manifest.read_text())["sources"])}
        self.assertIn("Relato", docs["pdf"]["nodes"][1]["properties"]["text"])
        self.assertEqual(docs["wav"]["nodes"][1]["properties"]["sample_count"], 40000)
        self.assertEqual(sum(node["kind"] == "cell" for node in docs["xlsx"]["nodes"]), 25)
        self.assertEqual(sum(node["kind"] == "video_frame" for node in docs["media"]["nodes"]), 50)
        self.assertEqual(self.pacote.verify_transform(), manifest)
        hub = HotHub(self.runtime)
        snapshot = json.loads(hub.manifest.read_text())
        original = {path.name: path.read_bytes() for path in sources}
        state = json.loads(self.pacote.state_file.read_text())
        for item in state["items"]:
            self.assertEqual(hub.reconstruct(item["renamed"], snapshot["sources"]), original[item["name"]])
        self.assertEqual(state["status"], "HUB_READY")
        self.assertFalse((self.runtime / "BN1_1/BBN1_1").exists())

    def test_empty_batch_and_existing_hub_ready_cycle_need_no_migration(self):
        self.pacote.close()
        hub_bytes = HotHub(self.runtime).manifest.read_bytes()
        manifest = self.pacote.transform()
        self.assertEqual(manifest["summary"]["files"], 0)
        self.assertEqual(HotHub(self.runtime).manifest.read_bytes(), hub_bytes)
        self.assertEqual(json.loads(self.pacote.state_file.read_text())["layout_version"], 3)

    def test_idempotent_retry_force_and_dependency_profile_change(self):
        self.add()
        first = self.pacote.transform()
        self.assertEqual(self.pacote.transform(), first)
        forced = self.pacote.transform(force=True)
        self.assertNotEqual(first["generation"], forced["generation"])
        self.assertEqual(first["records"], forced["records"])
        with patch("Transformer_Core.structural.pipeline.dependency_versions", return_value={"pypdf": "changed"}):
            changed = self.pacote.transform()
        self.assertNotEqual(changed["generation"], forced["generation"])

    def test_opaque_mode_and_strict_failure_keep_previous_generation(self):
        self.add("opaque.xyz", bytes(range(256)))
        first = self.pacote.transform()
        self.assertEqual(first["summary"]["opaque"], 1)
        before = self.buffer.manifest.read_bytes()
        with self.assertRaises(ProtocolError):
            self.pacote.transform(strict=True)
        self.assertEqual(self.buffer.manifest.read_bytes(), before)
        self.assertEqual(self.pacote.status()["transform"]["status"], "FAILED")
        self.assertEqual(self.pacote.verify_transform(), first)
        self.assertEqual(self.pacote.transform(), first)

    def test_failed_publication_keeps_previous_manifest_and_retry_recovers(self):
        self.add()
        first = self.pacote.transform()
        before = self.buffer.manifest.read_bytes()
        def fail_manifest(path, payload):
            if path == self.buffer.manifest:
                raise OSError("interrupted publication")
            return write_atomic(path, payload)
        with patch("BN1_2.buffer.write_atomic", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                self.pacote.transform(force=True)
        self.assertEqual(self.buffer.manifest.read_bytes(), before)
        self.assertEqual(self.pacote.verify_transform(), first)
        self.assertEqual(self.pacote.transform(), first)

    def test_corruption_is_detected_and_repaired_from_verified_bytes(self):
        self.add()
        first = self.pacote.transform()
        name, record = next(iter(first["records"].items()))
        path = self.buffer.generations / first["generation"] / record["file"]
        path.write_text("corrupted")
        with self.assertRaises(StructuralError):
            self.pacote.verify_transform()
        repaired = self.pacote.transform()
        self.assertNotEqual(first["generation"], repaired["generation"])
        self.assertEqual(first["records"], repaired["records"])
        self.assertEqual(self.pacote.verify_transform(), repaired)

    def test_updated_checksums_do_not_allow_semantic_edges_or_path_escape(self):
        self.add()
        first = self.pacote.transform()
        name, record = next(iter(first["records"].items()))
        path = self.buffer.generations / first["generation"] / record["file"]
        document = json.loads(path.read_text())
        document["provenance"]["edges"][0]["predicate"] = "contradicts"
        path.write_text(json.dumps(document))
        first["records"][name]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.buffer.manifest.write_text(json.dumps(first))
        with self.assertRaises(StructuralError):
            self.pacote.verify_transform()
        first["records"][name]["file"] = "../../escape"
        self.buffer.manifest.write_text(json.dumps(first))
        with self.assertRaises(StructuralError):
            self.pacote.verify_transform()

    def test_extra_records_and_symlink_generation_are_refused(self):
        self.add()
        first = self.pacote.transform()
        folder = self.buffer.generations / first["generation"]
        extra = folder / "extra.json"
        extra.write_text("{}")
        with self.assertRaises(StructuralError): self.pacote.verify_transform()
        extra.unlink()
        moved = self.root / "moved"
        folder.rename(moved)
        folder.symlink_to(moved)
        with self.assertRaises(StructuralError): self.pacote.verify_transform()

    def test_stale_upstream_generation_and_modified_original_are_refused(self):
        self.add()
        first = self.pacote.transform()
        hub = HotHub(self.runtime)
        snapshot = json.loads(hub.manifest.read_text())
        path = hub.generations / snapshot["generation"] / next(iter(snapshot["records"].values()))["file"]
        path.write_text("broken")
        with self.assertRaises(HubError): self.pacote.verify_transform()
        self.pacote.close()  # Repairs the byte Hub, changing source identity.
        with self.assertRaises(StructuralError): self.pacote.verify_transform()
        second = self.pacote.transform()
        self.assertNotEqual(first["upstream"], second["upstream"])
        next(self.pacote.files.glob("*/*")).write_bytes(b"changed")
        with self.assertRaises(CacheError): self.pacote.transform()

    def test_limits_stop_before_reconstruction_and_leave_hub_intact(self):
        self.add(data=b"larger")
        self.pacote.close()
        with patch.object(HotHub, "reconstruct_snapshot", side_effect=AssertionError("must not allocate")):
            with self.assertRaises(StructuralError):
                self.pacote.transform(Limits(max_file_bytes=2))
        self.assertFalse(self.buffer.manifest.exists())
        self.assertEqual(self.pacote.status()["status"], "HUB_READY")
        self.pacote.transform()

    def test_cli_inspect_verify_failure_and_concurrent_transform(self):
        self.add()
        env = {**os.environ, "MIMIR_RUNTIME_DIR": str(self.runtime)}
        def cli(*args):
            return subprocess.run([sys.executable, "-m", "input", *args], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(cli("verify").returncode, 1)
        processes = [subprocess.Popen([sys.executable, "-m", "input", "transform"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        generations = []
        for process in processes:
            output, error = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, error)
            generations.append(json.loads(output)["generation"])
        self.assertEqual(len(set(generations)), 1)
        self.assertTrue(json.loads(cli("verify").stdout)["verified"])
        names = json.loads(cli("inspect").stdout)
        doc = json.loads(cli("inspect", "--source", names[0]["source"]).stdout)
        self.assertEqual(doc["nodes"][1]["properties"]["text"], "one\n")
        self.assertEqual(cli("inspect", "--source", "../escape").returncode, 1)
        self.assertEqual(cli("transform", "--max-nodes", "0").returncode, 1)
