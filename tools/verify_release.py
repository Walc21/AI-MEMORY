"""Exercise all four audited contracts against the installed wheel, offline."""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sysconfig
import tempfile

import mimir
from mimir import Memory
from mimir.io import Channels, IOError
from mimir.io.drive import DriveSync
from mimir_version import __version__
from BN1_1.Pacote.cache import Pacote


def folder(identity, parent="root"):
    return {"id": identity, "name": identity, "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent], "trashed": False, "ownedByMe": True, "shared": False}


class OfflineDrive:
    def __init__(self):
        self.data = b"Eve works at Cedar.\n"
        self.rows = {r["id"]: r for r in (folder("home"), folder("incoming", "home"), folder("outgoing", "home"))}
        self.rows["input-1"] = {"id": "input-1", "name": "notes.txt", "mimeType": "text/plain", "parents": ["incoming"],
            "version": "1", "modifiedTime": "2026-10-03T00:00:00Z", "size": str(len(self.data)),
            "sha256Checksum": hashlib.sha256(self.data).hexdigest(), "trashed": False}
        self.downloads = self.uploads = 0

    def profile(self):
        return {"emailAddress": "acceptance@example.com"}

    def metadata(self, identity):
        return deepcopy(self.rows[identity])

    def files(self, identity, maximum):
        return [self.metadata("input-1")]

    def download(self, meta, maximum):
        self.downloads += 1
        self.rows["outgoing"]["shared"] = True
        return self.data

    def reserve(self):
        return "reserved-output"

    def upload(self, job, config, path):
        if job["remote_reserved_id"] not in self.rows:
            self.uploads += 1
            self.rows[job["remote_reserved_id"]] = {"id": job["remote_reserved_id"], "name": job["file_name"],
                "parents": ["outgoing"], "size": str(path.stat().st_size),
                "sha256Checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
                "ownedByMe": True, "shared": False, "trashed": False}
        return self.metadata(job["remote_reserved_id"])


def absent(memory, question, **settings):
    result = memory.query(question, **settings)
    assert result["abstained"], (question, result["answer"])
    assert result["claims"] == result["answer_evidence"] == []
    assert result["context"]["untrusted_evidence"] == []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    assert __version__ == args.expected_version
    assert Path(mimir.__file__).resolve().parent.parent == Path(sysconfig.get_path("purelib")).resolve(), "Run against the installed wheel, outside the checkout"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        memory = Memory(root / "memory")
        memory.episode("Maria Clara works at North South.\nJoão trabalhava na Acme em 2020.")
        absent(memory, "Where does Clara Maria work?")
        absent(memory, "Does Maria Clara work at South North?")
        assert not memory.query("Where does Maria Clara work?")["abstained"]
        absent(memory, "Onde João trabalha?", valid_at="2026")
        assert not memory.query("Onde João trabalhava em 2020?")["abstained"]
        source = root / "people.json"
        source.write_text(json.dumps({"a": {"name": "Alice", "salary": 100}, "b": {"name": "Bob", "password": "SECRET"}}))
        runtime = root / "runtime"
        package = Pacote(runtime)
        package.open()
        package.add([source])
        memory.ingest(runtime)
        absent(memory, "What is the password of Alice?")
        assert "SECRET" in memory.query("What is the password of Bob?")["answer"]
        io = Channels(memory)
        io.configure(folder("home"), folder("incoming", "home"), folder("outgoing", "home"), "acceptance@example.com")
        api = OfflineDrive()
        first = DriveSync(io, api).sync()
        assert not first["complete"] and api.uploads == 0
        assert io.status()["input_cycle"]["phase"] == "AWAITING_DELIVERY"
        try:
            memory.episode("Other input works at Beta.")
        except IOError:
            pass
        else:
            raise AssertionError("A second Input passed a pending cycle")
        api.rows["outgoing"]["shared"] = False
        second = DriveSync(Channels(memory), api).sync()
        assert second["complete"], second
        assert (api.downloads, api.uploads) == (1, 1)
        assert io.status()["input_cycle"]["phase"] == "CLOSED" and not io.pending()
        job_folder = io.inputs / first["accepted"][0]["id"]
        assert not (job_folder / "source").exists() and not (job_folder / "runtime").exists()
        assert memory.verify()["verified"]
        assert "Cedar" in memory.query("Where does Eve work?")["answer"]
    print(json.dumps({"version": __version__, "installed_wheel": True,
                      "identity": "passed", "field_ownership": "passed", "temporal_coverage": "passed",
                      "cycle_and_privacy": "passed", "drive_provider": "offline test double"}, indent=2))


if __name__ == "__main__":
    main()
