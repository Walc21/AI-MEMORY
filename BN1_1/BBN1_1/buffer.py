"""BBN1_1 requests real bytes from Pacote and verifies their integrity."""

import hashlib
import os
import tempfile
from pathlib import Path

from BN1_1.Sorter.sorter import Sorter


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
        for digest, filenames in groups.items():
            extension = sorter.extension_for(digest)
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
