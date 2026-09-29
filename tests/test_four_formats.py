import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from BN1_1.Pacote.cache import Pacote
from examples.hot_hub_four_formats import create_examples


class FourFormatExampleTest(unittest.TestCase):
    def test_one_batch_uses_the_same_entry_point_for_four_content_types(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = create_examples(root / "inputs")
            pacote = Pacote(root / "runtime")
            pacote.open()
            pacote.add(sources)
            self.assertEqual(pacote.close(), 4)
            hub = root / "runtime/Transformer_Core/Hot_Hub"
            manifest = json.loads((hub / "fields.json").read_text())
            self.assertEqual(manifest["summary"], {"decoded": 4, "partial": 0, "opaque": 0})
            by_source = {}
            all_records = []
            for name, relative in manifest["records"].items():
                record_path = hub / "representations" / manifest["generation"] / relative
                record = json.loads(record_path.read_text())
                by_source[record["adapter"]] = (record, record_path.parent)
                all_records.append((record, record_path.parent))
                self.assertEqual(record["source"]["sha256"], manifest["sources"][name])
            pdf, pdf_folder = by_source["pypdf"]
            pdf_field = pdf["fields"][0]
            letters = np.load(pdf_folder / pdf_field["chunks"][0]["samples"]["file"], allow_pickle=False)
            self.assertEqual("".join(map(chr, letters)), "Relato: a rua tem carros e pedestres.\n")
            xlsx, sheet_folder = by_source["openpyxl"]
            sheet = xlsx["fields"][0]
            cells = np.load(sheet_folder / sheet["chunks"][0]["samples"]["file"], allow_pickle=False)
            np.testing.assert_array_equal(cells, np.arange(1, 26).reshape(5, 5))
            audio, _ = next((rec, path) for rec, path in all_records
                            if rec.get("container") == "wav")
            self.assertEqual(audio["fields"][0]["native"]["sample_rate"], 8000)
            video, _ = next((rec, path) for rec, path in all_records
                            if rec.get("container") == "matroska,webm")
            frames = video["fields"][0]["chunks"]
            self.assertEqual(len(frames), 50)
            self.assertEqual(frames[0]["start"], [0, 1])
            self.assertEqual(frames[-1]["start"], [49, 10])


if __name__ == "__main__":
    unittest.main()
