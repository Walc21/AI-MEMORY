"""Hot Hub → Route Hub → protocols → Frankenstein → Curadoria/G_P → BN1_2."""

import json
import base64
from pathlib import Path

from BN1_2.buffer import BBN1_2, HANDOFF_SCHEMA, ensure_directories, source_descriptor, write_atomic
from Transformer_Core.Hot_Hub.hub import HotHub, _open_regular
from .curator import assemble
from .model import Limits, PIPELINE_VERSION, SCHEMA, StructuralError, ProtocolError, ProtocolResult, Unit, fingerprint
from .protocols import dependency_versions


class StructuralPipeline:
    """Caller owns the Pacote cycle lock, including downstream publication."""
    def __init__(self, runtime: Path):
        self.runtime = runtime
        self.hub = HotHub(runtime)
        self.buffer = BBN1_2(runtime)
        self.state_file = runtime / "Transformer_Core/Structural/state.json"

    def _state(self, status: str, **details) -> None:
        ensure_directories(self.runtime, self.state_file.parent)
        write_atomic(self.state_file, {"schema": "mimir.transform-state.v1", "status": status, **details})

    def build(self, expected: dict[str, str], limits: Limits | None = None, strict: bool = False, force: bool = False) -> dict:
        limits = limits or Limits()
        snapshot = self.hub.verify(expected)
        profile = {"pipeline_version": PIPELINE_VERSION, "limits": limits.profile(),
                   "strict": strict, "dependencies": dependency_versions()}
        if not force and self.buffer.manifest.exists():
            try:
                previous = self.buffer.verify(snapshot)
                if previous["profile"] == profile:
                    self._state("BN1_2_READY", generation=previous["generation"], summary=previous["summary"])
                    return previous
            except StructuralError:
                pass  # Intact upstream bytes permit rebuilding this derivative.
        self._state("TRANSFORMING", upstream=snapshot["generation"])
        try:
            generation, folder = self.buffer.begin()
            records = {}
            counts = {"files": 0, "nodes": 0, "edges": 0, "complete": 0, "opaque": 0}
            for position, name in enumerate(sorted(expected)):
                hub_path = self.hub.generations / snapshot["generation"] / snapshot["records"][name]["file"]
                with _open_regular(hub_path) as stream:
                    length = json.loads(stream.readline())["byte_length"]
                if length > limits.max_file_bytes:
                    raise StructuralError(f"{name}: arquivo excede max_file_bytes.")
                vector = self.hub.reconstruct_snapshot(name, snapshot)
                source = source_descriptor(snapshot, name, len(vector))
                from Transformer_Core.semantic.worker import execute
                from Transformer_Core.semantic.model import SemanticLimits, SemanticError
                try:
                    observed = execute({"mode": "structural", "data": base64.b64encode(vector).decode(),
                                        "filename": name, "limits": limits.profile(), "strict": strict}, SemanticLimits())
                    if set(observed) != {"route", "status", "reason", "dependencies", "units"}:
                        raise StructuralError("Worker estrutural retornou contrato inválido.")
                    result = ProtocolResult(observed["route"], observed["status"], observed["reason"],
                                           observed["dependencies"], [Unit(**unit) for unit in observed["units"]])
                except SemanticError as exc:
                    if str(exc).startswith("ProtocolError:"):
                        raise ProtocolError(str(exc).partition(":")[2].strip()) from exc
                    raise StructuralError(str(exc)) from exc
                document = assemble(source, result)
                del vector
                records[name] = self.buffer.store_document(folder, position, document)
                for key, value in self.buffer.summary([document]).items():
                    counts[key] += value
            manifest = {"schema": HANDOFF_SCHEMA, "representation_schema": SCHEMA,
                        "generation": generation, "upstream": {"generation": snapshot["generation"],
                        "fingerprint": fingerprint(snapshot)}, "profile": profile,
                        "records": records, "summary": counts}
            # Prevent publication if the upstream generation changed mid-build.
            if self.hub.verify(expected) != snapshot:
                raise StructuralError("Hot Hub mudou durante a transformação.")
            self.buffer.publish(manifest, snapshot)
            self._state("BN1_2_READY", generation=generation, summary=counts)
            return manifest
        except Exception:
            self._state("FAILED", upstream=snapshot["generation"])
            raise

    def verify(self, expected: dict[str, str]) -> dict:
        return self.buffer.verify(self.hub.verify(expected))

    def documents(self, expected: dict[str, str]):
        yield from self.buffer.documents(self.hub.verify(expected))
