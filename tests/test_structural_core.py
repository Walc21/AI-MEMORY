"""Run with python -S to prove the core does not require third-party readers."""

import json
from pathlib import Path
import tempfile
import unittest

from BN1_1.Pacote.cache import Pacote
from Transformer_Core.structural.model import Limits
from Transformer_Core.structural.protocols import observe


class StandardLibraryTests(unittest.TestCase):
    def test_text_json_csv_pipeline_without_optional_readers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = []
            for name, data in {"a.txt": b"a\nb", "b.json": b'{"a":[1,null]}', "c.csv": b"a,b\n1,2\n"}.items():
                path = root / name
                path.write_bytes(data)
                files.append(path)
            pacote = Pacote(root / "runtime")
            pacote.open()
            pacote.add(files)
            manifest = pacote.transform(strict=True)
            self.assertEqual(manifest["summary"]["complete"], 3)
            self.assertEqual(pacote.verify_transform(), manifest)
            self.assertEqual({item["protocol"]["route"] for item in pacote.inspect_transform()}, {"text", "json", "csv"})

    def test_unknown_bytes_remain_addressable_in_opaque_mode(self):
        result = observe(bytes(range(256)), "1_aB7.unknown", Limits())
        self.assertEqual(result.status, "opaque")
        self.assertEqual(result.reason, "unsupported_extension")

    def test_pillow_pdf_and_av_imports_are_lazy(self):
        # This test is also executed by a python -S CI job, where optional
        # site-packages are absent, and then must report dependency absence.
        import sys
        if sys.flags.no_site:
            for name in ("1_aB7.pdf", "1_aB7.png", "1_aB7.mkv"):
                with self.subTest(name=name):
                    self.assertEqual(observe(b"fixture", name, Limits()).reason, "missing_dependency")
