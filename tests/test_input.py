import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from BN1_1.BBN1_1.buffer import BBN1_1, BufferError
from BN1_1.Sorter.sorter import Sorter, SorterError
from Transformer_Core.Hot_Hub.hub import HotHub, HubError


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
        self.assertEqual(json.loads(state_path.read_text())["status"], "HUB_READY")

    def test_sorter_and_hub_preserve_case_and_last_suffix(self):
        names = ["a.pdf", "b.pdf", "c.PDF", "archive.tar.gz", "plain"]
        for name in names:
            (self.base / name).write_bytes(name.encode())
        self.run_cli("open")
        self.run_cli("add", *(self.base / name for name in names))
        self.run_cli("close")

        state = json.loads((self.runtime / "cycle.json").read_text())
        groups = Sorter.classify([item["renamed"] for item in state["items"]])
        self.assertEqual(set(groups), {"pdf", "PDF", "gz", ""})
        for extension in ["pdf", "PDF", "gz", ""]:
            self.assertEqual(len(groups[extension]), 2 if extension == "pdf" else 1)
            folder = self.runtime / "Transformer_Core/Hot_Hub/data" / (f"by_extension/{extension}" if extension else "no_extension")
            self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted(groups[extension]))
        contents = [p.read_bytes() for p in (self.runtime / "Transformer_Core/Hot_Hub/data").rglob("*") if p.is_file()]
        self.assertEqual(sorted(contents), sorted(name.encode() for name in names))
        self.assertFalse((self.runtime / "BN1_1/BBN1_1").exists())
        self.assertEqual(json.loads((self.runtime / "cycle.json").read_text())["status"], "HUB_READY")

    def test_sorter_rejects_invalid_or_repeated_names(self):
        with self.assertRaises(SorterError):
            Sorter.classify(["sample.pdf"])
        with self.assertRaises(SorterError):
            Sorter.classify(["1_aB7.pdf", "1_aB7.pdf"])
        with self.assertRaises(SorterError):
            Sorter.classify(["../1_aB7.pdf"])

    def test_separate_cycles_classify_same_extension_without_registry(self):
        first = self.base / "first.pdf"
        first.write_bytes(b"first")
        self.run_cli("open")
        self.run_cli("add", first)
        self.run_cli("close")
        self.env["MIMIR_RUNTIME_DIR"] = str(self.base / "second-cycle")
        second = self.base / "second.pdf"
        second.write_bytes(b"second")
        self.run_cli("open")
        self.run_cli("add", second)
        self.run_cli("close")
        second_folder = self.base / "second-cycle/Transformer_Core/Hot_Hub/data/by_extension/pdf"
        self.assertEqual(len(list(second_folder.iterdir())), 1)
        self.assertFalse((self.base / "persistent").exists())

    def test_changed_cached_bytes_are_refused_and_incomplete_add_is_recovered(self):
        source = self.base / "source.txt"
        source.write_bytes(b"original")
        self.run_cli("open")
        self.run_cli("add", source)
        orphan = self.runtime / "BN1_1/Pacote/files" / ("f" * 32)
        orphan.mkdir()
        (orphan / "partial").write_bytes(b"partial")
        cached = next((self.runtime / "BN1_1/Pacote/files").glob("*/source.txt"))
        cached.write_bytes(b"altered!")
        self.run_cli("close", expected=1)
        self.assertFalse(orphan.exists())
        folder = self.runtime / "BN1_1/BBN1_1/by_extension/txt"
        self.assertFalse(folder.exists() and any(folder.iterdir()))

    def test_bbn_cleanup_requires_both_hub_and_pacote_copies(self):
        bbn = BBN1_1(self.runtime)
        original = bbn.root / "by_extension" / "pdf" / "1_aB7.pdf"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"verified")
        relative = "by_extension/pdf/1_aB7.pdf"
        expected = {relative: hashlib.sha256(b"verified").hexdigest()}
        hub = HotHub(self.runtime)
        hub.mirror(bbn.root, expected)

        with self.assertRaises(BufferError):
            bbn.purge_verified(expected, hub, lambda: {})
        self.assertTrue(original.exists())
        (hub.data / relative).write_bytes(b"corrupted")
        with self.assertRaises(HubError):
            bbn.purge_verified(expected, hub, lambda: expected)
        self.assertTrue(original.exists())

        hub.mirror(bbn.root, expected)
        bbn.purge_verified(expected, hub, lambda: expected)
        self.assertFalse(bbn.root.exists())
        self.assertEqual((hub.data / relative).read_bytes(), b"verified")

    def test_interrupted_bbn_cleanup_can_restart_from_pacote(self):
        source = self.base / "sample.md"
        source.write_bytes(b"document")
        self.run_cli("open")
        self.run_cli("add", source)
        self.run_cli("close")
        state_path = self.runtime / "cycle.json"
        state = json.loads(state_path.read_text())
        state["status"] = "HUB_VERIFIED"
        state_path.write_text(json.dumps(state))
        self.run_cli("close")
        self.assertFalse((self.runtime / "BN1_1/BBN1_1").exists())
        self.assertEqual(json.loads(state_path.read_text())["status"], "HUB_READY")

        mirrored = next((self.runtime / "Transformer_Core/Hot_Hub/data").rglob("*.md"))
        mirrored.write_bytes(b"damage")
        self.run_cli("close")
        self.assertEqual(mirrored.read_bytes(), b"document")
        self.assertEqual(json.loads(state_path.read_text())["status"], "HUB_READY")


if __name__ == "__main__":
    unittest.main()
