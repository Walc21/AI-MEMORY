"""Regressions for valid documents rejected or changed during execution."""

from concurrent.futures import ThreadPoolExecutor
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from BN1_1.Pacote.cache import Pacote
from Transformer_Core.structural.model import Limits, PIPELINE_VERSION, ProtocolError
from Transformer_Core.structural.protocols import observe


def word_document(body):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>"
        ))
    return output.getvalue()


class PipelineRuntimeRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def pipeline(self, filename, data):
        source = self.root / filename
        source.write_bytes(data)
        pacote = Pacote(self.root / "runtime")
        pacote.open()
        pacote.add([source])
        return pacote

    def test_csv_large_multiline_field_reaches_verified_bn1_2(self):
        field = "á" * 140_000 + "\nsecond line"
        data = ('"' + field + '",ok\r\nnext,row\r\n').encode()
        pacote = self.pipeline("long.csv", data)
        manifest = pacote.transform(strict=True)
        self.assertEqual(manifest["summary"]["complete"], 1)
        name = next(iter(manifest["records"]))
        document = pacote.inspect_transform(name)
        self.assertEqual(document["nodes"][1]["properties"]["fields"], [field, "ok"])
        self.assertEqual(document["nodes"][1]["locator"]["line_end"], 2)
        self.assertEqual(document["nodes"][2]["locator"]["line_start"], 3)
        self.assertEqual(pacote.verify_transform(), manifest)
        self.assertEqual(pacote.transform(strict=True), manifest)

    def test_csv_restores_process_limit_on_success_and_parser_failure(self):
        initial = csv.field_size_limit()
        self.addCleanup(csv.field_size_limit, initial)
        csv.field_size_limit(16)
        large = b"x" * 150_000
        for data, limits, expected in (
            (large + b",ok\n", Limits(), "complete"),
            (b'"' + large, Limits(), "opaque"),
            (large + b",ok\n", Limits(max_nodes=1), "opaque"),
        ):
            with self.subTest(expected=expected, limits=limits):
                result = observe(data, "1_aB7.csv", limits)
                self.assertEqual(result.status, expected)
                self.assertEqual(csv.field_size_limit(), 16)
        limited = observe(large, "1_aB7.csv", Limits(max_text_chars=149_999))
        self.assertEqual(limited.reason, "text_limit")
        self.assertEqual(csv.field_size_limit(), 16)

    def test_concurrent_csv_and_tsv_calls_preserve_their_fields_and_global_limit(self):
        initial = csv.field_size_limit()
        def parse(index):
            field = "x" * (140_000 + index)
            is_tsv = index % 2
            delimiter, extension = ("\t", "tsv") if is_tsv else (",", "csv")
            result = observe((field + delimiter + "tail\n").encode(),
                             "1_aB7." + extension, Limits(), strict=True)
            return result.units[0].properties["fields"] == [field, "tail"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(pool.map(parse, range(12))))
        self.assertEqual(csv.field_size_limit(), initial)

    def test_docx_preserves_inline_controls_and_cell_paragraph_boundaries(self):
        data = word_document(
            "<w:p><w:r><w:t>Nome</w:t><w:tab/><w:t>Valor</w:t><w:br/>"
            "<w:t>Fim</w:t><w:cr/><w:t>A</w:t><w:noBreakHyphen/><w:t>B</w:t>"
            "<w:softHyphen/><w:t>C</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Primeiro</w:t></w:r></w:p>"
            "<w:p/><w:p><w:r><w:t>Segundo</w:t><w:tab/><w:t>Coluna</w:t>"
            "</w:r></w:p></w:tc></w:tr></w:tbl>"
        )
        pacote = self.pipeline("separators.docx", data)
        manifest = pacote.transform(strict=True)
        document = pacote.inspect_transform(next(iter(manifest["records"])))
        self.assertEqual(document["nodes"][1]["properties"]["text"], "Nome\tValor\nFim\nA\u2011B\u00adC")
        self.assertEqual(document["nodes"][3]["properties"]["cells"], ["Primeiro\n\nSegundo\tColuna"])
        self.assertEqual(document["nodes"][3]["parent_id"], document["nodes"][2]["id"])
        self.assertEqual(pacote.verify_transform(), manifest)

    def test_docx_separators_count_toward_declared_text_limit(self):
        data = word_document("<w:p><w:r><w:t>A</w:t><w:tab/><w:br/><w:t>B</w:t></w:r></w:p>")
        result = observe(data, "1_aB7.docx", Limits(max_text_chars=3))
        self.assertEqual(result.reason, "text_limit")
        self.assertEqual(result.units, [])
        with self.assertRaises(ProtocolError):
            observe(data, "1_aB7.docx", Limits(max_text_chars=3), strict=True)

    def test_previous_protocol_version_is_reprocessed_automatically(self):
        pacote = self.pipeline("input.txt", b"retained original\n")
        with patch("Transformer_Core.structural.pipeline.PIPELINE_VERSION", "0.2.0"):
            before = pacote.transform(strict=True)
        after = pacote.transform(strict=True)
        self.assertEqual(after["profile"]["pipeline_version"], PIPELINE_VERSION)
        self.assertNotEqual(after["generation"], before["generation"])
        self.assertEqual(after["upstream"], before["upstream"])
        self.assertEqual(after["records"], before["records"])
        self.assertEqual(pacote.verify_transform(), after)
        self.assertEqual(pacote.transform(strict=True), after)


if __name__ == "__main__":
    unittest.main()
