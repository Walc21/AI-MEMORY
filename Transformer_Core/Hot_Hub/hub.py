"""Mirror flat originals and publish a common field representation."""

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from BN1_1.contracts import validate_names


class HubError(Exception):
    pass


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise HubError(f"Arquivo inválido: {path}")
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class HotHub:
    def __init__(self, runtime: Path):
        self.root = runtime / "Transformer_Core" / "Hot_Hub"
        self.data = self.root / "data"
        self.manifest = self.root / "manifest.json"

    @staticmethod
    def _tree_files(root: Path) -> set[str]:
        if not root.exists():
            return set()
        files = set()
        for path in root.rglob("*"):
            if path.is_symlink() or (not path.is_dir() and not path.is_file()):
                raise HubError("Árvore espelhada contém entrada inesperada.")
            if path.is_file():
                files.add(path.relative_to(root).as_posix())
        return files

    def mirror(self, bbn_root: Path, expected: dict[str, str]) -> int:
        validate_names(list(expected))
        if self._tree_files(bbn_root) != set(expected):
            raise HubError("O BBN1_1 não contém exatamente o lote esperado.")
        self.data.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A killed process may have left a temporary copy, never a published file.
        for temporary in self.data.rglob(".mirror-*"):
            if temporary.is_symlink() or not temporary.is_file():
                raise HubError("Cópia temporária inválida no Hot Hub.")
            temporary.unlink()
        for relative, digest in expected.items():
            source = bbn_root / relative
            if _digest(source) != digest:
                raise HubError("O arquivo do BBN1_1 diverge do conteúdo recebido.")
            target = self.data / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".mirror-", dir=target.parent)
            try:
                hasher = hashlib.sha256()
                with os.fdopen(fd, "wb") as outgoing:
                    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(descriptor, "rb") as incoming:
                        if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                            raise HubError("Fonte inválida durante o espelhamento.")
                        for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                            hasher.update(chunk)
                            outgoing.write(chunk)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                if hasher.hexdigest() != digest:
                    raise HubError("Conteúdo alterado durante o espelhamento.")
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        if self._tree_files(self.data) != set(expected):
            raise HubError("O Hot Hub contém arquivos extras ou ausentes.")
        # Publish the manifest last. It marks the entire mirror as verified.
        fd, temporary = tempfile.mkstemp(prefix=".manifest-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"files": expected}, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.manifest)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.verify(expected)
        return len(expected)

    def verify(self, expected: dict[str, str]) -> None:
        try:
            with self.manifest.open(encoding="utf-8") as stream:
                manifest = json.load(stream)
        except (OSError, ValueError) as exc:
            raise HubError("Manifesto íntegro do Hot Hub não encontrado.") from exc
        if manifest != {"files": expected} or self._tree_files(self.data) != set(expected):
            raise HubError("O espelho do Hot Hub está incompleto ou divergente.")
        for relative, digest in expected.items():
            if _digest(self.data / relative) != digest:
                raise HubError("Arquivo alterado no Hot Hub.")

    def normalize(self, expected: dict[str, str]) -> dict:
        from Transformer_Core.Fields.store import FieldStore, FieldError

        self.verify(expected)
        try:
            return FieldStore(self.root).build(expected)
        except FieldError as exc:
            raise HubError(str(exc)) from exc
