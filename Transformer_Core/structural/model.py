"""Versioned contracts shared by protocols, Frankenstein and Curadoria."""

from dataclasses import asdict, dataclass, field
import hashlib
import json

SCHEMA = "mimir.structural.v1"
PROVENANCE_SCHEMA = "mimir.provenance.v1"
PIPELINE_VERSION = "0.2.1"


class StructuralError(Exception):
    """A structural batch cannot be safely published or consumed."""


class ProtocolError(StructuralError):
    """A supported protocol could not observe a complete file."""


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 64 * 1024 * 1024
    max_nodes: int = 10000
    max_text_chars: int = 2_000_000
    max_expanded_bytes: int = 128 * 1024 * 1024
    max_pixels: int = 25_000_000

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in asdict(self).values()):
            raise ValueError("Todos os limites devem ser inteiros positivos.")

    def profile(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Unit:
    kind: str
    locator: dict
    properties: dict = field(default_factory=dict)
    # Index in ProtocolResult.units. None means the file root.
    parent: int | None = None


@dataclass
class ProtocolResult:
    route: str
    status: str = "complete"
    reason: str | None = None
    dependencies: dict = field(default_factory=dict)
    units: list[Unit] = field(default_factory=list)

    def add(self, unit: Unit, limits: Limits) -> int:
        if len(self.units) + 1 >= limits.max_nodes:
            raise ProtocolError("node_limit")
        self.units.append(unit)
        return len(self.units) - 1
