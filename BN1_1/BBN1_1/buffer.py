"""BBN1_1 requests real bytes from Pacote and verifies their integrity."""

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Callable

from BN1_1.Sorter.sorter import Sorter
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

    def store(self, groups: dict[str, list[str]], sorter: Sorter, pacote) -> int:
        stored = 0
        for extension, filenames in groups.items():
            # A separate branch avoids collision with any literal extension.
            folder = self.root / "by_extension" / extension if extension else self.root / "no_extension"
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            for filename in filenames:
                if sorter.extension(filename) != extension:
                    raise BufferError("O arquivo não corresponde à partição do Sorter.")
                target = folder / filename
                fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=folder)
                os.close(fd)
                try:
                    name_hash, content_hash = pacote.supply(filename, Path(temporary))
                    if hashlib.sha256(filename.encode("utf-8")).hexdigest() != name_hash:
                        raise BufferError("SHA-256 do nome não confere.")
                    if _sha256(Path(temporary)) != content_hash:
                        raise BufferError("SHA-256 do conteúdo não confere.")
                    os.replace(temporary, target)
                    stored += 1
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        return stored

    def purge_verified(
        self, expected: dict[str, str], hub: HotHub,
        verify_pacote: Callable[[], dict[str, str]],
    ) -> None:
        """Drop BBN bytes only while two complete verified copies exist."""
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
