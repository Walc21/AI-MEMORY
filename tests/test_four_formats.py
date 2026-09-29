"""Real PDF/WAV/MKV/XLSX fixtures all traverse the same byte-only algorithm."""
import json
import tempfile
import unittest
from pathlib import Path

from BN1_1.Pacote.cache import Pacote
from Transformer_Core.Hot_Hub.hub import HotHub
from examples.hot_hub_four_formats import create_examples


class FourFormatExampleTest(unittest.TestCase):
    def test_one_representation_and_exact_reconstruction_for_all_four_formats(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = create_examples(root / "inputs")
            pacote = Pacote(root / "runtime")
            pacote.open()
            pacote.add(sources)
            self.assertEqual(pacote.close(), 4)
            hub = HotHub(root / "runtime")
            manifest = json.loads(hub.manifest.read_text())
            state = json.loads(pacote.state_file.read_text())
            originals = {p.name: p.read_bytes() for p in sources}
            for item in state["items"]:
                name = item["renamed"]
                data = originals[item["name"]]
                self.assertEqual(hub.reconstruct(name, manifest["sources"]), data)
                path = hub.generations / manifest["generation"] / manifest["records"][name]["file"]
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual(rows[0]["chunk_count"], (len(data) + 1023) // 1024)
                self.assertTrue(all(len(chunk["values"]) == 1024 for chunk in rows[1:]))
                self.assertEqual(rows[0]["marker"], name.replace(".", "/", 1))
            self.assertEqual(manifest["summary"]["files"], 4)
            self.assertEqual(manifest["summary"]["bytes"], sum(map(len, originals.values())))
            self.assertEqual({p.suffix for p in hub.root.rglob("*") if p.is_file()}, {".jsonl"})
            self.assertFalse((hub.root / "data").exists())
            self.assertFalse((root / "runtime/BN1_1/BBN1_1").exists())


if __name__ == "__main__":
    unittest.main()
