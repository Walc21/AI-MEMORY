"""One persistent admitted Input per memory namespace until delivery/cleanup.

All methods run under Channels.locked. The namespace journal is authoritative;
per-job journals preserve completed cycles after the next Input is admitted.
"""

from pathlib import Path
import re
import shutil
from BN1_2.buffer import write_atomic
from Transformer_Core.semantic.model import now
from Transformer_Core.semantic.storage import read_json
from .model import IOError


PHASES = {"DOWNLOADING", "PROCESSING", "AWAITING_DELIVERY", "CLEANING", "CLOSED"}


class InputCycle:
    def __init__(self, channels):
        self.channels = channels
        self.path = channels.root / "input-cycle.json"

    def load(self):
        if not self.path.exists() and not self.path.is_symlink():
            return None
        value = read_json(self.path)
        expected = {"schema", "input_key", "input_job_id", "phase", "output_ids", "staging", "started_at", "closed_at"}
        if (not isinstance(value, dict) or set(value) != expected or value["schema"] != "mimir.input-cycle.v1"
                or value["phase"] not in PHASES or not re.fullmatch(r"[0-9a-f]{64}", str(value["input_key"]))
                or value["input_job_id"] is not None and not re.fullmatch(r"[0-9a-f]{64}", str(value["input_job_id"]))
                or not isinstance(value["output_ids"], list) or any(not re.fullmatch(r"[0-9a-f]{64}", str(x)) for x in value["output_ids"])
                or not isinstance(value["staging"], list) or any(not re.fullmatch(r"incoming-[A-Za-z0-9_-]+", str(x)) for x in value["staging"])):
            raise IOError("Journal do ciclo de Input inválido.")
        return value

    def write(self, value):
        write_atomic(self.path, value)
        if value["input_job_id"]:
            write_atomic(self.channels.inputs / value["input_job_id"] / "cycle.json", value)
        return value

    def claim(self, key, job_id=None, staged=None):
        current = self.load()
        if current and current["phase"] != "CLOSED":
            if current["input_key"] != key or job_id and current["input_job_id"] not in (None, job_id):
                raise IOError("Input ocupado: conclua entrega e limpeza do ciclo ativo antes de aceitar outra entrada.")
        else:
            current = {"schema": "mimir.input-cycle.v1", "input_key": key, "input_job_id": None,
                       "phase": "DOWNLOADING", "output_ids": [], "staging": [], "started_at": now(), "closed_at": None}
        if job_id:
            current["input_job_id"] = job_id
            current["phase"] = "PROCESSING"
        if staged and staged.name not in current["staging"]:
            current["staging"].append(staged.name)
        return self.write(current)

    def release_download(self, key):
        current = self.load()
        if current and current["input_key"] == key and current["input_job_id"] is None:
            current.update(phase="CLOSED", closed_at=now())
            self.write(current)

    def link(self, receipt, output):
        current = self.load()
        if current and current["phase"] != "CLOSED" and current["input_job_id"] == receipt["id"]:
            if output["id"] not in current["output_ids"]:
                current["output_ids"].append(output["id"])
            current["phase"] = "AWAITING_DELIVERY"
            self.write(current)

    def finish(self):
        current = self.load()
        if not current or current["phase"] == "CLOSED" or not current["input_job_id"] or not current["output_ids"]:
            return False
        job_id = current["input_job_id"]
        receipt = read_json(self.channels.inputs / job_id / "receipt.json")
        if receipt.get("status") != "COMPLETE":
            return False
        self.channels._verify_input(receipt, job_id)
        for identity in current["output_ids"]:
            output = self.channels._load_output(self.channels.outbox / identity)
            if output is None or output["status"] != "DELIVERED" or output.get("privacy_verified") is not True:
                return False
        # Persist CLEANING before deletion. A crash here retries deletion;
        # ingestion and the verified remote upload are never repeated.
        current["phase"] = "CLEANING"
        self.write(current)
        folder = self.channels.inputs / job_id
        for name in ("runtime", "source"):
            path = folder / name
            if path.is_symlink():
                raise IOError("Limpeza recusada: intermediário é um symlink.")
            if path.exists():
                shutil.rmtree(path)
        for name in current["staging"]:
            path = self.channels.staging / name
            if path.is_symlink():
                raise IOError("Limpeza recusada: staging é um symlink.")
            path.unlink(missing_ok=True)
        current.update(phase="CLOSED", closed_at=now())
        self.write(current)
        return True

    def guard(self, runtime):
        current = self.load()
        if current and current["phase"] != "CLOSED":
            allowed = self.channels.inputs / str(current["input_job_id"]) / "runtime"
            if Path(runtime).absolute() != allowed.absolute():
                raise IOError("Input ocupado: outra ingestão aguarda entrega e limpeza no namespace.")
