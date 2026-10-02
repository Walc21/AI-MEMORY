"""Crash-recoverable inbox/outbox beside the canonical memory namespace."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from BN1_1.Pacote.cache import Pacote
from BN1_2.buffer import ensure_directories, write_atomic
from Transformer_Core.Hot_Hub.hub import _open_regular
from Transformer_Core.semantic.model import content_id, now
from Transformer_Core.semantic.storage import read_json
from Transformer_Core.structural.model import canonical, fingerprint
from .model import IOError, IOLimits, account_fingerprint, export_spec, local_name, remote_id, revision, verify_bytes


class Channels:
    def __init__(self, memory, limits=None):
        self.memory, self.store = memory, memory.store
        self.limits = limits or IOLimits()
        self.root = self.store.root / "IO"
        self.staging = self.root / "staging"
        self.inputs = self.store.root / "Input_Storage/jobs"
        self.outbox = self.store.root / "Output_Storage/outbox"
        self.config_file = self.root / "drive.json"

    @contextmanager
    def locked(self, write=False):
        self.store._layout()
        self.store.authorize(write)
        for directory in (self.staging, self.inputs, self.outbox):
            ensure_directories(self.store.base, directory)
        descriptor = os.open(self.root / "channels.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise IOError("Lock de I/O inválido.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self.store.authorize(write)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def config(self):
        if not self.config_file.exists():
            raise IOError("Drive não configurado: execute drive bootstrap ou vincule as pastas pela ponte MCP.")
        value = read_json(self.config_file)
        fields = {"schema", "root_folder_id", "input_folder_id", "output_folder_id", "account_fingerprint", "backend"}
        if not isinstance(value, dict) or set(value) != fields or value["schema"] != "mimir.drive-config.v1" or value["backend"] not in {"plugin", "api"}:
            raise IOError("Configuração do Drive malformada.")
        for key in ("root_folder_id", "input_folder_id", "output_folder_id"):
            remote_id(value[key])
        if len({value[key] for key in ("root_folder_id", "input_folder_id", "output_folder_id")}) != 3 or not re.fullmatch(r"[0-9a-f]{64}", value["account_fingerprint"]):
            raise IOError("Pastas distintas e identidade da conta são obrigatórias.")
        return value

    def configure(self, root, incoming, outgoing, email, backend="plugin"):
        with self.locked(write=True):
            if backend not in {"plugin", "api"}:
                raise IOError("Backend deve ser plugin ou api.")
            for metadata in (root, incoming, outgoing):
                remote_id(metadata["id"])
                if metadata.get("mimeType") != "application/vnd.google-apps.folder" or metadata.get("trashed"):
                    raise IOError("A configuração requer pastas reais não excluídas.")
                if metadata.get("shared") or metadata.get("ownedByMe") is False:
                    raise IOError("A configuração requer pastas privadas pertencentes à conta autenticada.")
            if root["id"] not in incoming.get("parents", []) or root["id"] not in outgoing.get("parents", []) or len({row["id"] for row in (root, incoming, outgoing)}) != 3:
                raise IOError("Entrada e saída devem ser pastas distintas dentro da raiz verificada.")
            value = {"schema": "mimir.drive-config.v1", "root_folder_id": root["id"],
                "input_folder_id": incoming["id"], "output_folder_id": outgoing["id"],
                "account_fingerprint": account_fingerprint(email), "backend": backend}
            if self.config_file.exists():
                previous = self.config()
                if previous != value:
                    raise IOError("Namespace já vinculado; use outro namespace para outra conta ou conjunto de pastas.")
            write_atomic(self.config_file, value)
            return value

    def _input_metadata(self, metadata):
        config = self.config()
        if metadata.get("trashed") or config["input_folder_id"] not in metadata.get("parents", []):
            raise IOError("Origem não pertence à pasta Entrada autorizada.")
        local_name(metadata)
        return revision(metadata)

    def ingest_file(self, path, before, after, **settings):
        """Materialized connector files must be staged inside this namespace."""
        with self.locked(write=True):
            first, last = self._input_metadata(before), self._input_metadata(after)
            if first != last or before["parents"] != after["parents"]:
                raise IOError("O arquivo do Drive mudou durante o download.")
            path = Path(os.path.abspath(path))
            staging = Path(os.path.abspath(self.staging))
            if path.is_symlink() or not path.is_relative_to(staging) or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(staging)):
                raise IOError("Arquivo materializado precisa estar dentro do staging autorizado, sem symlinks.")
            with _open_regular(path) as stream:
                data = stream.read(self.limits.max_file_bytes + 1)
            digest = verify_bytes(data, before, self.limits.max_file_bytes)
            config = self.config()
            export_mime, _ = export_spec(before)
            identity = fingerprint({"file_id": before["id"], "revision": first, "sha256": digest,
                                    "export_mime_type": export_mime})
            folder = self.inputs / identity
            ensure_directories(self.store.base, folder)
            receipt_path = folder / "receipt.json"
            receipt = read_json(receipt_path) if receipt_path.exists() else None
            if receipt and receipt["status"] == "COMPLETE" and not settings.get("force"):
                self._verify_input(receipt, identity)
                self._enqueue_unlocked(receipt, "ingestion", receipt["semantic_generation"])
                return receipt
            metadata = {"provider": "google-drive", "file_id": before["id"], "revision": first,
                "name": before["name"], "mime_type": before["mimeType"], "export_mime_type": export_mime,
                "modified_time": before["modifiedTime"], "web_url": before.get("webViewLink"),
                "parent_folder_id": config["input_folder_id"], "account_fingerprint": config["account_fingerprint"],
                "sha256": digest, "input_job_id": identity}
            receipt = {"schema": "mimir.input-receipt.v1", "id": identity, "status": "PROCESSING",
                       "source": metadata, "created_at": receipt["created_at"] if receipt else now()}
            write_atomic(receipt_path, receipt)
            runtime = folder / "runtime"
            source_folder = folder / "source"
            ensure_directories(self.store.base, source_folder)
            pacote = Pacote(runtime)
            try:
                if not pacote.state_file.exists():
                    source = source_folder / local_name(before)
                    if source.exists() or source.is_symlink():
                        with _open_regular(source) as stream:
                            if hashlib.sha256(stream.read(self.limits.max_file_bytes + 1)).hexdigest() != digest:
                                raise IOError("Cache de entrada existente diverge dos bytes recebidos.")
                    else:
                        descriptor = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(descriptor, "wb") as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                    pacote.open()
                if not pacote._state()["items"]:
                    source = source_folder / local_name(before)
                    pacote.add([source])
                generation = self.memory.ingest(runtime, source_metadata={content_id(digest): metadata}, **settings)
                receipt.update(status="COMPLETE", semantic_generation=generation["generation"],
                    semantic_fingerprint=generation["fingerprint"], completed_at=now(), summary=generation["summary"])
                write_atomic(receipt_path, receipt)
                seen = self.root / "seen"
                ensure_directories(self.store.base, seen)
                write_atomic(seen / (self._seen_key(before) + ".json"), {"input_job_id": identity})
                self._enqueue_unlocked(receipt, "ingestion", generation["generation"])
                return receipt
            except Exception as exc:
                write_atomic(receipt_path, {**receipt, "status": "FAILED", "error_type": type(exc).__name__, "failed_at": now()})
                raise

    @staticmethod
    def _seen_key(metadata):
        return fingerprint({"file_id": metadata["id"], "revision": revision(metadata), "export": export_spec(metadata)[0]})

    def _verify_input(self, receipt, identity):
        if receipt.get("schema") != "mimir.input-receipt.v1" or receipt.get("id") != identity or receipt.get("status") != "COMPLETE":
            raise IOError("Recibo de entrada inválido.")
        source = receipt["source"]
        if source.get("input_job_id") != identity or fingerprint({"file_id": source["file_id"], "revision": source["revision"], "sha256": source["sha256"], "export_mime_type": source["export_mime_type"]}) != identity:
            raise IOError("Identidade do recibo de entrada divergente.")
        generation = receipt["semantic_generation"]
        if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise IOError("Geração do recibo inválida.")
        with self.store.locked():
            _, ledger = self.store.load()
            archived = read_json(self.store.generations / generation / "manifest.json")
            self.store._check_manifest(archived)
            if archived["fingerprint"] != receipt["semantic_fingerprint"] or not any(run["parameters"].get("source_metadata", {}).get(content_id(source["sha256"])) == source for run in ledger.rows("inference_runs")):
                raise IOError("Recibo não corresponde à proveniência canônica da memória.")

    def processed(self, metadata):
        """Skip unchanged revisions only after checking their canonical receipt."""
        with self.locked(write=True):
            self._input_metadata(metadata)
            pointer = self.root / "seen" / (self._seen_key(metadata) + ".json")
            if not pointer.exists():
                return None
            identity = read_json(pointer)["input_job_id"]
            if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
                raise IOError("Índice de entrada inválido.")
            receipt = read_json(self.inputs / identity / "receipt.json")
            if receipt.get("status") != "COMPLETE":
                return None
            self._verify_input(receipt, identity)
            if receipt["source"]["revision"] != revision(metadata):
                raise IOError("Índice aponta para outra revisão.")
            self._enqueue_unlocked(receipt, "ingestion", receipt["semantic_generation"])
            return receipt

    def stage(self, data, name="source.bin"):
        if not isinstance(data, bytes) or len(data) > self.limits.max_file_bytes:
            raise IOError("Conteúdo de staging inválido ou excessivo.")
        with self.locked(write=True):
            descriptor, path = tempfile.mkstemp(prefix="incoming-", dir=self.staging)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            return Path(path)

    def _enqueue_unlocked(self, payload, topic, generation=None):
        if not isinstance(payload, dict) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", topic):
            raise IOError("Saída requer objeto JSON e tópico simples.")
        message = {"schema": "mimir.exchange.v1", "namespace": self.store.namespace, "topic": topic,
                   "semantic_generation": generation, "payload": payload}
        data = canonical(message) + b"\n"
        if len(data) > self.limits.max_output_bytes:
            raise IOError("Saída excede max_output_bytes.")
        identity = fingerprint(message)
        folder = self.outbox / identity
        ensure_directories(self.store.base, folder)
        job_path = folder / "receipt.json"
        if job_path.exists():
            job = read_json(job_path)
            self._check_output(job)
            return job
        path = folder / "payload.json"
        write_atomic(path, message)
        job = self._new_output(identity, message, data)
        write_atomic(job_path, job)
        return job

    @staticmethod
    def _new_output(identity, message, data):
        return {"schema": "mimir.output-receipt.v1", "id": identity, "status": "PENDING", "topic": message["topic"],
               "semantic_generation": message["semantic_generation"], "file_name": f"mimir-{message['topic']}-{identity}.json",
               "sha256": hashlib.sha256(data).hexdigest(), "md5": hashlib.md5(data).hexdigest(),
               "size": len(data), "created_at": now(), "remote_reserved_id": None, "remote": None}

    def _load_output(self, folder):
        if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", folder.name):
            raise IOError("Entrada inesperada na outbox.")
        receipt = folder / "receipt.json"
        if receipt.exists() or receipt.is_symlink():
            job = read_json(receipt)
            if job.get("id") != folder.name:
                raise IOError("Recibo pertence a outra saída.")
            self._check_output(job)
            return job
        path = folder / "payload.json"
        if not path.exists() and not path.is_symlink():
            return None  # Crash before payload publication; no delivery to perform.
        with _open_regular(path) as stream:
            data = stream.read(self.limits.max_output_bytes + 1)
        message = json.loads(data)
        if len(data) > self.limits.max_output_bytes or not isinstance(message, dict) or set(message) != {"schema", "namespace", "topic", "semantic_generation", "payload"} or message["schema"] != "mimir.exchange.v1" or message["namespace"] != self.store.namespace or fingerprint(message) != folder.name or data != canonical(message) + b"\n":
            raise IOError("Publicação interrompida contém payload inválido.")
        self.store.authorize(write=True)
        job = self._new_output(folder.name, message, data)
        self._check_output(job)
        write_atomic(receipt, job)
        return job

    def enqueue(self, payload, topic="system", generation=None):
        with self.locked(write=True):
            return self._enqueue_unlocked(payload, topic, generation)

    def _job_folder(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise IOError("ID de saída inválido.")
        folder = self.outbox / identity
        ensure_directories(self.store.base, folder)
        return folder

    def _check_output(self, job):
        if job.get("schema") != "mimir.output-receipt.v1" or job.get("status") not in {"PENDING", "DELIVERED"} or type(job.get("size")) is not int or not 0 < job["size"] <= self.limits.max_output_bytes or job.get("file_name") != f"mimir-{job.get('topic')}-{job.get('id')}.json":
            raise IOError("Recibo de saída inválido.")
        path = self._job_folder(job["id"]) / "payload.json"
        with _open_regular(path) as stream:
            data = stream.read(self.limits.max_output_bytes + 1)
        if len(data) != job["size"] or hashlib.sha256(data).hexdigest() != job["sha256"] or hashlib.md5(data).hexdigest() != job["md5"] or fingerprint(json.loads(data)) != job["id"]:
            raise IOError("Conteúdo da outbox alterado ou corrompido.")
        return path

    def pending(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise IOError("Limite da outbox deve ser 1–10000.")
        with self.locked():
            jobs = []
            for folder in sorted(self.outbox.iterdir()):
                job = self._load_output(folder)
                if job is None:
                    continue
                path = self._check_output(job)
                if job["status"] != "DELIVERED":
                    jobs.append({**job, "local_path": str(path)})
                if len(jobs) == limit:
                    break
            return jobs

    def reserve(self, identity, file_id):
        with self.locked(write=True):
            remote_id(file_id)
            folder = self._job_folder(identity)
            job = read_json(folder / "receipt.json")
            self._check_output(job)
            if job["remote_reserved_id"] not in {None, file_id}:
                raise IOError("Uma saída não pode trocar seu ID remoto reservado.")
            job["remote_reserved_id"] = file_id
            write_atomic(folder / "receipt.json", job)
            return job

    def acknowledge(self, identity, metadata):
        with self.locked(write=True):
            config = self.config()
            folder = self._job_folder(identity)
            job = read_json(folder / "receipt.json")
            self._check_output(job)
            remote_id(metadata["id"])
            if metadata.get("trashed") or config["output_folder_id"] not in metadata.get("parents", []) or metadata.get("name") != job["file_name"] or int(metadata.get("size", -1)) != job["size"]:
                raise IOError("Recibo remoto não corresponde à saída/pasta autorizada.")
            checksums = [(metadata.get("sha256Checksum"), job["sha256"]), (metadata.get("md5Checksum"), job["md5"])]
            if not any(value for value, expected in checksums) or any(value and value != expected for value, expected in checksums):
                raise IOError("Confirmação requer checksum do arquivo remoto.")
            if job["remote_reserved_id"] is not None and metadata["id"] != job["remote_reserved_id"]:
                raise IOError("O Drive retornou outro ID que o reservado.")
            if job["status"] == "DELIVERED":
                if metadata["id"] != job["remote"]["id"]:
                    raise IOError("Saída já confirmada em outro arquivo remoto.")
                return job
            job.update(status="DELIVERED", remote={key: metadata.get(key) for key in
                ("id", "name", "parents", "size", "md5Checksum", "sha256Checksum", "webViewLink")}, delivered_at=now())
            write_atomic(folder / "receipt.json", job)
            return job

    def status(self):
        with self.locked():
            config = self.config() if self.config_file.exists() else None
            inputs, outputs = {}, {}
            for base, counts in ((self.inputs, inputs), (self.outbox, outputs)):
                for folder in base.iterdir():
                    if folder.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}", folder.name):
                        raise IOError("Entrada inesperada no armazenamento de I/O.")
                    row = self._load_output(folder) if base == self.outbox else read_json(folder / "receipt.json") if (folder / "receipt.json").exists() else None
                    if row is None:
                        continue
                    counts[row["status"]] = counts.get(row["status"], 0) + 1
            return {"schema": "mimir.io-status.v1", "configured": config is not None,
                    "drive": config, "inputs": inputs, "outputs": outputs, "staging_directory": str(self.staging)}

    def export_memory(self):
        with self.store.locked():
            manifest, ledger = self.store.load()
            if manifest is None:
                raise IOError("A memória ainda está vazia.")
            payload = {"schema": "mimir.memory-export.v1", "manifest": manifest,
                       "collections": {name: ledger.rows(name) for name in ledger.data}}
        return self.enqueue(payload, "memory", manifest["generation"])

    def publish_query(self, question, **settings):
        result = self.memory.query(question, **settings)
        return {"result": result, "delivery": self.enqueue(result, "answer", result["generation"])}
