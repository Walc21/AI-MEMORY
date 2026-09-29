import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from BN1_1.Pacote.cache import CacheError, Pacote
from Transformer_Core.Hot_Hub.hub import CHUNK_SIZE, HotHub, HubError, byte_chunks, marker


class ByteHubTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "bbn"
        self.source.mkdir()
        self.hub = HotHub(self.root / "runtime")

    def build(self, files):
        for name, data in files.items():
            (self.source / name).write_bytes(data)
        expected = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        return expected, self.hub.build(self.source, expected)

    def record_path(self, manifest, name):
        return self.hub.generations / manifest["generation"] / manifest["records"][name]["file"]

    def test_boundaries_and_all_256_byte_values_are_lossless(self):
        for length in (0, 1, 1023, 1024, 1025, 2048, 2049, 10000):
            with self.subTest(length=length):
                vector = (bytes(range(256)) * 40)[:length]
                expected, manifest = self.build({"1_aB7.bin": vector})
                self.assertEqual(self.hub.reconstruct("1_aB7.bin", expected), vector)
                rows = [json.loads(line) for line in self.record_path(manifest, "1_aB7.bin").read_text().splitlines()]
                self.assertEqual(len(rows) - 1, (length + 1023) // 1024)
                self.assertEqual(sum(c["valid_length"] for c in rows[1:]), length)
                self.assertTrue(all(len(c["values"]) == CHUNK_SIZE for c in rows[1:]))
                if length % 1024:
                    self.assertEqual(rows[-1]["values"][length % 1024:], [0] * (1024 - length % 1024))

    def test_arbitrary_extensions_do_not_change_bytes_or_algorithm(self):
        files = {f"{i}_aB7{ext}": b"not a decodable media file\x00\xff" for i, ext in enumerate(
            (".pdf", ".wav", ".xlsx", ".mkv", ".PDF", ".unknown", ""), 1)}
        expected, manifest = self.build(files)
        for name, data in files.items():
            self.assertEqual(self.hub.reconstruct(name, expected), data)
        self.assertEqual(manifest["summary"]["files"], 7)
        self.assertEqual(marker("1_aB7.PDF"), "1_aB7/PDF")
        self.assertEqual(marker("2_aB7"), "2_aB7/")

    def test_identical_chunks_keep_indices_and_file_identity(self):
        files = {"1_aB7.bin": b"\0" * 2048, "2_aB7.bin": b"\0" * 2048}
        expected, manifest = self.build(files)
        identities = set()
        for name in files:
            rows = [json.loads(line) for line in self.record_path(manifest, name).read_text().splitlines()]
            self.assertEqual([c["index"] for c in rows[1:]], [0, 1])
            identities.add(rows[1]["source_id"])
            self.assertEqual(self.hub.reconstruct(name, expected), files[name])
        self.assertEqual(len(identities), 2)

    def test_same_labels_in_distinct_batches_have_distinct_source_ids(self):
        expected, first = self.build({"1_aB7.pdf": b"abc"})
        _, second = self.build({"1_aB7.pdf": b"abc"})
        def identity(manifest):
            return json.loads(self.record_path(manifest, "1_aB7.pdf").read_text().splitlines()[0])["source_id"]
        self.assertNotEqual(identity(first), identity(second))
        self.assertEqual(self.hub.reconstruct("1_aB7.pdf", expected), b"abc")

    def test_bad_chunks_rejected_even_when_record_digest_is_updated(self):
        expected, initial = self.build({"1_aB7.bin": bytes(range(256)) * 5})
        path = self.record_path(initial, "1_aB7.bin")
        original = path.read_text()
        def mutate_values(rows): rows[1]["values"][0] = 999
        def mutate_byte(rows): rows[1]["values"][0] = 100
        def mutate_bool(rows): rows[1]["values"][0] = False
        def mutate_index(rows): rows[1]["index"] = 2
        def mutate_index_bool(rows): rows[1]["index"] = False
        def mutate_marker(rows): rows[1]["marker"] = "2_aB7/bin"
        def mutate_identity(rows): rows[1]["source_id"] = "other-cycle:1_aB7.bin"
        def mutate_padding(rows): rows[-1]["values"][-1] = 1
        def mutate_length(rows): rows[-1]["valid_length"] += 1
        def mutate_dimension(rows): rows[1]["values"].pop()
        def mutate_order(rows): rows[1], rows[2] = rows[2], rows[1]
        def mutate_missing(rows): rows.pop()
        def mutate_extra(rows): rows.append(rows[-1])
        for mutation in (mutate_values, mutate_byte, mutate_bool, mutate_index, mutate_index_bool,
                         mutate_marker, mutate_identity, mutate_padding, mutate_length,
                         mutate_dimension, mutate_order, mutate_missing, mutate_extra):
            with self.subTest(mutation=mutation.__name__):
                rows = [json.loads(line) for line in original.splitlines()]
                mutation(rows)
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                manifest = json.loads(json.dumps(initial))
                manifest["records"]["1_aB7.bin"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                self.hub.manifest.write_text(json.dumps(manifest) + "\n")
                with self.assertRaises(HubError): self.hub.verify(expected)

    def test_mixed_file_records_and_path_traversal_rejected(self):
        expected, manifest = self.build({"1_aB7.bin": b"one", "2_aB7.bin": b"two"})
        manifest["records"]["1_aB7.bin"] = manifest["records"]["2_aB7.bin"]
        self.hub.manifest.write_text(json.dumps(manifest))
        with self.assertRaises(HubError): self.hub.verify(expected)
        manifest["records"]["1_aB7.bin"]["file"] = "../../outside"
        self.hub.manifest.write_text(json.dumps(manifest))
        with self.assertRaises(HubError): self.hub.verify(expected)

    def test_extra_source_or_output_and_wrong_digest_refused(self):
        expected, manifest = self.build({"1_aB7.bin": b"one"})
        (self.source / "2_aB7.bin").write_bytes(b"extra")
        with self.assertRaises(HubError): self.hub.build(self.source, expected)
        (self.source / "2_aB7.bin").unlink()
        with self.assertRaises(HubError): self.hub.build(self.source, {"1_aB7.bin": "0" * 64})
        self.assertEqual(self.hub.verify(expected)["generation"], manifest["generation"])
        (self.hub.generations / manifest["generation"] / "extra.jsonl").write_text("{}")
        with self.assertRaises(HubError): self.hub.verify(expected)

    def test_source_and_generation_symlinks_refused(self):
        target = self.root / "original"
        target.write_bytes(b"original")
        (self.source / "1_aB7.bin").symlink_to(target)
        with self.assertRaises(HubError): self.hub.build(self.source, {"1_aB7.bin": hashlib.sha256(b"original").hexdigest()})
        (self.source / "1_aB7.bin").unlink()
        expected, manifest = self.build({"1_aB7.bin": b"one"})
        folder = self.hub.generations / manifest["generation"]
        moved = self.root / "moved"
        folder.rename(moved)
        folder.symlink_to(moved)
        with self.assertRaises(HubError): self.hub.verify(expected)

    def test_fifo_source_is_rejected_without_blocking(self):
        os.mkfifo(self.source / "1_aB7.bin")
        with self.assertRaises(HubError): self.hub.build(self.source, {"1_aB7.bin": "0" * 64})

    def test_failed_publication_keeps_previous_generation_active(self):
        expected, first = self.build({"1_aB7.bin": b"one"})
        with patch("Transformer_Core.Hot_Hub.hub.os.replace", side_effect=OSError("disk interrupted")):
            with self.assertRaises(OSError): self.hub.build(self.source, expected)
        self.assertEqual(self.hub.verify(expected)["generation"], first["generation"])

    def test_failed_publication_retains_sources_and_retry_repairs_corruption(self):
        source = self.root / "opaque.anything"
        source.write_bytes(b"hello\x00\xff")
        pacote = Pacote(self.root / "pipeline")
        pacote.open()
        pacote.add([source])
        with patch("Transformer_Core.Hot_Hub.hub._line", side_effect=OSError("disk interrupted")):
            with self.assertRaises(OSError): pacote.close()
        self.assertTrue((pacote.runtime / "BN1_1/BBN1_1").exists())
        self.assertEqual(len(list(pacote.files.glob("*/*"))), 1)
        self.assertEqual(pacote.close(), 1)
        hub = HotHub(pacote.runtime)
        first = json.loads(hub.manifest.read_text())
        name = next(iter(first["sources"]))
        path = hub.generations / first["generation"] / first["records"][name]["file"]
        path.write_text("broken")
        with self.assertRaises(HubError): hub.verify(first["sources"])
        self.assertEqual(pacote.close(), 1)
        self.assertEqual(hub.reconstruct(name, first["sources"]), source.read_bytes())
        self.assertNotEqual(hub.verify(first["sources"])["generation"], first["generation"])
        self.assertFalse((pacote.runtime / "BN1_1/BBN1_1").exists())
        stable = hub.manifest.read_bytes()
        pacote.close()
        self.assertEqual(hub.manifest.read_bytes(), stable)

    def test_legacy_cycle_and_hub_are_preserved(self):
        runtime = self.root / "legacy"
        runtime.mkdir()
        state = runtime / "cycle.json"
        original = b'{"layout_version":2,"status":"HUB_READY","items":[]}'
        state.write_bytes(original)
        with self.assertRaisesRegex(CacheError, "layout antigo"): Pacote(runtime).close()
        self.assertEqual(state.read_bytes(), original)
        self.hub.root.mkdir(parents=True)
        old = self.hub.root / "data"
        old.mkdir()
        (old / "original").write_bytes(b"preserved")
        with self.assertRaises(HubError): self.hub.build(self.source, {})
        self.assertEqual((old / "original").read_bytes(), b"preserved")

    def test_pure_chunker_does_not_decode_or_accept_mutable_vectors(self):
        with self.assertRaises(TypeError): list(byte_chunks(bytearray(b"x"), "1_aB7/bin", "id"))
        chunks = list(byte_chunks(b"\xff\x00", "1_aB7/bin", "id"))
        self.assertEqual(chunks[0]["values"][:2], [255, 0])
        self.assertEqual(chunks[0]["valid_length"], 2)


if __name__ == "__main__":
    unittest.main()
