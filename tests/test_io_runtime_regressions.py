"""I/O failures must preserve canonical ingestion checkpoints and recover indexes."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mimir import Memory
from mimir.io import Channels, IOError


def folder(identity, parent=None):
    return {"id": identity, "mimeType": "application/vnd.google-apps.folder", "parents": [parent] if parent else ["root"]}


class InputCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = Memory(self.root / "memory")
        self.io = Channels(self.memory)
        self.io.configure(folder("home"), folder("incoming", "home"), folder("outgoing", "home"), "test@example.com")
        self.data = b"Alice works at Acme.\n"
        self.metadata = {"id": "source-1", "name": "notes.txt", "mimeType": "text/plain", "parents": ["incoming"],
            "version": "1", "modifiedTime": "2026-10-02T00:00:00Z", "size": str(len(self.data)),
            "sha256Checksum": hashlib.sha256(self.data).hexdigest()}

    def ingest(self, **settings):
        return self.io.ingest_file(self.io.stage(self.data), self.metadata, self.metadata, **settings)

    def receipt(self):
        paths = list(self.io.inputs.glob("*/receipt.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    def test_publication_failure_keeps_successful_ingestion_and_retries_without_reinference(self):
        with patch.object(self.io, "_enqueue_unlocked", side_effect=IOError("outbox temporarily unavailable")):
            with self.assertRaises(IOError):
                self.ingest()
        receipt = self.receipt()
        self.assertEqual(receipt["status"], "COMPLETE")
        self.assertEqual(self.memory.verify()["generation"], receipt["semantic_generation"])
        with patch.object(self.memory, "ingest", side_effect=AssertionError("must not repeat inference")):
            retried = self.io.processed(self.metadata)
        self.assertEqual(retried, receipt)
        self.assertEqual(len(self.io.pending()), 1)

    def test_failed_reprocessing_preserves_previous_complete_receipt(self):
        completed = self.ingest()
        with patch.object(self.memory, "ingest", side_effect=IOError("new extractor unavailable")):
            with self.assertRaises(IOError):
                self.ingest(force=True)
        self.assertEqual(self.receipt(), completed)
        self.assertEqual(self.io.processed(self.metadata), completed)
        self.assertTrue(self.memory.verify()["verified"])

    def test_repeated_ingestion_repairs_missing_revision_index(self):
        completed = self.ingest()
        for pointer in (self.io.root / "seen").iterdir():
            pointer.unlink()
        self.assertIsNone(self.io.processed(self.metadata))
        with patch.object(self.memory, "ingest", side_effect=AssertionError("must not repeat inference")):
            self.assertEqual(self.ingest(), completed)
        self.assertEqual(self.io.processed(self.metadata), completed)

    def test_incomplete_folder_metadata_is_a_domain_error(self):
        for root in ({}, [], {"id": "home", "mimeType": "application/vnd.google-apps.folder", "parents": "root"}):
            with self.subTest(root=root), self.assertRaises(IOError):
                self.io.configure(root, folder("incoming", "home"), folder("outgoing", "home"), "test@example.com")


if __name__ == "__main__":
    unittest.main()
