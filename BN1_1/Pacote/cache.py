"""Pacote: single-cycle, format-agnostic staging of real files."""

from __future__ import annotations

import fcntl
import hashlib
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
from BN1_1.contracts import validate_names
from BN1_1.BBN1_1.buffer import BBN1_1, BufferError
from Transformer_Core.Hot_Hub.hub import HotHub, HubError, _digest


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
        if state.get("status") not in (
            "OPEN", "CLOSED", "NAMING", "NAMED", "STAGED",
            "CHUNKING", "HUB_VERIFIED", "HUB_READY",
        ) or not isinstance(state.get("items"), list):
            raise CacheError("Estado do ciclo inválido.")
        if state.get("layout_version") != 3:
            raise CacheError("Ciclo do layout antigo preservado. Use outro MIMIR_RUNTIME_DIR e reenvie os originais; migração automática não é suportada.")
        return state

    def open(self) -> None:
        with self._locked():
            if self.state_file.exists() or self.state_file.is_symlink():
                raise CacheError("Já existe um ciclo; um novo Input depende da conclusão e limpeza do anterior.")
            self.files.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.namer_inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
            if any(self.files.iterdir()) or any(self.namer_inbox.iterdir()):
                raise CacheError("Há dados de um início incompleto; escolha outro diretório de execução.")
            _write_json(self.state_file, {"layout_version": 3, "status": "OPEN", "items": []})

    def add(self, paths: list[Path]) -> int:
        if not paths:
            raise CacheError("Informe pelo menos um arquivo.")
        with self._locked():
            state = self._state()
            if state["status"] != "OPEN":
                raise CacheError("A janela de entrada está fechada.")
            self._recover_open(state)
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
                        content_hash = hashlib.sha256()
                        with target.open("xb") as outgoing:
                            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                                outgoing.write(chunk)
                                content_hash.update(chunk)
                            outgoing.flush()
                            os.fsync(outgoing.fileno())
                    state["items"].append({
                        "entry": entry_id, "name": name,
                        "sha256_content": content_hash.hexdigest(),
                    })
                    _write_json(self.state_file, state)
                except (OSError, CacheError):
                    shutil.rmtree(destination_dir)
                    raise
            return len(state["items"])

    def _recover_open(self, state: dict) -> None:
        """Discard only incomplete, unindexed copies from an interrupted add."""
        if state["status"] != "OPEN":
            return
        indexed = {item["entry"] for item in state["items"]}
        for entry in self.files.iterdir():
            if entry.name not in indexed:
                if not re.fullmatch(r"[0-9a-f]{32}", entry.name) or entry.is_symlink() or not entry.is_dir():
                    raise CacheError("Entrada inesperada no cache; recuperação interrompida.")
                shutil.rmtree(entry)

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
            expected = {item["renamed"]} if state["status"] in (
                "NAMED", "STAGED", "CHUNKING", "HUB_VERIFIED", "HUB_READY"
            ) else {item["name"]}
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

        if state["status"] in ("NAMED", "STAGED", "CHUNKING", "HUB_VERIFIED", "HUB_READY"):
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

    def supply(self, filename: str, target: Path) -> tuple[str, str]:
        """Provide one named file on BBN1_1 request."""
        state = self._state()
        matches = [item for item in state["items"] if item.get("renamed") == filename]
        if len(matches) != 1 or state["status"] not in (
            "NAMED", "STAGED", "CHUNKING", "HUB_VERIFIED", "HUB_READY"
        ):
            raise CacheError("Arquivo requisitado pelo BBN1_1 não está disponível.")
        source = self.files / matches[0]["entry"] / filename
        content_hash = hashlib.sha256()
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as incoming, target.open("wb") as outgoing:
            if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                raise CacheError("Entrada inválida no cache.")
            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                content_hash.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        expected_hash = matches[0].get("sha256_content")
        if expected_hash is not None and content_hash.hexdigest() != expected_hash:
            raise CacheError("O conteúdo no Pacote mudou desde a entrada.")
        return hashlib.sha256(filename.encode("utf-8")).hexdigest(), content_hash.hexdigest()

    def _expected_sources(self, state: dict) -> dict[str, str]:
        """Verify the Pacote copy and describe the exact source inventory."""
        expected = {}
        for item in state["items"]:
            filename = item["renamed"]
            validate_names([filename])
            relative = filename
            digest = _digest(self.files / item["entry"] / filename)
            if item.get("sha256_content") not in (None, digest):
                raise CacheError("O conteúdo do Pacote mudou desde a entrada.")
            if relative in expected:
                raise CacheError("Dois arquivos disputam o mesmo destino no Hot Hub.")
            expected[relative] = digest
        return expected

    def close(self) -> int:
        with self._locked():
            state = self._state()
            self._recover_open(state)
            n = self._count(state)
            if state["status"] == "HUB_READY":
                expected = self._expected_sources(state)
                try:
                    hub = HotHub(self.runtime)
                    state["representations"] = hub.verify(expected)["summary"]
                    _write_json(self.state_file, state)
                    return n
                except HubError:
                    # Pacote is still intact: rebuild the intermediate copy,
                    # then repair the hub before releasing BBN1_1 again.
                    state["status"] = "STAGED"
                    _write_json(self.state_file, state)
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
            try:
                stored = BBN1_1(self.runtime).store([item["renamed"] for item in state["items"]], self)
            except (ValueError, BufferError) as exc:
                raise CacheError(str(exc)) from exc
            if stored != n:
                raise CacheError("O BBN1_1 não recebeu todos os arquivos.")
            if state["status"] != "STAGED":
                state["status"] = "STAGED"
                _write_json(self.state_file, state)
            expected = self._expected_sources(state)
            hub = HotHub(self.runtime)
            bbn = BBN1_1(self.runtime)
            state["status"] = "CHUNKING"
            _write_json(self.state_file, state)
            manifest = hub.build(bbn.root, expected)
            state["status"] = "HUB_VERIFIED"
            _write_json(self.state_file, state)
            state["representations"] = manifest["summary"]
            bbn.purge_verified(expected, hub, lambda: self._expected_sources(state))
            state["status"] = "HUB_READY"
            _write_json(self.state_file, state)
            return n

    def status(self) -> dict:
        with self._locked():
            state = self._state()
            result = {"status": state["status"], "n": self._count(state)}
            if "representations" in state:
                result["representations"] = state["representations"]
            transform_state = self.runtime / "Transformer_Core/Structural/state.json"
            if transform_state.exists():
                from Transformer_Core.Hot_Hub.hub import _open_regular
                with _open_regular(transform_state) as stream:
                    result["transform"] = json.load(stream)
            return result

    def transform(self, limits=None, strict: bool = False, force: bool = False) -> dict:
        """Finish byte ingestion, then advance under the existing cycle lock."""
        from Transformer_Core.structural.pipeline import StructuralPipeline
        self.close()
        with self._locked():
            state = self._state()
            if state["status"] != "HUB_READY":
                raise CacheError("Transformação requer Hot Hub pronto.")
            return StructuralPipeline(self.runtime).build(self._expected_sources(state), limits, strict, force)

    def verify_transform(self) -> dict:
        from Transformer_Core.structural.pipeline import StructuralPipeline
        with self._locked():
            state = self._state()
            if state["status"] != "HUB_READY":
                raise CacheError("Transformação requer Hot Hub pronto.")
            self._count(state)
            return StructuralPipeline(self.runtime).verify(self._expected_sources(state))

    def inspect_transform(self, name: str | None = None):
        from Transformer_Core.structural.pipeline import StructuralPipeline
        with self._locked():
            state = self._state()
            if state["status"] != "HUB_READY":
                raise CacheError("Transformação requer Hot Hub pronto.")
            self._count(state)
            expected = self._expected_sources(state)
            if name is not None and name not in expected:
                raise CacheError("Nome canônico não encontrado no ciclo.")
            documents = StructuralPipeline(self.runtime).documents(expected)
            if name is not None:
                return next(document for source, document in documents if source == name)
            return [{"source": source, "protocol": document["protocol"],
                     "nodes": len(document["nodes"]), "edges": len(document["provenance"]["edges"])}
                    for source, document in documents]
