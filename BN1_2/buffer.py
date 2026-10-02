"""BN1_2 stores complete generations; manifest.json is the publication point."""

import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from Transformer_Core.Hot_Hub.hub import HotHub, HubError, _digest, _open_regular, _sync_directory
from Transformer_Core.structural.curator import validate
from Transformer_Core.structural.model import Limits, SCHEMA, StructuralError, canonical, fingerprint

HANDOFF_SCHEMA = "mimir.bn1_2.v1"


def source_descriptor(snapshot: dict, name: str, byte_length: int) -> dict:
    return {"name": name, "id": snapshot["generation"] + ":" + name,
            "sha256": snapshot["sources"][name], "byte_length": byte_length,
            "hot_hub_generation": snapshot["generation"],
            "record_sha256": snapshot["records"][name]["sha256"]}


def ensure_directories(runtime: Path, target: Path) -> None:
    paths = [runtime, *reversed(list(target.parents)[:len(target.relative_to(runtime).parts) - 1]), target]
    for path in paths:
        if path.is_symlink():
            raise StructuralError("Diretório estrutural não pode ser link simbólico.")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)


def write_atomic(path: Path, payload: dict) -> None:
    if path.is_symlink():
        raise StructuralError("Destino de publicação não pode ser link simbólico.")
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(payload) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class BBN1_2:
    def __init__(self, runtime: Path):
        self.runtime = runtime
        self.root = runtime / "BN1_2"
        self.generations = self.root / "generations"
        self.manifest = self.root / "manifest.json"

    def _layout(self):
        ensure_directories(self.runtime, self.generations)
        if self.manifest.is_symlink():
            raise StructuralError("Manifesto do BN1_2 não pode ser link simbólico.")

    def begin(self) -> tuple[str, Path]:
        self._layout()
        generation = uuid.uuid4().hex
        folder = self.generations / generation
        folder.mkdir(mode=0o700)
        return generation, folder

    def store_document(self, folder: Path, position: int, document: dict) -> dict:
        target = folder / f"{position:08d}.json"
        with target.open("xb") as stream:
            stream.write(canonical(document) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        return {"file": target.name, "sha256": _digest(target)}

    @staticmethod
    def summary(documents) -> dict:
        counts = {"files": 0, "nodes": 0, "edges": 0, "complete": 0, "opaque": 0}
        for document in documents:
            counts["files"] += 1
            counts["nodes"] += len(document["nodes"])
            counts["edges"] += len(document["provenance"]["edges"])
            counts[document["protocol"]["status"]] += 1
        return counts

    def _documents(self, manifest: dict, snapshot: dict):
        try:
            if set(manifest) != {"schema", "representation_schema", "generation", "upstream", "profile", "records", "summary"}:
                raise StructuralError("Contrato do manifesto BN1_2 inválido.")
            if manifest["schema"] != HANDOFF_SCHEMA or manifest["representation_schema"] != SCHEMA:
                raise StructuralError("Esquema BN1_2 inválido.")
            generation = manifest["generation"]
            if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
                raise StructuralError("Geração BN1_2 inválida.")
            if manifest["upstream"] != {"generation": snapshot["generation"], "fingerprint": fingerprint(snapshot)}:
                raise StructuralError("BN1_2 pertence a outra geração do Hot Hub.")
            records = manifest["records"]
            if set(records) != set(snapshot["sources"]):
                raise StructuralError("Inventário BN1_2 diverge dos originais.")
            folder = self.generations / generation
            expected_paths = {f"{position:08d}.json" for position in range(len(records))}
            if not folder.is_dir() or HotHub._tree_files(folder) != expected_paths:
                raise StructuralError("Geração BN1_2 incompleta ou com arquivos extras.")
            for position, name in enumerate(sorted(records)):
                record = records[name]
                if set(record) != {"file", "sha256"} or record["file"] != f"{position:08d}.json":
                    raise StructuralError("Caminho do registro BN1_2 inválido.")
                path = folder / record["file"]
                if _digest(path) != record["sha256"]:
                    raise StructuralError("Representação estrutural alterada.")
                with _open_regular(path) as stream:
                    document = json.load(stream)
                # The length comes from the already verified Hot Hub header.
                hub_path = self.runtime / "Transformer_Core/Hot_Hub/generations" / snapshot["generation"] / snapshot["records"][name]["file"]
                with _open_regular(hub_path) as stream:
                    length = json.loads(stream.readline())["byte_length"]
                validate(document, source_descriptor(snapshot, name, length))
                yield name, document
        except (HubError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise StructuralError("Geração estrutural inválida ou incompleta.") from exc

    def verify_candidate(self, manifest: dict, snapshot: dict) -> dict:
        try:
            profile = manifest["profile"]
            if set(profile) != {"pipeline_version", "limits", "strict", "dependencies"} or type(profile["strict"]) is not bool or not isinstance(profile["dependencies"], dict) or not isinstance(profile["pipeline_version"], str):
                raise StructuralError("Perfil da transformação inválido.")
            limits = Limits(**profile["limits"])
        except (ValueError, TypeError, KeyError) as exc:
            raise StructuralError("Limites da transformação inválidos.") from exc
        def checked_documents():
            for _, document in self._documents(manifest, snapshot):
                if len(document["nodes"]) > limits.max_nodes or document["source"]["byte_length"] > limits.max_file_bytes:
                    raise StructuralError("Representação excede os limites publicados.")
                if profile["strict"] and document["protocol"]["status"] != "complete":
                    raise StructuralError("Perfil estrito contém representação opaca.")
                yield document
        counts = self.summary(checked_documents())
        if manifest["summary"] != counts or any(type(value) is not int for value in manifest["summary"].values()):
            raise StructuralError("Resumo do BN1_2 divergente.")
        return manifest

    def publish(self, manifest: dict, snapshot: dict) -> dict:
        self._layout()
        self.verify_candidate(manifest, snapshot)
        _sync_directory(self.generations / manifest["generation"])
        _sync_directory(self.generations)
        write_atomic(self.manifest, manifest)
        return manifest

    def verify(self, snapshot: dict) -> dict:
        self._layout()
        try:
            with _open_regular(self.manifest) as stream:
                manifest = json.load(stream)
            return self.verify_candidate(manifest, snapshot)
        except (OSError, ValueError, HubError) as exc:
            raise StructuralError("Manifesto íntegro do BN1_2 não encontrado.") from exc

    def documents(self, snapshot: dict):
        manifest = self.verify(snapshot)
        yield from self._documents(manifest, snapshot)
