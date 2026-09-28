import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InputFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.runtime = self.base / "runtime"
        self.env = {**os.environ, "MIMIR_RUNTIME_DIR": str(self.runtime)}

    def run_cli(self, *args, expected=0):
        result = subprocess.run(
            [sys.executable, "-m", "input", *map(str, args)],
            cwd=ROOT, env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def test_manual_window_counts_distinct_binary_submissions_and_only_sends_n(self):
        source = self.base / "blob.unknown"
        source.write_bytes(b"\x00\xff\x00")
        self.run_cli("open")
        self.run_cli("add", source, source)
        self.run_cli("close")
        inbox = self.runtime / "BN1_1" / "Namer" / "inbox" / "n.json"
        self.assertEqual(json.loads(inbox.read_text()), {"n": 2})
        stems = json.loads((self.runtime / "BN1_1/Namer/outbox/stems.json").read_text())["stems"]
        self.assertRegex(stems[0], r"^1_[A-Za-z0-9]{3}$")
        self.assertEqual(stems[1], "2_" + stems[0][2:])
        copies = list((self.runtime / "BN1_1" / "Pacote" / "files").glob("*/*"))
        self.assertEqual(len(copies), 2)
        self.assertEqual({copy.name for copy in copies}, {stem + ".unknown" for stem in stems})
        self.assertTrue(all(copy.read_bytes() == source.read_bytes() for copy in copies))
        self.run_cli("add", source, expected=1)
        self.run_cli("open", expected=1)
        self.run_cli("close")  # retry is safe
        self.assertEqual(json.loads(inbox.read_text()), {"n": 2})
        self.assertEqual(json.loads((self.runtime / "BN1_1/Namer/outbox/stems.json").read_text())["stems"], stems)

    def test_empty_cycle_and_missing_file_detection(self):
        self.run_cli("open")
        self.assertIn('"n": 0', self.run_cli("status").stdout)
        self.run_cli("close")
        self.assertEqual(json.loads((self.runtime / "BN1_1/Namer/inbox/n.json").read_text()), {"n": 0})
        self.assertEqual(json.loads((self.runtime / "BN1_1/Namer/outbox/stems.json").read_text()), {"stems": []})

    def test_tampered_cache_refuses_to_handoff(self):
        source = self.base / "document"
        source.write_text("content")
        self.run_cli("open")
        self.run_cli("add", source)
        next((self.runtime / "BN1_1/Pacote/files").glob("*/document")).unlink()
        self.run_cli("close", expected=1)
        self.assertFalse((self.runtime / "BN1_1/Namer/inbox/n.json").exists())

    def test_concurrent_adds_are_serialized(self):
        source = self.base / "same.txt"
        source.write_text("data")
        self.run_cli("open")
        processes = [subprocess.Popen(
            [sys.executable, "-m", "input", "add", str(source)],
            cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) for _ in range(5)]
        for process in processes:
            _, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, err.decode())
        self.run_cli("close")
        self.assertEqual(json.loads((self.runtime / "BN1_1/Namer/inbox/n.json").read_text()), {"n": 5})
        stems = json.loads((self.runtime / "BN1_1/Namer/outbox/stems.json").read_text())["stems"]
        self.assertEqual([int(stem.split("_")[0]) for stem in stems], list(range(1, 6)))

    def test_resume_an_interrupted_rename_without_new_id_or_draw(self):
        first, second = self.base / "one.PDF", self.base / "two.md"
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        self.run_cli("open")
        self.run_cli("add", first, second)
        self.run_cli("close")
        state_path = self.runtime / "cycle.json"
        state = json.loads(state_path.read_text())
        stems = json.loads((self.runtime / "BN1_1/Namer/outbox/stems.json").read_text())["stems"]
        item = state["items"][0]
        directory = self.runtime / "BN1_1/Pacote/files" / item["entry"]
        (directory / item["renamed"]).rename(directory / item["name"])
        state["status"] = "NAMING"
        state_path.write_text(json.dumps(state))
        self.run_cli("close")
        self.assertTrue((directory / item["renamed"]).is_file())
        self.assertEqual(json.loads((self.runtime / "BN1_1/Namer/outbox/stems.json").read_text())["stems"], stems)
        self.assertEqual(json.loads(state_path.read_text())["status"], "STAGED")

    def test_sorter_uses_hash_registry_and_bbn_preserves_case_and_last_suffix(self):
        names = ["a.pdf", "b.pdf", "c.PDF", "archive.tar.gz", "plain"]
        for name in names:
            (self.base / name).write_bytes(name.encode())
        self.run_cli("open")
        self.run_cli("add", *(self.base / name for name in names))
        self.run_cli("close")

        registry = json.loads((self.runtime / "BN1_1/Sorter/extension_registry.json").read_text())
        for extension in ["pdf", "PDF", "gz", ""]:
            digest = hashlib.sha256(extension.encode()).hexdigest()
            self.assertEqual(registry[digest], extension)
            partition = json.loads((self.runtime / "BN1_1/Sorter/partitions" / digest / "names.json").read_text())
            self.assertEqual(partition["idd"], digest)
            self.assertEqual(len(partition["filenames"]), 2 if extension == "pdf" else 1)
            folder = self.runtime / "BN1_1/BBN1_1" / (f"by_extension/{extension}" if extension else "no_extension")
            self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted(partition["filenames"]))
        contents = [p.read_bytes() for p in (self.runtime / "BN1_1/BBN1_1").rglob("*") if p.is_file()]
        self.assertEqual(sorted(contents), sorted(name.encode() for name in names))
        self.assertEqual(json.loads((self.runtime / "cycle.json").read_text())["status"], "STAGED")

    def test_sorter_rejects_hash_inconsistency_without_touching_cached_bytes(self):
        source = self.base / "sample.pdf"
        source.write_bytes(b"payload")
        self.run_cli("open")
        self.run_cli("add", source)
        self.run_cli("close")
        registry_path = self.runtime / "BN1_1/Sorter/extension_registry.json"
        registry = json.loads(registry_path.read_text())
        registry[hashlib.sha256(b"pdf").hexdigest()] = "other"
        registry_path.write_text(json.dumps(registry))
        self.run_cli("close", expected=1)
        cached = next((self.runtime / "BN1_1/Pacote/files").glob("*/*"))
        self.assertEqual(cached.read_bytes(), b"payload")


if __name__ == "__main__":
    unittest.main()
