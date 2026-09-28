"""Pacote: single-cycle, format-agnostic staging of real files."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from BN1_1.Namer.namer import Namer, NamerError


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
        if state.get("status") not in ("OPEN", "CLOSED", "NAMING", "NAMED") or not isinstance(state.get("items"), list):
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
            expected = {item["renamed"]} if state["status"] == "NAMED" else {item["name"]}
            if state["status"] == "NAMING":
                expected.add(item["renamed"])
            if len(children) != 1 or children[0].name not in expected or children[0].is_symlink() or not children[0].is_file():
                raise CacheError("Arquivo ausente ou inesperado no cache.")
        return len(items)

    def _rename(self, state: dict, stems: list[str]) -> None:
        n = len(state["items"])
        if len(stems) != n or len(set(stems)) != n:
            raise CacheError("Lista de stems do Namer inválida.")
        if n:
            match = re.fullmatch(r"1_([A-Za-z0-9]{3})", stems[0])
            if match is None or stems != [f"{i}_{match.group(1)}" for i in range(1, n + 1)]:
                raise CacheError("Índices ou ID do Namer inválidos.")

        if state["status"] == "CLOSED":
            # Store the complete random bijection before moving anything, so
            # interrupted renames can resume without drawing new assignments.
            shuffled = stems.copy()
            secrets.SystemRandom().shuffle(shuffled)
            for item, stem in zip(state["items"], shuffled):
                item["renamed"] = stem + Path(item["name"]).suffix
            state["status"] = "NAMING"
            _write_json(self.state_file, state)

        if state["status"] == "NAMED":
            assigned_stems = {
                item["renamed"][:-len(Path(item["name"]).suffix)]
                if Path(item["name"]).suffix else item["renamed"]
                for item in state["items"]
            }
            if assigned_stems != set(stems):
                raise CacheError("Resposta do Namer diverge da nomeação existente.")
            return

        assigned_stems = {item["renamed"][:-len(Path(item["name"]).suffix)] if Path(item["name"]).suffix else item["renamed"] for item in state["items"]}
        if assigned_stems != set(stems):
            raise CacheError("Atribuições do Pacote divergem da resposta do Namer.")
        for item in state["items"]:
            directory = self.files / item["entry"]
            original = directory / item["name"]
            renamed = directory / item["renamed"]
            if renamed == original:
                continue
            if renamed.exists():
                if original.exists():
                    raise CacheError("Colisão durante a renomeação.")
                continue  # Already renamed before an interruption.
            if not original.is_file() or original.is_symlink():
                raise CacheError("Arquivo original indisponível para renomeação.")
            original.rename(renamed)
        state["status"] = "NAMED"
        _write_json(self.state_file, state)

    def close(self) -> int:
        with self._locked():
            state = self._state()
            n = self._count(state)
            if state["status"] == "OPEN":
                state["status"] = "CLOSED"
                _write_json(self.state_file, state)
            # The same lock covers the Namer reply and the random assignment.
            _write_json(self.namer_inbox / "n.json", {"n": n})
            try:
                stems = Namer(self.runtime).respond()
            except (NamerError, ValueError, TypeError) as exc:
                raise CacheError(str(exc)) from exc
            self._rename(state, stems)
            return n

    def status(self) -> dict:
        with self._locked():
            state = self._state()
            return {"status": state["status"], "n": self._count(state)}
