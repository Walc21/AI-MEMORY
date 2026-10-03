"""Acceptance tests for durable Drive channels without optional packages."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from BN1_2.buffer import write_atomic
from mimir import Memory
from mimir.io import Channels, IOError, IOLimits
from mimir.io.drive import DriveError, DriveSync
from mimir.io.model import export_spec, local_name, revision
from Transformer_Core.structural.model import fingerprint


def folder(identity, parent=None):
    return {"id": identity, "name": identity, "mimeType": "application/vnd.google-apps.folder", "parents": [parent] if parent else ["root"], "trashed": False, "ownedByMe": True, "shared": False}


def validation():
    from mimir.io.model import account_fingerprint
    return {"account_fingerprint": account_fingerprint("test@example.com"), "root": folder("home"),
            "incoming": folder("incoming", "home"), "outgoing": folder("outgoing", "home")}


def metadata(data=b"Alice works at Acme.\n", **values):
    return {"id": "source-1", "name": "notes.txt", "mimeType": "text/plain", "parents": ["incoming"],
            "version": "1", "modifiedTime": "2026-10-02T00:00:00Z", "size": str(len(data)),
            "md5Checksum": hashlib.md5(data).hexdigest(), "sha256Checksum": hashlib.sha256(data).hexdigest(),
            "webViewLink": "https://drive.google.com/file/d/source-1/view", "trashed": False, **values}


class ChannelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = Memory(self.root / "memory")
        self.io = Channels(self.memory)
        self.io.configure(folder("home"), folder("incoming", "home"), folder("outgoing", "home"), "test@example.com")

    def ingest(self, data=b"Alice works at Acme.\n", **values):
        meta = metadata(data, **values)
        return self.io.ingest_file(self.io.stage(data), meta, deepcopy(meta))

    def remote(self, job, **changes):
        return {"id": "remote-1", "name": job["file_name"], "parents": ["outgoing"], "size": str(job["size"]), "md5Checksum": job["md5"], "trashed": False, "ownedByMe": True, "shared": False, "folder_validation": validation(), **changes}

    def test_ingestion_links_drive_revision_to_explanation_and_is_idempotent(self):
        receipt = self.ingest()
        result = self.memory.query("Where does Alice work?")
        evidence = self.memory.explain(result["claims"][0]["assertion_id"])["evidence"][0]
        self.assertEqual(evidence["external_source"], receipt["source"])
        self.assertEqual(evidence["external_source"]["file_id"], "source-1")
        self.assertEqual(self.ingest(), receipt)
        self.assertEqual(self.io.processed(metadata()), receipt)
        self.assertEqual(len(self.io.pending()), 1)
        self.assertTrue(self.memory.verify()["verified"])

    def test_remote_revision_adds_history_without_erasing_source(self):
        first = self.ingest()
        job = self.io.pending()[0]
        self.io.acknowledge(job["id"], self.remote(job))
        second = self.ingest(b"Alice works at Beta.\n", version="2")
        self.assertNotEqual(first["semantic_generation"], second["semantic_generation"])
        self.assertEqual(len(self.memory.inspect("assertions")), 2)
        self.assertTrue(self.memory.verify()["verified"])

    def test_file_revised_during_download_is_not_ingested(self):
        before, after = metadata(), metadata(version="2")
        with self.assertRaisesRegex(IOError, "mudou"):
            self.io.ingest_file(self.io.stage(b"Alice works at Acme.\n"), before, after)
        self.assertEqual(self.io.status()["inputs"], {})

    def test_tampered_bytes_wrong_folder_and_unsafe_name_rejected(self):
        for data, changes in ((b"tampered", {}), (b"Alice works at Acme.\n", {"parents": ["elsewhere"]}), (b"Alice works at Acme.\n", {"name": "../escape.txt"})):
            with self.assertRaises(IOError):
                self.io.ingest_file(self.io.stage(data), metadata(**changes), metadata(**changes))

    def test_paths_outside_staging_and_symlinks_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_bytes(b"Alice works at Acme.\n")
        for path in (outside, self.io.staging / "alias", self.io.staging / ".." / ".." / ".." / ".." / "outside.txt"):
            if path == self.io.staging / "alias":
                path.symlink_to(outside)
            with self.assertRaises(IOError):
                self.io.ingest_file(path, metadata(), metadata())

    def test_account_and_folders_cannot_be_silently_switched(self):
        with self.assertRaisesRegex(IOError, "Namespace"):
            self.io.configure(folder("home"), folder("incoming", "home"), folder("outgoing", "home"), "other@example.com")
        with self.assertRaises(IOError):
            self.io.configure(folder("home"), folder("incoming", "wrong"), folder("outgoing", "home"), "test@example.com")
        self.assertNotIn("test@example.com", self.io.config_file.read_text())

    def test_ingestion_can_resume_after_failed_pipeline(self):
        with patch.object(self.memory, "ingest", side_effect=IOError("interrupted")):
            with self.assertRaises(IOError):
                self.ingest()
        self.assertEqual(self.io.status()["inputs"], {"FAILED": 1})
        self.assertEqual(self.ingest()["status"], "COMPLETE")

    def test_output_query_uses_existing_memory_and_queues_cited_response(self):
        self.ingest()
        response = self.io.publish_query("Where does Alice work?")
        job = response["delivery"]
        self.assertEqual(job["semantic_generation"], response["result"]["generation"])
        message = json.loads((self.io.outbox / job["id"] / "payload.json").read_text())
        self.assertEqual(message["payload"], response["result"])
        self.assertTrue(message["payload"]["claims"])
        self.assertEqual(job["status"], "PENDING")

    def test_remote_name_cannot_collide_with_local_receipt_or_runtime(self):
        self.assertEqual(self.ingest(name="receipt.json")["status"], "COMPLETE")
        job = self.io.pending()[0]
        self.io.acknowledge(job["id"], self.remote(job))
        self.assertEqual(self.ingest(name="runtime", id="source-2")["status"], "COMPLETE")

    def test_payload_published_without_receipt_recovers_after_restart(self):
        job = self.io.enqueue({"answer": "hello"})
        (self.io.outbox / job["id"] / "receipt.json").unlink()
        recovered = Channels(self.memory).pending()[0]
        self.assertEqual(recovered["id"], job["id"])
        self.assertEqual(recovered["sha256"], job["sha256"])
        empty = self.io.outbox / ("a" * 64)
        empty.mkdir()
        self.assertEqual(len(self.io.pending()), 1)

    def test_outbox_deduplicates_and_checks_remote_receipts(self):
        job = self.io.enqueue({"answer": "hello"})
        self.assertEqual(job, self.io.enqueue({"answer": "hello"}))
        for change in ({"parents": ["wrong"]}, {"md5Checksum": "bad"}, {"size": "0"}, {"name": "wrong"}):
            with self.assertRaises(IOError):
                self.io.acknowledge(job["id"], self.remote(job, **change))
        delivered = self.io.acknowledge(job["id"], self.remote(job))
        self.assertEqual(delivered["status"], "DELIVERED")
        self.assertEqual(self.io.acknowledge(job["id"], self.remote(job)), delivered)
        self.assertEqual(self.io.pending(), [])
        with self.assertRaises(IOError):
            self.io.acknowledge(job["id"], self.remote(job, id="another"))

    def test_reserved_id_survives_restart_and_cannot_change(self):
        job = self.io.enqueue({"answer": "hello"})
        self.io.reserve(job["id"], "reserved")
        self.assertEqual(Channels(self.memory).pending()[0]["remote_reserved_id"], "reserved")
        with self.assertRaises(IOError):
            self.io.reserve(job["id"], "different")
        with self.assertRaises(IOError):
            self.io.acknowledge(job["id"], self.remote(job))

    def test_corrupt_output_and_forged_input_receipt_fail_closed(self):
        receipt = self.ingest()
        path = self.io.inputs / receipt["id"] / "receipt.json"
        receipt["source"]["file_id"] = "another"
        write_atomic(path, receipt)
        with self.assertRaises(IOError):
            self.io.processed(metadata())
        job = self.io.pending()[0]
        Path(job["local_path"]).write_bytes(b"{}")
        with self.assertRaises(IOError):
            self.io.pending()

    def test_native_exports_have_real_extensions_not_original_checksums(self):
        data = metadata(mimeType="application/vnd.google-apps.document", name="Document")
        self.assertEqual(local_name(data), "Document.docx")
        self.assertIn("wordprocessingml", export_spec(data)[0])
        with self.assertRaises(IOError):
            export_spec({"mimeType": "application/vnd.google-apps.shortcut"})

    def test_limits_prevent_silent_truncation_and_export_omits_credentials(self):
        small = Channels(self.memory, IOLimits(max_file_bytes=3, max_output_bytes=30))
        with self.assertRaises(IOError):
            small.stage(b"four")
        with self.assertRaises(IOError):
            small.enqueue({"text": "x" * 100})
        self.ingest()
        exported = self.io.export_memory()
        message = json.loads((self.io.outbox / exported["id"] / "payload.json").read_text())
        self.assertEqual(message["payload"]["manifest"]["generation"], exported["semantic_generation"])
        self.assertNotIn("credentials", message["payload"])


class FakeDrive:
    def __init__(self):
        self.rows = {row["id"]: row for row in (folder("home"), folder("incoming", "home"), folder("outgoing", "home"), metadata())}
        self.downloads = self.uploads = 0
        self.reservations = 0
        self.lose_response = False

    def profile(self):
        return {"emailAddress": "test@example.com"}

    def metadata(self, identity):
        return deepcopy(self.rows[identity])

    def files(self, identity, maximum):
        return [self.metadata("source-1")]

    def download(self, meta, maximum):
        self.downloads += 1
        return b"Alice works at Acme.\n"

    def reserve(self):
        self.reservations += 1
        return f"reserved-{self.reservations}"

    def upload(self, job, config, path):
        identity = job["remote_reserved_id"]
        if identity not in self.rows:
            self.uploads += 1
            self.rows[identity] = {"id": identity, "name": job["file_name"], "parents": ["outgoing"], "size": str(job["size"]), "sha256Checksum": job["sha256"], "trashed": False, "ownedByMe": True, "shared": False}
        if self.lose_response:
            self.lose_response = False
            raise IOError("response lost")
        return self.metadata(identity)


class SyncTests(unittest.TestCase):
    setUp = ChannelTests.setUp
    def test_complete_sync_and_retry_do_not_redownload_or_duplicate_uploads(self):
        api = FakeDrive()
        sync = DriveSync(self.io, api)
        api.lose_response = True
        self.assertFalse(sync.sync()["complete"])
        self.assertEqual(self.io.pending()[0]["remote_reserved_id"], "reserved-1")
        retried = sync.sync()
        self.assertTrue(retried["complete"])
        self.assertEqual((api.downloads, api.uploads), (1, 1))
        self.assertEqual(retried["skipped"][0]["reason"], "unchanged_revision")
        self.assertEqual(self.io.pending(), [])

    def test_sync_rejects_another_account_or_moved_folder_before_any_transfer(self):
        api = FakeDrive()
        with patch.object(api, "profile", return_value={"emailAddress": "other@example.com"}):
            with self.assertRaises(IOError):
                DriveSync(self.io, api).sync()
        api.rows["outgoing"]["parents"] = ["another"]
        with self.assertRaises(IOError):
            DriveSync(self.io, api).sync()
        self.assertEqual((api.downloads, api.uploads), (0, 0))


if __name__ == "__main__":
    unittest.main()
