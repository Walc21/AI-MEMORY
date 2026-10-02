"""Observable extraction, bounded fallback and the G_P semantic barrier."""

from copy import deepcopy
import hashlib
import io
import json
import unittest
from unittest.mock import patch
import wave
import zipfile
from PIL import Image
from openpyxl import Workbook

from Transformer_Core.structural.curator import assemble, node_id, validate
from Transformer_Core.structural.model import Limits, ProtocolError, StructuralError
from Transformer_Core.structural.protocols import observe
from Transformer_Core.structural.router import route


def archive(parts):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as stream:
        for name, data in parts.items():
            stream.writestr(name, data)
    return output.getvalue()


def document(data, name="1_aB7.txt", limits=None, strict=True):
    source = {"name": name, "id": "a" * 32 + ":" + name,
              "sha256": hashlib.sha256(data).hexdigest(), "byte_length": len(data),
              "hot_hub_generation": "a" * 32, "record_sha256": "b" * 64}
    return assemble(source, observe(data, name, limits or Limits(), strict))


class ProtocolTests(unittest.TestCase):
    def test_text_locators_recover_exact_utf8_bytes_and_order(self):
        data = b"\xef\xbb\xbf" + "ação\r\nnext\nlast".encode()
        doc = document(data)
        self.assertEqual([n["properties"]["text"] for n in doc["nodes"][1:]], ["ação\r\n", "next\n", "last"])
        for node in doc["nodes"][1:]:
            self.assertEqual(data[node["locator"]["start"]:node["locator"]["end"]].decode(), node["properties"]["text"])
        predicates = [edge["predicate"] for edge in doc["provenance"]["edges"]]
        self.assertEqual(predicates.count("precedes"), 2)
        self.assertEqual(set(predicates), {"contains", "derived_from", "precedes"})

    def test_json_pointer_escaping_null_and_parentage(self):
        doc = document(b'{"a/b~":[null,true,3]}', "1_aB7.json")
        nodes = doc["nodes"]
        self.assertEqual([node["locator"]["pointer"] for node in nodes[1:]], ["", "/a~1b~0", "/a~1b~0/0", "/a~1b~0/1", "/a~1b~0/2"])
        self.assertEqual(nodes[3]["parent_id"], nodes[2]["id"])
        self.assertIsNone(nodes[3]["properties"]["value"])
        self.assertEqual(document(b"", strict=True)["nodes"][0]["properties"]["byte_length"], 0)

    def test_csv_multiline_fields_keep_physical_line_coordinates(self):
        doc = document(b'a,b\r\n"first\nsecond",3\r\n', "1_aB7.csv")
        self.assertEqual(doc["nodes"][2]["properties"]["fields"], ["first\nsecond", "3"])
        self.assertEqual(doc["nodes"][2]["locator"], {"type": "csv_row", "row": 2, "line_start": 2, "line_end": 3})
        tsv = document(b"a\tb\n", "1_aB7.tsv")
        self.assertEqual(tsv["nodes"][1]["properties"]["fields"], ["a", "b"])

    def test_docx_paragraph_table_and_embedded_media_have_member_locations(self):
        body = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
        <w:p><w:r><w:t>Paragraph</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>'''
        data = archive({"word/document.xml": body, "word/media/image1.png": b"opaque_image"})
        doc = document(data, "1_aB7.docx")
        self.assertEqual([n["kind"] for n in doc["nodes"]], ["file", "paragraph", "table", "table_row", "embedded_media"])
        self.assertEqual(doc["nodes"][3]["parent_id"], doc["nodes"][2]["id"])
        self.assertEqual(doc["nodes"][4]["properties"]["sha256"], hashlib.sha256(b"opaque_image").hexdigest())

    def test_wav_sample_coordinates_and_truncation(self):
        stream = io.BytesIO()
        with wave.open(stream, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\x00\x00" * 160)
        data = stream.getvalue()
        doc = document(data, "1_aB7.wav")
        self.assertEqual(doc["nodes"][1]["properties"]["duration"], "1/50")
        self.assertEqual(doc["nodes"][1]["locator"]["end"], 160)
        result = observe(data[:-2], "1_aB7.wav", Limits())
        self.assertEqual(result.reason, "truncated_audio")

    def test_unknown_invalid_duplicate_keys_and_missing_dependency_are_explicit(self):
        cases = [(b"anything", "1_aB7.xyz", "unsupported_extension"),
                 (b"not PDF", "1_aB7.pdf", "invalid_format"),
                 (b"\xff", "1_aB7.txt", "invalid_format"),
                 (b'{"a":1,"a":2}', "1_aB7.json", "duplicate_json_key"),
                 (b"NaN", "1_aB7.json", "non_finite_json"),
                 (b"1e309", "1_aB7.json", "non_finite_json")]
        for data, name, reason in cases:
            with self.subTest(name=name, reason=reason):
                doc = document(data, name, strict=False)
                self.assertEqual(doc["protocol"]["reason"], reason)
                self.assertEqual(len(doc["nodes"]), 1)
                with self.assertRaises(ProtocolError):
                    observe(data, name, Limits(), strict=True)
        with patch.dict("Transformer_Core.structural.protocols.PROTOCOLS", {"pdf": lambda *args: (_ for _ in ()).throw(ImportError())}):
            self.assertEqual(observe(b"x", "1_aB7.pdf", Limits()).reason, "missing_dependency")

    def test_limits_discard_partial_observations(self):
        for limits, reason in [(Limits(max_nodes=2), "node_limit"), (Limits(max_text_chars=2), "text_limit")]:
            doc = document(b"one\ntwo\n", limits=limits, strict=False)
            self.assertEqual(doc["protocol"]["reason"], reason)
            self.assertEqual(len(doc["nodes"]), 1)
        with self.assertRaises(StructuralError):
            observe(b"123", "1_aB7.txt", Limits(max_file_bytes=2))
        with self.assertRaises(ValueError):
            Limits(max_nodes=True)

    def test_ooxml_refuses_entities_and_archive_escape(self):
        for parts, reason in [({"../escape": b"x"}, "invalid_archive_paths"),
            ({"word/document.xml": b'<!DOCTYPE x [<!ENTITY a "xx">]><x>&a;</x>'}, "xml_entities_refused")]:
            self.assertEqual(observe(archive(parts), "1_aB7.docx", Limits()).reason, reason)
        self.assertEqual(observe(archive({"word/document.xml": b"x" * 1000}), "1_aB7.docx", Limits(max_expanded_bytes=100)).reason, "archive_limit")
        xml = '<!DOCTYPE x [<!ENTITY a "xx">]><x>&a;</x>'.encode("utf-16")
        self.assertEqual(observe(archive({"word/document.xml": xml}), "1_aB7.docx", Limits()).reason, "xml_entities_refused")

    def test_image_dimensions_validation_and_pixel_limit(self):
        stream = io.BytesIO()
        Image.new("RGB", (12, 8), color="red").save(stream, format="PNG")
        data = stream.getvalue()
        doc = document(data, "1_aB7.PNG")
        self.assertEqual(doc["nodes"][1]["properties"], {"width": 12, "height": 8, "mode": "RGB", "format": "PNG"})
        self.assertEqual(observe(data, "1_aB7.png", Limits(max_pixels=10)).reason, "pixel_limit")
        self.assertEqual(observe(data[:20], "1_aB7.png", Limits()).status, "opaque")

    def test_xlsx_preserves_formula_and_strings_without_evaluation(self):
        stream = io.BytesIO()
        book = Workbook()
        book.active["A1"] = "=1+2"
        book.active["B1"] = "hello"
        book.active["C1"] = True
        book.save(stream)
        doc = document(stream.getvalue(), "1_aB7.xlsx")
        cells = {n["locator"]["coordinate"]: n["properties"] for n in doc["nodes"] if n["kind"] == "cell"}
        self.assertEqual(cells["A1"]["formula"], "1+2")
        self.assertIsNone(cells["A1"]["value"])
        self.assertEqual(cells["B1"]["value"], "hello")
        self.assertEqual(cells["C1"]["cell_type"], "b")

    def test_curadoria_rejects_semantic_edges_foreign_origins_and_orphans(self):
        original = document(b"one\ntwo")
        for mutation in ("semantic", "source", "parent", "sequence", "locator", "locator_type", "extra", "meaning"):
            doc = deepcopy(original)
            if mutation == "semantic": doc["provenance"]["edges"][0]["predicate"] = "explains"
            if mutation == "source": doc["nodes"][1]["source_id"] = "other"
            if mutation == "parent": doc["nodes"][1]["parent_id"] = doc["nodes"][2]["id"]
            if mutation == "sequence": doc["nodes"][1]["sequence"] = True
            if mutation == "locator": doc["nodes"][1]["locator"]["end"] = 1000
            if mutation == "locator_type": doc["nodes"][1]["locator"] = {"type": "image_frame", "frame": 0}
            if mutation == "extra": doc["nodes"][1]["meaning"] = "invented"
            if mutation == "meaning": doc["nodes"][1]["properties"]["meaning"] = "invented"
            with self.subTest(mutation=mutation), self.assertRaises(StructuralError):
                validate(doc, original["source"])

    def test_routing_preserves_case_in_identity_and_repeated_text_is_distinct(self):
        self.assertEqual(route("1_aB7.PDF"), "pdf")
        self.assertEqual(route("1_aB7"), "unknown")
        doc = document(b"same\nsame\n")
        self.assertNotEqual(doc["nodes"][1]["id"], doc["nodes"][2]["id"])
        self.assertEqual(len({n["id"] for n in doc["nodes"]}), 3)
        self.assertEqual(doc, document(b"same\nsame\n"))
