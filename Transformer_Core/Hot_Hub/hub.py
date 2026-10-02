"""One lossless byte-vector/chunk representation for every input format.

Pacote owns the cycle lock. Direct callers must serialize build operations.
Only the generation referenced by manifest.jsonl is published.
"""

import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path

from BN1_1.contracts import validate_names

CHUNK_SIZE = 1024
SCHEMA = "mimir.byte-chunks.v1"


class HubError(Exception):
    pass


def _open_regular(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(descriptor, "rb")
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise HubError(f"Arquivo inválido: {path}")
    return stream


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with _open_regular(path) as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def marker(filename: str) -> str:
    """The slash is metadata, never a filesystem path. Empty ext means none."""
    validate_names([filename])
    stem, separator, extension = filename.partition(".")
    return f"{stem}/{extension if separator else ''}"


def byte_chunks(vector: bytes, label: str, source_id: str):
    """Partition one immutable vector; zero padding is outside valid_length."""
    if not isinstance(vector, bytes):
        raise TypeError("O vetor deve ser bytes.")
    for index, start in enumerate(range(0, len(vector), CHUNK_SIZE)):
        block = vector[start:start + CHUNK_SIZE]
        yield {
            "kind": "chunk", "marker": label, "source_id": source_id,
            "index": index, "valid_length": len(block),
            "values": list(block) + [0] * (CHUNK_SIZE - len(block)),
        }


def _line(stream, payload: dict) -> None:
    stream.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")


def _sync(stream) -> None:
    stream.flush()
    os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _integer(value) -> bool:
    return type(value) is int


class HotHub:
    def __init__(self, runtime: Path):
        self.root = runtime / "Transformer_Core" / "Hot_Hub"
        self.generations = self.root / "generations"
        self.manifest = self.root / "manifest.jsonl"

    @staticmethod
    def _tree_files(root: Path) -> set[str]:
        if root.is_symlink():
            raise HubError("A raiz não pode ser um link simbólico.")
        if not root.exists():
            return set()
        files = set()
        for path in root.rglob("*"):
            if path.is_symlink() or (not path.is_dir() and not path.is_file()):
                raise HubError("Árvore contém entrada inesperada.")
            if path.is_file():
                files.add(path.relative_to(root).as_posix())
        return files

    def _check_layout(self) -> None:
        for directory in (self.root.parent, self.root, self.generations):
            if directory.is_symlink():
                raise HubError("Diretório do Hot Hub não pode ser link simbólico.")
        if any((self.root / name).exists() for name in ("data", "fields.json", "manifest.json", "representations")):
            raise HubError("Hot Hub antigo preservado; use outro MIMIR_RUNTIME_DIR.")

    def build(self, bbn_root: Path, expected: dict[str, str]) -> dict:
        """Read each source into one byte vector and atomically publish a batch."""
        validate_names(list(expected))
        self._check_layout()
        if self._tree_files(bbn_root) != set(expected):
            raise HubError("O BBN1_1 não contém exatamente o lote esperado.")
        self.generations.mkdir(mode=0o700, parents=True, exist_ok=True)
        generation = uuid.uuid4().hex
        folder = self.generations / generation
        folder.mkdir(mode=0o700)
        records = {}
        summary = {"files": len(expected), "bytes": 0, "chunks": 0}
        for position, (name, digest) in enumerate(sorted(expected.items())):
            with _open_regular(bbn_root / name) as source:
                vector = source.read()  # bytes is the single ordered uint8 vector.
            if hashlib.sha256(vector).hexdigest() != digest:
                raise HubError("Os bytes do BBN1_1 divergem do arquivo recebido.")
            count = (len(vector) + CHUNK_SIZE - 1) // CHUNK_SIZE
            header = {
                "kind": "file", "schema": SCHEMA, "source_name": name,
                "source_id": f"{generation}:{name}", "marker": marker(name),
                "byte_length": len(vector), "sha256": digest,
                "chunk_size": CHUNK_SIZE, "chunk_count": count,
            }
            relative = f"{position:08d}.jsonl"
            target = folder / relative
            with target.open("x", encoding="utf-8") as stream:
                _line(stream, header)
                for chunk in byte_chunks(vector, header["marker"], header["source_id"]):
                    _line(stream, chunk)
                _sync(stream)
            records[name] = {"file": relative, "sha256": _digest(target)}
            summary["bytes"] += len(vector)
            summary["chunks"] += count
            del vector
        manifest = {
            "schema": SCHEMA, "generation": generation, "sources": expected,
            "records": records, "summary": summary,
        }
        # Verify reconstruction before publication or any BBN cleanup.
        self._verify_manifest(manifest, expected)
        _sync_directory(folder)
        _sync_directory(self.generations)
        fd, temporary = tempfile.mkstemp(prefix=".manifest-", suffix=".jsonl", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                _line(stream, manifest)
                _sync(stream)
            os.replace(temporary, self.manifest)
            _sync_directory(self.root)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return self.verify(expected)

    def _verify_record(self, path: Path, name: str, digest: str, generation: str) -> tuple[int, int]:
        hasher = hashlib.sha256()
        with _open_regular(path) as stream:
            header = json.loads(stream.readline())
            length, count = header["byte_length"], header["chunk_count"]
            if not _integer(length) or length < 0 or not _integer(count):
                raise HubError("Dimensões inválidas.")
            if count != (length + CHUNK_SIZE - 1) // CHUNK_SIZE:
                raise HubError("Quantidade de chunks inválida.")
            identity = f"{generation}:{name}"
            if header != {
                "kind": "file", "schema": SCHEMA, "source_name": name,
                "source_id": identity, "marker": marker(name),
                "byte_length": length, "sha256": digest,
                "chunk_size": CHUNK_SIZE, "chunk_count": count,
            }:
                raise HubError("Identidade ou contrato do arquivo divergente.")
            for index in range(count):
                chunk = json.loads(stream.readline())
                valid = min(CHUNK_SIZE, length - index * CHUNK_SIZE)
                values = chunk["values"]
                if not isinstance(values, list) or len(values) != CHUNK_SIZE or any(
                    not _integer(value) or not 0 <= value <= 255 for value in values
                ):
                    raise HubError("Chunk não contém 1024 bytes inteiros.")
                if not _integer(chunk["index"]) or not _integer(chunk["valid_length"]):
                    raise HubError("Índice ou comprimento inválido.")
                if chunk != {
                    "kind": "chunk", "marker": marker(name), "source_id": identity,
                    "index": index, "valid_length": valid, "values": values,
                } or any(values[valid:]):
                    raise HubError("Chunk fora de ordem, de outro arquivo ou padding inválido.")
                hasher.update(bytes(values[:valid]))
            if stream.read(1) or hasher.hexdigest() != digest:
                raise HubError("Bytes ausentes, extras ou alterados no conjunto de chunks.")
        return length, count

    def _verify_manifest(self, manifest: dict, expected: dict[str, str]) -> dict:
        try:
            generation = manifest["generation"]
            if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
                raise HubError("Geração inválida.")
            if manifest["schema"] != SCHEMA or manifest["sources"] != expected:
                raise HubError("Manifesto diverge do lote esperado.")
            records = manifest["records"]
            if set(records) != set(expected):
                raise HubError("Inventário de arquivos divergente.")
            folder = self.generations / generation
            paths = {f"{i:08d}.jsonl" for i in range(len(expected))}
            if not folder.is_dir() or self._tree_files(folder) != paths:
                raise HubError("Geração incompleta ou com arquivos extras.")
            summary = {"files": len(expected), "bytes": 0, "chunks": 0}
            for position, (name, digest) in enumerate(sorted(expected.items())):
                record = records[name]
                relative = f"{position:08d}.jsonl"
                if record["file"] != relative or _digest(folder / relative) != record["sha256"]:
                    raise HubError("Registro alterado ou associado a outro arquivo.")
                length, count = self._verify_record(folder / relative, name, digest, generation)
                summary["bytes"] += length
                summary["chunks"] += count
            if manifest["summary"] != summary:
                raise HubError("Resumo divergente.")
            return manifest
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HubError("Conjunto de chunks inválido ou incompleto.") from exc

    def verify(self, expected: dict[str, str]) -> dict:
        validate_names(list(expected))
        self._check_layout()
        try:
            with _open_regular(self.manifest) as stream:
                manifest = json.loads(stream.read())
            return self._verify_manifest(manifest, expected)
        except (OSError, ValueError, TypeError) as exc:
            raise HubError("Manifesto íntegro do Hot Hub não encontrado.") from exc

    def reconstruct(self, name: str, expected: dict[str, str]) -> bytes:
        """Reference consumer: verify the batch, then concatenate valid bytes."""
        return self.reconstruct_snapshot(name, self.verify(expected))

    def reconstruct_snapshot(self, name: str, manifest: dict) -> bytes:
        """Consume one record from an already verified generation snapshot.

        The pipeline verifies the batch once, then checks each requested record.
        This avoids verifying the entire batch once per source. Direct callers
        must obtain this snapshot through verify() and serialize build operations.
        """
        validate_names([name])
        path = self.generations / manifest["generation"] / manifest["records"][name]["file"]
        digest = manifest["sources"][name]
        if _digest(path) != manifest["records"][name]["sha256"]:
            raise HubError("Registro mudou após a verificação do lote.")
        self._verify_record(path, name, digest, manifest["generation"])
        with _open_regular(path) as stream:
            header = json.loads(stream.readline())
            vector = b"".join(bytes(chunk["values"][:chunk["valid_length"]])
                              for chunk in map(json.loads, stream))
        if len(vector) != header["byte_length"] or hashlib.sha256(vector).hexdigest() != digest:
            raise HubError("O registro mudou durante a reconstrução.")
        return vector
