"""Admission, delivery, privacy and crash recovery of the complete Input cycle."""

from copy import deepcopy
import json
import multiprocessing
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from BN1_2.buffer import write_atomic

from mimir import Memory
from mimir.io import Channels, IOError
from mimir.io.drive import DriveSync
from test_io_core import FakeDrive, folder, metadata, validation


def remote(job, identity="remote-1"):
    return {"id": identity, "name": job["file_name"], "parents": ["outgoing"],
            "size": str(job["size"]), "sha256Checksum": job["sha256"],
            "ownedByMe": True, "shared": False, "trashed": False, "folder_validation": validation()}


def attempt_input(root, result):
    channels = Channels(Memory(Path(root)))
    try:
        data = b"Bob works at Beta.\n"
        meta = metadata(data, id="source-2")
        channels.ingest_file(channels.stage(data), meta, meta)
    except IOError:
        result.put("blocked")
    else:
        result.put("accepted")


class InputCycleContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = Memory(self.root / "memory")
        self.io = Channels(self.memory)
        self.io.configure(folder("home"), folder("incoming", "home"), folder("outgoing", "home"), "test@example.com")

    def ingest(self, data=b"Alice works at Acme.\n", **values):
        meta = metadata(data, **values)
        return self.io.ingest_file(self.io.stage(data), meta, deepcopy(meta))

    def acknowledge(self):
        job = self.io.pending()[0]
        return self.io.acknowledge(job["id"], remote(job))

    def assert_clean(self, receipt):
        folder_path = self.io.inputs / receipt["id"]
        self.assertFalse((folder_path / "runtime").exists())
        self.assertFalse((folder_path / "source").exists())
        self.assertEqual(json.loads((folder_path / "cycle.json").read_text())["phase"], "CLOSED")
        self.assertTrue(self.memory.verify()["verified"])
        self.assertFalse(self.memory.query("Where does Alice work?")["abstained"])

    def test_delivered_input_cleans_intermediates_and_keeps_canon(self):
        receipt = self.ingest()
        self.assertEqual(self.io.status()["input_cycle"]["phase"], "AWAITING_DELIVERY")
        self.acknowledge()
        self.assertEqual(self.io.status()["input_cycle"]["phase"], "CLOSED")
        self.assert_clean(receipt)
        self.assertEqual(list(self.io.staging.iterdir()), [])
        self.assertEqual(self.io.processed(metadata()), receipt)

    def test_namespace_blocks_other_process_and_direct_memory_until_cleanup(self):
        first = self.ingest()
        ctx = multiprocessing.get_context("spawn")
        result = ctx.Queue()
        child = ctx.Process(target=attempt_input, args=(str(self.memory.store.base), result))
        child.start()
        child.join(20)
        self.assertFalse(child.is_alive())
        self.assertEqual(child.exitcode, 0)
        self.assertEqual(result.get(timeout=2), "blocked")
        with self.assertRaisesRegex(IOError, "Input ocupado"):
            self.memory.episode("Carol works at Cedar.")
        self.acknowledge()
        self.assert_clean(first)
        second = self.ingest(b"Bob works at Beta.\n", id="source-2")
        self.assertEqual(second["status"], "COMPLETE")

    def test_cleanup_interruption_resumes_without_inference_or_upload(self):
        api = FakeDrive()
        original = shutil.rmtree
        def interrupted(path, *args, **kwargs):
            if Path(path).is_relative_to(self.io.inputs):
                raise OSError("cleanup interrupted")
            return original(path, *args, **kwargs)
        with patch("mimir.io.cycle.shutil.rmtree", side_effect=interrupted):
            first = DriveSync(self.io, api).sync()
        self.assertFalse(first["complete"])
        self.assertEqual(self.io.status()["input_cycle"]["phase"], "CLEANING")
        with patch.object(self.memory, "ingest", side_effect=AssertionError("no re-inference")):
            retry = DriveSync(Channels(self.memory), api).sync()
        self.assertTrue(retry["complete"], retry)
        self.assertEqual((api.downloads, api.uploads), (1, 1))
        self.assert_clean(first["accepted"][0])

    def test_admission_interruption_before_first_receipt_can_retry_only_owner(self):
        data = b"Alice works at Acme.\n"
        meta = metadata(data)
        original = write_atomic
        def interrupted(path, value):
            if Path(path).name == "receipt.json" and value.get("status") == "PROCESSING":
                raise OSError("receipt publication interrupted")
            return original(path, value)
        with patch("mimir.io.channels.write_atomic", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.ingest(data)
        self.assertEqual(self.io.status()["input_cycle"]["phase"], "PROCESSING")
        receipt_path = self.io.inputs / self.io.cycle.load()["input_job_id"] / "receipt.json"
        self.assertFalse(receipt_path.exists())
        other = metadata(data, id="source-2")
        with self.assertRaisesRegex(IOError, "Input ocupado"):
            self.io.ingest_file(self.io.stage(data), other, other)
        recovered = Channels(self.memory)
        receipt = recovered.ingest_file(recovered.stage(data), meta, meta)
        self.acknowledge()
        self.assert_clean(receipt)

    def test_force_after_closed_cycle_recreates_only_ephemeral_input(self):
        first = self.ingest()
        self.acknowledge()
        self.assert_clean(first)
        data = b"Alice works at Acme.\n"
        meta = metadata(data)
        second = self.io.ingest_file(self.io.stage(data), meta, meta, force=True)
        self.assertNotEqual(first["semantic_generation"], second["semantic_generation"])
        self.acknowledge()
        self.assert_clean(second)

    def test_delivery_failure_blocks_next_download_and_recovers(self):
        io = self.io

        class TwoInputs(FakeDrive):
            def __init__(self):
                super().__init__()
                self.rows["source-2"] = metadata(id="source-2")

            def files(self, identity, maximum):
                return [self.metadata("source-1"), self.metadata("source-2")]

            def download(self, meta, maximum):
                if meta["id"] == "source-2":
                    self_test.assertEqual(io.status()["input_cycle"]["phase"], "DOWNLOADING")
                    first_folder = next(p for p in io.inputs.iterdir() if (p / "receipt.json").exists())
                    self_test.assertFalse((first_folder / "runtime").exists())
                return super().download(meta, maximum)

        self_test = self
        api = TwoInputs()
        api.lose_response = True
        first = DriveSync(io, api).sync()
        self.assertFalse(first["complete"])
        self.assertEqual(api.downloads, 1)
        retry = DriveSync(Channels(self.memory), api).sync()
        self.assertTrue(retry["complete"], retry)
        self.assertEqual((api.downloads, api.uploads), (2, 2))
        for path in io.inputs.glob("*/receipt.json"):
            self.assert_clean(json.loads(path.read_text()))

    def test_changed_privacy_during_download_prevents_upload_and_next_input(self):
        class Changed(FakeDrive):
            def download(self, meta, maximum):
                self.rows["outgoing"]["shared"] = True
                return super().download(meta, maximum)
        api = Changed()
        result = DriveSync(self.io, api).sync()
        self.assertFalse(result["complete"])
        self.assertEqual(api.uploads, 0)
        self.assertEqual(self.io.pending()[0]["status"], "PENDING")
        self.assertEqual(self.io.status()["input_cycle"]["phase"], "AWAITING_DELIVERY")
        api.rows["outgoing"]["shared"] = False
        self.assertTrue(DriveSync(Channels(self.memory), api).sync()["complete"])
        self.assertEqual(api.downloads, 1)

    def test_post_upload_privacy_change_does_not_acknowledge_or_clean(self):
        class Changed(FakeDrive):
            def upload(self, job, config, path):
                remote_value = super().upload(job, config, path)
                self.rows["outgoing"]["shared"] = True
                return remote_value
        api = Changed()
        result = DriveSync(self.io, api).sync()
        self.assertFalse(result["complete"])
        self.assertEqual(self.io.pending()[0]["status"], "PENDING")
        self.assertTrue((self.io.inputs / result["accepted"][0]["id"] / "runtime").exists())

    def test_missing_privacy_flags_are_rejected_for_config_and_delivery(self):
        for key in ("ownedByMe", "shared", "trashed"):
            incomplete = folder("home")
            del incomplete[key]
            with self.subTest(key=key), self.assertRaises(IOError):
                self.io.configure(incomplete, folder("incoming", "home"), folder("outgoing", "home"), "test@example.com")
        job = self.io.enqueue({"answer": "x"})
        for key in ("ownedByMe", "shared", "folder_validation"):
            incomplete = remote(job)
            del incomplete[key]
            with self.subTest(key=key), self.assertRaises(IOError):
                self.io.acknowledge(job["id"], incomplete)
        self.assertEqual(self.io.pending()[0]["status"], "PENDING")
        malformed = validation()
        malformed["incoming"]["parents"] = "home"
        with self.assertRaises(IOError):
            self.io.acknowledge(job["id"], {**remote(job), "folder_validation": malformed})

    def test_transfer_lock_rejects_concurrent_sync(self):
        with self.io.transfer_locked():
            with self.assertRaisesRegex(IOError, "Outro processo"):
                DriveSync(Channels(self.memory), FakeDrive()).sync()

    def test_cleanup_refuses_symlink_and_recovers_after_repair(self):
        receipt = self.ingest()
        source = self.io.inputs / receipt["id"] / "source"
        saved = source.with_name("source-original")
        source.rename(saved)
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep")
        source.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(IOError, "symlink"):
            self.acknowledge()
        self.assertEqual(sentinel.read_text(), "keep")
        source.unlink()
        saved.rename(source)
        self.io.recover_cycle()
        self.assert_clean(receipt)

    def test_retained_v050_cycle_is_adopted_and_cleaned(self):
        receipt = self.ingest()
        # v0.5.0 had the same canonical receipt/outbox, without cycle journals.
        self.io.cycle.path.unlink()
        (self.io.inputs / receipt["id"] / "cycle.json").unlink()
        self.io.recover_cycle()
        self.acknowledge()
        self.assert_clean(receipt)

    def test_abandoned_download_can_be_canceled_but_committed_input_cannot(self):
        state = self.io.begin_input(metadata())["input_cycle"]
        other = metadata(id="source-2")
        with self.assertRaises(IOError):
            self.io.begin_input(other)
        self.io.cancel_download(state["input_key"])
        self.assertEqual(self.io.status()["input_cycle"]["phase"], "CLOSED")
        self.ingest()
        with self.assertRaises(IOError):
            self.io.cancel_download(state["input_key"])

    def test_legacy_delivered_receipt_requires_fresh_privacy_readback_before_cleanup(self):
        receipt = self.ingest()
        job = self.io.pending()[0]
        legacy = {**job, "status": "DELIVERED", "remote": remote(job, "already-uploaded")}
        legacy.pop("local_path")
        legacy.pop("privacy_verified")
        write_atomic(self.io.outbox / job["id"] / "receipt.json", legacy)
        self.io.recover_cycle()
        self.assertTrue((self.io.inputs / receipt["id"] / "runtime").exists())
        api = FakeDrive()
        api.rows["already-uploaded"] = remote(job, "already-uploaded")
        result = DriveSync(self.io, api).sync()
        self.assertTrue(result["complete"], result)
        self.assertEqual((api.downloads, api.uploads), (0, 0))
        self.assert_clean(receipt)
