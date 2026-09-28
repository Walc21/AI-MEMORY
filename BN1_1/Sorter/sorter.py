"""Sorter sees renamed filenames only, never file bytes or original names."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


RENAMED = re.compile(r"^[1-9][0-9]*_[A-Za-z0-9]{3}(?:\.(.+))?$")


class SorterError(Exception):
    pass


def _write_json(path: Path, value: object) -> None:
    fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Sorter:
    def __init__(self, runtime: Path):
        self.root = runtime / "BN1_1" / "Sorter"
        self.registry = self.root / "extension_registry.json"
        self.partitions = self.root / "partitions"

    @staticmethod
    def extension(filename: str) -> str:
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise SorterError("O Sorter só aceita nomes renomeados, sem caminhos.")
        if not RENAMED.fullmatch(filename):
            raise SorterError("Nome fora do contrato Namer → Pacote → Sorter.")
        return filename.rsplit(".", 1)[1] if "." in filename else ""

    @staticmethod
    def idd(extension: str) -> str:
        # SHA-256 is an identifier, not a reversible encoding.
        return hashlib.sha256(extension.encode("utf-8")).hexdigest()

    def classify(self, filenames: list[str]) -> dict[str, list[str]]:
        if len(filenames) != len(set(filenames)):
            raise SorterError("O mesmo nome renomeado apareceu mais de uma vez.")
        self.partitions.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with self.registry.open(encoding="utf-8") as stream:
                registry = json.load(stream)
        except FileNotFoundError:
            registry = {}
        if not isinstance(registry, dict):
            raise SorterError("Registro de extensões inválido.")

        groups: dict[str, list[str]] = {}
        for filename in filenames:
            extension = self.extension(filename)
            digest = self.idd(extension)
            if digest in registry and registry[digest] != extension:
                raise SorterError("Colisão ou inconsistência no registro de SHA-256.")
            registry[digest] = extension
            # Reserve the partition as soon as its first filename appears.
            (self.partitions / digest).mkdir(mode=0o700, exist_ok=True)
            groups.setdefault(digest, []).append(filename)

        # The persisted map is required to recover extensions: hashing alone
        # cannot reconstruct an unknown original string.
        _write_json(self.registry, registry)
        for digest, names in groups.items():
            _write_json(self.partitions / digest / "names.json", {"idd": digest, "filenames": names})
        return groups

    def extension_for(self, digest: str) -> str:
        with self.registry.open(encoding="utf-8") as stream:
            registry = json.load(stream)
        extension = registry.get(digest)
        if not isinstance(extension, str) or self.idd(extension) != digest:
            raise SorterError("IDD sem extensão verificável no registro.")
        return extension
