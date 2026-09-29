"""Publish complete, checksummed generations; never replace the source bytes."""

import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import av
import numpy as np

from BN1_1.contracts import validate_names
from Transformer_Core.Hot_Hub.hub import HotHub, _digest
from .adapters import Unmodeled, decode


SCHEMA = "mimir.sampled-fields.v1"


class FieldError(Exception):
    pass


@dataclass(frozen=True)
class Limits:
    max_elements: int = 32_000_000
    max_derived_bytes: int = 1024 * 1024 * 1024
    max_frames: int = 100_000


def write_json(path, payload):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


class ArrayWriter:
    def __init__(self, root, limits):
        self.root = root
        self.root.mkdir(mode=0o700)
        self.limits = limits
        self.max_elements = limits.max_elements
        self.max_frames = limits.max_frames
        self.used = self.count = 0

    def array(self, values):
        if values.size > self.max_elements:
            raise Unmodeled("array_limit")
        if self.used + values.nbytes + 256 > self.limits.max_derived_bytes:
            raise Unmodeled("derived_byte_limit")
        name = f"{self.count:08d}.npy"
        path = self.root / name
        with path.open("wb") as stream:
            np.save(stream, values, allow_pickle=False)
            stream.flush()
            os.fsync(stream.fileno())
        self.used += path.stat().st_size
        self.count += 1
        return {"file": name, "dtype": values.dtype.str, "shape": list(values.shape)}


class FieldStore:
    def __init__(self, hub_root: Path, limits: Limits | None = None):
        self.hub_root = hub_root
        self.data = hub_root / "data"
        self.root = hub_root / "representations"
        self.manifest = hub_root / "fields.json"
        self.limits = limits or Limits()

    def verify(self, expected):
        try:
            manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
            if manifest["schema"] != SCHEMA or manifest["sources"] != expected:
                raise FieldError("Manifesto de campos não corresponde ao lote.")
            generation = manifest["generation"]
            if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
                raise FieldError("Geração inválida.")
            root = self.root / generation
            if root.is_symlink() or not root.is_dir():
                raise FieldError("Geração ausente ou inválida.")
            inventory = manifest["files"]
            if HotHub._tree_files(root) != set(inventory):
                raise FieldError("Geração de campos incompleta.")
            for relative, digest in inventory.items():
                if _digest(root / relative) != digest:
                    raise FieldError("Campo alterado após publicação.")
            if set(manifest["records"]) != set(expected):
                raise FieldError("Lista de representações incompleta.")
            for name, relative in manifest["records"].items():
                if relative not in inventory:
                    raise FieldError("Registro fora da geração.")
                record = json.loads((root / relative).read_text(encoding="utf-8"))
                if (record["schema"] != SCHEMA or record["source"]["name"] != name
                        or record["source"]["sha256"] != expected[name]):
                    raise FieldError("Proveniência divergente.")
            return manifest
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FieldError("Manifesto de campos ausente ou inválido.") from exc

    def build(self, expected):
        validate_names(list(expected))
        try:
            return self.verify(expected)
        except FieldError:
            pass
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        generation = uuid.uuid4().hex
        stage = self.root / (".pending-" + generation)
        stage.mkdir(mode=0o700)
        records = {}
        summary = {"decoded": 0, "partial": 0, "opaque": 0}
        try:
            for index, (name, digest) in enumerate(expected.items()):
                source = self.data / name
                if _digest(source) != digest:
                    raise FieldError("Original alterado antes da representação.")
                writer = ArrayWriter(stage / str(index), self.limits)
                record = {"schema": SCHEMA,
                          "source": {"name": name, "sha256": digest, "bytes": source.stat().st_size},
                          "original_retained": True,
                          "byte_field": {"dtype": "|u1", "axis": "byte_index",
                                         "length": source.stat().st_size, "time": "unmodeled",
                                         "storage": "original"}}
                try:
                    decoded = decode(source, writer)
                except (Unmodeled, av.FFmpegError, ValueError, EOFError) as exc:
                    # Partial decoded results must never appear as complete.
                    shutil.rmtree(writer.root)
                    writer.root.mkdir(mode=0o700)
                    decoded = {"adapter": "opaque", "status": "opaque", "fields": [],
                               "reason": str(exc) if isinstance(exc, Unmodeled) else type(exc).__name__}
                if _digest(source) != digest:
                    raise FieldError("Original alterado durante a representação.")
                record.update(decoded)
                summary[record["status"]] += 1
                path = writer.root / "record.json"
                write_json(path, record)
                records[name] = path.relative_to(stage).as_posix()
            inventory = {relative: _digest(stage / relative)
                         for relative in sorted(HotHub._tree_files(stage))}
            target = self.root / generation
            os.replace(stage, target)
            manifest = {"schema": SCHEMA, "generation": generation, "sources": expected,
                        "records": records, "files": inventory, "summary": summary}
            fd, temporary = tempfile.mkstemp(prefix=".fields-", dir=self.hub_root)
            os.close(fd)
            try:
                write_json(Path(temporary), manifest)
                os.replace(temporary, self.manifest)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return self.verify(expected)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
