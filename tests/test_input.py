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
        copies = list((self.runtime / "BN1_1" / "Pacote" / "files").glob("*/blob.unknown"))
        self.assertEqual(len(copies), 2)
        self.assertTrue(all(copy.read_bytes() == source.read_bytes() for copy in copies))
        self.run_cli("add", source, expected=1)
        self.run_cli("open", expected=1)
        self.run_cli("close")  # retry is safe
        self.assertEqual(json.loads(inbox.read_text()), {"n": 2})

    def test_empty_cycle_and_missing_file_detection(self):
        self.run_cli("open")
        self.assertIn('"n": 0', self.run_cli("status").stdout)
        self.run_cli("close")
        self.assertEqual(json.loads((self.runtime / "BN1_1/Namer/inbox/n.json").read_text()), {"n": 0})

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


if __name__ == "__main__":
    unittest.main()
