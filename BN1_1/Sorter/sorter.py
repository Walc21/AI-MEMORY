"""Sorter sees renamed filenames only, never file bytes or original names."""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
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
        # The format registry survives individual ephemeral cycle directories.
        self.registry = Path(os.environ.get(
            "MIMIR_REGISTRY_DB",
            Path(__file__).resolve().parents[2] / ".mimir-state" / "extensions.sqlite3",
        ))
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
        groups: dict[str, list[str]] = {}
        extensions: dict[str, str] = {}
        for filename in filenames:
            extension = self.extension(filename)
            digest = self.idd(extension)
            if digest in extensions and extensions[digest] != extension:
                raise SorterError("Colisão SHA-256 entre extensões deste ciclo.")
            extensions[digest] = extension
            groups.setdefault(digest, []).append(filename)
        self.registry.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(self.registry, timeout=10)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("""CREATE TABLE IF NOT EXISTS extension_registry (
                    idd TEXT PRIMARY KEY COLLATE BINARY CHECK(length(idd) = 64),
                    extension TEXT NOT NULL UNIQUE COLLATE BINARY
                )""")
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    for digest, extension in extensions.items():
                        row = connection.execute(
                            "SELECT extension FROM extension_registry WHERE idd = ?", (digest,)
                        ).fetchone()
                        if row is None:
                            connection.execute(
                                "INSERT INTO extension_registry (idd, extension) VALUES (?, ?)",
                                (digest, extension),
                            )
                        elif row[0] != extension:
                            raise SorterError("Colisão ou inconsistência no registro de SHA-256.")
        except sqlite3.Error as exc:
            raise SorterError(f"Falha no registro SQLite: {exc}") from exc

        self.partitions.mkdir(mode=0o700, parents=True, exist_ok=True)
        for digest, names in groups.items():
            # A partition is a metadata folder; it never holds file bytes.
            (self.partitions / digest).mkdir(mode=0o700, exist_ok=True)
            _write_json(self.partitions / digest / "names.json", {"idd": digest, "filenames": names})
        return groups

    def extension_for(self, digest: str) -> str:
        try:
            # BBN1_1's lookup is read-only. Only Sorter writes this registry.
            with closing(sqlite3.connect(self.registry.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                row = connection.execute(
                    "SELECT extension FROM extension_registry WHERE idd = ?", (digest,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise SorterError(f"Falha na leitura do registro SQLite: {exc}") from exc
        extension = row[0] if row else None
        if not isinstance(extension, str) or self.idd(extension) != digest:
            raise SorterError("IDD sem extensão verificável no registro.")
        return extension
