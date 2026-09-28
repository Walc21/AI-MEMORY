"""Pacote: single-cycle, format-agnostic staging of real files."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path


class CacheError(Exception):
    """An intake operation cannot be completed safely."""


def _write_json(path: Path, payload: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Pacote:
    def __init__(self, runtime: Path):
        self.runtime = runtime
        self.state_file = runtime / "cycle.json"
        self.files = runtime / "BN1_1" / "Pacote" / "files"
        self.namer_inbox = runtime / "BN1_1" / "Namer" / "inbox"

    @contextmanager
    def _locked(self):
        if self.runtime.is_symlink():
            raise CacheError("O diretório de execução não pode ser um link simbólico.")
        self.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.runtime.is_symlink():
            raise CacheError("O diretório de execução não pode ser um link simbólico.")
        os.chmod(self.runtime, 0o700)
        lock_path = self.runtime / "cycle.lock"
        if lock_path.is_symlink():
            raise CacheError("O arquivo de bloqueio não pode ser um link simbólico.")
        with lock_path.open("a+b") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _state(self) -> dict:
        if self.state_file.is_symlink():
            raise CacheError("Estado inválido: link simbólico.")
        try:
            with self.state_file.open(encoding="utf-8") as stream:
                state = json.load(stream)
        except (OSError, ValueError) as exc:
            raise CacheError("Nenhum ciclo válido. Execute 'open' primeiro.") from exc
        if state.get("status") not in ("OPEN", "CLOSED") or not isinstance(state.get("items"), list):
            raise CacheError("Estado do ciclo inválido.")
        return state

    def open(self) -> None:
        with self._locked():
            if self.state_file.exists() or self.state_file.is_symlink():
                raise CacheError("Já existe um ciclo; um novo Input depende da conclusão e limpeza do anterior.")
            self.files.mkdir(mode=0o700, parents=True)
            self.namer_inbox.mkdir(mode=0o700, parents=True)
            _write_json(self.state_file, {"status": "OPEN", "items": []})

    def add(self, paths: list[Path]) -> int:
        if not paths:
            raise CacheError("Informe pelo menos um arquivo.")
        with self._locked():
            state = self._state()
            if state["status"] != "OPEN":
                raise CacheError("A janela de entrada está fechada.")
            for source in paths:
                # No filename or extension is inspected for classification. Every
                # successful submission is a distinct file entry, even if bytes match.
                entry_id = uuid.uuid4().hex
                destination_dir = self.files / entry_id
                destination_dir.mkdir(mode=0o700)
                try:
                    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(descriptor, "rb") as incoming:
                        if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                            raise CacheError(f"Não é um arquivo regular: {source}")
                        name = source.name
                        if name in ("", ".", ".."):
                            raise CacheError("Nome de arquivo inválido.")
                        target = destination_dir / name
                        with target.open("xb") as outgoing:
                            shutil.copyfileobj(incoming, outgoing)
                            outgoing.flush()
                            os.fsync(outgoing.fileno())
                    state["items"].append({"entry": entry_id, "name": name})
                    _write_json(self.state_file, state)
                except (OSError, CacheError):
                    shutil.rmtree(destination_dir)
                    raise
            return len(state["items"])

    def _count(self, state: dict) -> int:
        items = state["items"]
        entries = {item["entry"] for item in items}
        if len(entries) != len(items) or entries != {p.name for p in self.files.iterdir()}:
            raise CacheError("O cache diverge do índice; contagem interrompida.")
        for item in items:
            directory = self.files / item["entry"]
            if not directory.is_dir() or directory.is_symlink():
                raise CacheError("Entrada do cache inválida.")
            children = list(directory.iterdir())
            if len(children) != 1 or children[0].name != item["name"] or children[0].is_symlink() or not children[0].is_file():
                raise CacheError("Arquivo ausente ou inesperado no cache.")
        return len(items)

    def close(self) -> int:
        with self._locked():
            state = self._state()
            n = self._count(state)
            if state["status"] == "OPEN":
                state["status"] = "CLOSED"
                _write_json(self.state_file, state)
            # If a previous attempt stopped after closing, retry the handoff.
            # This is the whole Namer interface for this phase: no names or bytes.
            _write_json(self.namer_inbox / "n.json", {"n": n})
            return n

    def status(self) -> dict:
        with self._locked():
            state = self._state()
            return {"status": state["status"], "n": self._count(state)}
