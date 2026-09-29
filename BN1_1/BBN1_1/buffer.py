"""BBN1_1 requests real bytes from Pacote and verifies their integrity."""

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Callable

from BN1_1.contracts import validate_names
from Transformer_Core.Hot_Hub.hub import HotHub, _digest


class BufferError(Exception):
    pass


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class BBN1_1:
    def __init__(self, runtime: Path):
        self.root = runtime / "BN1_1" / "BBN1_1"

    def store(self, filenames: list[str], pacote) -> int:
        validate_names(filenames)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for temporary in self.root.glob(".incoming-*"):
            if temporary.is_symlink() or not temporary.is_file():
                raise BufferError("Cópia temporária inválida no BBN1_1.")
            temporary.unlink()
        for filename in filenames:
            target = self.root / filename
            fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=self.root)
            os.close(fd)
            try:
                name_hash, content_hash = pacote.supply(filename, Path(temporary))
                if hashlib.sha256(filename.encode("utf-8")).hexdigest() != name_hash:
                    raise BufferError("SHA-256 do nome não confere.")
                if _sha256(Path(temporary)) != content_hash:
                    raise BufferError("SHA-256 do conteúdo não confere.")
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return len(filenames)

    def purge_verified(
        self, expected: dict[str, str], hub: HotHub,
        verify_pacote: Callable[[], dict[str, str]],
    ) -> None:
        """Drop BBN bytes only while Pacote bytes and lossless Hub chunks are verified."""
        hub.verify(expected)
        if verify_pacote() != expected:
            raise BufferError("A segunda cópia no Pacote não está íntegra.")
        actual = HotHub._tree_files(self.root)
        if actual != set(expected):
            raise BufferError("O BBN1_1 diverge do lote antes da limpeza.")
        for relative, digest in expected.items():
            if _digest(self.root / relative) != digest:
                raise BufferError("Arquivo do BBN1_1 alterado antes da limpeza.")
        for relative in expected:
            (self.root / relative).unlink()
        if self.root.exists():
            for directory in sorted(
                (p for p in self.root.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts), reverse=True,
            ):
                directory.rmdir()
            self.root.rmdir()

