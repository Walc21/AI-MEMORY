"""Real OCR/signature integration; deterministic local model contract fixtures."""

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from BN1_1.Pacote.cache import Pacote
from Transformer_Core.semantic.evaluation import adapt, evaluate
from Transformer_Core.semantic.extraction import rule_claims
from Transformer_Core.semantic.model import SemanticError, SemanticLimits
from Transformer_Core.semantic.multimodal import observations, profile
from Transformer_Core.semantic.pipeline import Memory
from Transformer_Core.semantic.worker import _asr, _vision, execute

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def ollama_fixture(invalid=False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if invalid == "redirect":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/never-follow")
                self.end_headers()
                return
            if self.path == "/api/show":
                value = {"model_info": {"architecture": "fixture", "revision": "test-only"}}
            elif data.get("images"):
                value = {"response": json.dumps({"observations": [{"text": "A red rectangle.", "confidence": 0.8}]})}
            else:
                text = json.loads(data["prompt"].split("\n", 1)[1])
                claims = rule_claims(text)
                if invalid and claims:
                    claims[0]["quote"] = "fabricated"
                value = {"response": json.dumps({"claims": claims})}
            payload = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class SemanticIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = Memory(self.root / "memory")

    def runtime(self, path):
        runtime = self.root / "runtime"
        pacote = Pacote(runtime)
        pacote.open()
        pacote.add([path])
        return runtime

    def picture(self, words=None):
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            self.skipTest("Pillow optional dependency absent")
        image = Image.new("RGB", (700, 140), "white")
        if words:
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", 36)
            except OSError:
                self.skipTest("OCR integration needs DejaVuSans.ttf")
            ImageDraw.Draw(image).text((20, 40), words, font=font, fill="black")
        path = self.root / "image.png"
        image.save(path)
        return path

    def test_real_ocr_preserves_pixels_and_cites_line_bounding_box(self):
        if not shutil.which("tesseract"):
            self.skipTest("Tesseract optional system dependency absent")
        image = self.picture("Alice works at Acme.")
        original = image.read_bytes()
        runtime = self.runtime(image)
        generation = self.memory.ingest(runtime, ocr=True, strict_multimodal=True)
        self.assertEqual(image.read_bytes(), original)
        self.assertEqual(generation["summary"]["observations"], 1)
        result = self.memory.query("Where Alice works?")
        self.assertEqual(result["claims"][0]["object"], "acme")
        observation = result["hits"][0]["citation"]["derived_observation"]
        self.assertEqual(observation["method"], "ocr")
        self.assertGreater(observation["locator"]["width"], 0)
        self.assertTrue(self.memory.verify()["verified"])

    def test_strict_multimodal_failure_keeps_existing_memory_and_default_reports_failure(self):
        runtime = self.runtime(self.picture())
        first = self.memory.ingest(runtime)
        with patch("Transformer_Core.semantic.multimodal.execute", side_effect=SemanticError("fixture model missing")):
            with self.assertRaises(SemanticError): self.memory.ingest(runtime, ocr=True, strict_multimodal=True)
            self.assertEqual(self.memory.verify()["generation"], first["generation"])
            self.memory.ingest(runtime, ocr=True)
        self.assertTrue(any(row["multimodal_errors"] for row in self.memory.inspect("reports")))

    def test_local_ollama_worker_contract_and_untrusted_text(self):
        source = self.root / "source.txt"
        source.write_text("Alice works at Acme.\n")
        runtime = self.runtime(source)
        with ollama_fixture() as endpoint:
            generation = self.memory.ingest(runtime, extractor="ollama", model="fixture-model", endpoint=endpoint)
        run = self.memory.inspect("inference_runs")[0]
        self.assertEqual(len(run["model_revision"]), 64)
        self.assertEqual(run["model_id"], "fixture-model")
        self.assertEqual(self.memory.query("Alice")["claims"][0]["object"], "acme")
        with ollama_fixture(invalid=True) as endpoint:
            with self.assertRaises(SemanticError):
                self.memory.ingest(runtime, extractor="ollama", model="fixture-model", endpoint=endpoint)
        self.assertEqual(self.memory.verify()["generation"], generation["generation"])

    def test_model_redirect_cannot_escape_the_local_endpoint(self):
        source = self.root / "source.txt"
        source.write_text("Alice works at Acme.\n")
        runtime = self.runtime(source)
        with ollama_fixture(invalid="redirect") as endpoint:
            with self.assertRaisesRegex(SemanticError, "Redirecionamento"):
                self.memory.ingest(runtime, extractor="ollama", model="fixture-model", endpoint=endpoint)
        self.assertFalse(self.memory.store.manifest.exists())

    def test_vision_local_model_records_derived_description(self):
        runtime = self.runtime(self.picture())
        with ollama_fixture() as endpoint:
            self.memory.ingest(runtime, vision_model="fixture-vision", endpoint=endpoint, strict_multimodal=True)
        row = self.memory.inspect("observations")[0]
        self.assertEqual(row["method"], "vision")
        result = self.memory.query("red rectangle")
        self.assertEqual(result["hits"][0]["citation"]["derived_observation"]["id"], row["id"])
        self.assertEqual(result["claims"], [])

    def test_asr_adapter_sample_coordinates_and_local_weights_revision(self):
        folder = self.root / "model"
        folder.mkdir()
        (folder / "model.bin").write_bytes(b"fixture weights")
        first = profile(asr_model=str(folder))
        (folder / "model.bin").write_bytes(b"new weights")
        self.assertNotEqual(first["asr_revision"], profile(asr_model=str(folder))["asr_revision"])
        model = Mock()
        model.transcribe.return_value = ([SimpleNamespace(text=" Alice works at Acme. ", start=1.25, end=3.5, no_speech_prob=.1)], None)
        with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=Mock(return_value=model))}):
            result = _asr({"model": str(folder), "data": base64.b64encode(b"fixture").decode(), "locator": {"type": "sample_range"}})
        self.assertEqual(result[0]["locator"]["sample_start"], 20000)
        self.assertEqual(result[0]["locator"]["sample_end"], 56000)
        self.assertEqual(result[0]["method"], "asr")
        (folder / "unsafe").symlink_to(self.root / "outside")
        with self.assertRaises(SemanticError): profile(asr_model=str(folder))

    def test_multimodal_invalid_coordinates_never_publish(self):
        runtime = self.runtime(self.picture())
        first = self.memory.ingest(runtime)
        bogus = [{"text": "Alice works at Acme.", "locator": {"type": "ocr_box", "left": 10, "top": 0,
            "width": 100, "height": 50, "image_width": 20, "image_height": 20}, "confidence": .8, "method": "ocr"}]
        with patch("Transformer_Core.semantic.multimodal.execute", return_value=bogus):
            with self.assertRaises(SemanticError): self.memory.ingest(runtime, ocr=True)
        self.assertEqual(self.memory.verify()["generation"], first["generation"])

    def test_optional_signatures_authenticate_generation_and_force_signed_updates(self):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            self.skipTest("cryptography optional dependency absent")
        key_path = self.root / "key.pem"
        key_path.write_bytes(Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key_path.chmod(0o600)
        source = self.root / "text.txt"
        source.write_text("Alice works at Acme.\n")
        runtime = self.runtime(source)
        first = self.memory.ingest(runtime, signing_key=key_path)
        self.assertTrue(self.memory.verify()["signature"])
        with self.assertRaises(SemanticError): self.memory.ingest(runtime, force=True)
        self.assertEqual(self.memory.verify()["generation"], first["generation"])
        self.memory.ingest(runtime, force=True, signing_key=key_path)
        manifest = json.loads(self.memory.store.manifest.read_text())
        manifest["signature"]["value"] = "00" * 64
        self.memory.store.manifest.write_text(json.dumps(manifest))
        (self.memory.store.generations / manifest["generation"] / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(SemanticError): self.memory.verify()

    def test_worker_blocks_network_external_writes_and_arbitrary_programs(self):
        code = """
import os, socket, subprocess
from Transformer_Core.semantic.worker import _guard
_guard({'mode': 'extract', 'profile': {'name': 'rules'}})
operations = [lambda: open(os.environ['MIMIR_TEST_TARGET'], 'w'),
              lambda: os.remove(os.environ['MIMIR_TEST_TARGET']),
              lambda: socket.create_connection(('127.0.0.1', 9)),
              lambda: subprocess.run(['/bin/sh', '-c', 'true'])]
for operation in operations:
    try:
        operation()
        raise AssertionError('operation escaped the guard')
    except PermissionError:
        pass
"""
        target = self.root / "outside-worker"
        target.write_text("keep")
        temp = self.root / "worker"
        temp.mkdir()
        env = {**os.environ, "MIMIR_WORKER_MEMORY_MB": "512", "MIMIR_WORKER_CPU": "10",
               "MIMIR_WORKER_OUTPUT": "10000", "MIMIR_WORKER_TMP": str(temp), "MIMIR_TEST_TARGET": str(target)}
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(), "keep")

    def test_evaluation_and_longmemeval_locomo_adapters(self):
        long = adapt([{"haystack_sessions": [[{"content": "Alice works at Acme."}]], "question": "Alice?", "answer": "Acme"}])
        loco = adapt([{"conversation": {"session_1": [{"text": "Alice works at Acme."}]}, "qa": [{"question": "Alice?", "answer": "Acme"}]}])
        self.assertEqual(long["episodes"], loco["episodes"])
        metrics = evaluate()
        self.assertEqual(metrics["recall_at_8"], 1)
        self.assertEqual(metrics["conflict_accuracy"], 1)
        self.assertEqual(metrics["abstention_accuracy"], 1)
        self.assertTrue(metrics["index_rebuild_equivalent"])
        self.assertTrue(metrics["canonical_unchanged_by_index_rebuild"])
        self.assertEqual(metrics["llm_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
