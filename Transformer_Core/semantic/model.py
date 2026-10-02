"""Strict, content-addressed contracts for evidence, G_M and G_S."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re
import unicodedata

from Transformer_Core.structural.model import fingerprint
from mimir_version import __version__

VERSION = __version__
COLLECTIONS = {
    "upstreams": ("mimir.upstream.v1", "upstream"),
    "occurrences": ("mimir.source-occurrence.v1", "occurrence"),
    "anchors": ("mimir.evidence-anchor.v1", "anchor"),
    "bindings": ("mimir.evidence-binding.v1", "binding"),
    "observations": ("mimir.observation.v1", "observation"),
    "mentions": ("mimir.mention.v1", "mention"),
    "entities": ("mimir.entity.v1", "entity"),
    "resolutions": ("mimir.entity-resolution.v1", "resolution"),
    "events": ("mimir.event.v1", "event"),
    "propositions": ("mimir.proposition.v1", "prop"),
    "assertions": ("mimir.assertion.v1", "assert"),
    "inference_runs": ("mimir.inference-run.v1", "run"),
    "relations": ("mimir.semantic-relation.v1", "relation"),
    "episodes": ("mimir.episode.v1", "episode"),
    "reflections": ("mimir.reflection.v1", "reflection"),
    "reports": ("mimir.extraction-report.v1", "report"),
}


class SemanticError(Exception):
    """Semantic evidence, publication or query cannot be safely completed."""


@dataclass(frozen=True)
class SemanticLimits:
    max_records: int = 200000
    max_text_chars: int = 2000000
    max_assertions: int = 20000
    worker_timeout: int = 60
    worker_memory_mb: int = 2048
    max_output_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in asdict(self).values()):
            raise ValueError("Limites semânticos devem ser inteiros positivos.")

    def profile(self):
        return asdict(self)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def timestamp(value: str) -> str:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone")
        return result.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (AttributeError, ValueError, TypeError) as exc:
        raise SemanticError("Timestamp deve ser ISO 8601 com fuso horário.") from exc


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def record(collection: str, **fields) -> dict:
    schema, prefix = COLLECTIONS[collection]
    value = {"schema": schema, **fields}
    identity = {k: v for k, v in value.items() if collection != "inference_runs" or k != "output_hash"}
    return {"id": prefix + ":" + fingerprint(identity), **value}


def check_record(collection: str, value: dict) -> None:
    schema, prefix = COLLECTIONS[collection]
    identity = {k: v for k, v in value.items() if k != "id" and (collection != "inference_runs" or k != "output_hash")}
    if value.get("schema") != schema or value.get("id") != prefix + ":" + fingerprint(identity):
        raise SemanticError(f"Identidade ou schema de {collection} inválido.")


def content_id(digest: str) -> str:
    if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
        raise SemanticError("Identidade de conteúdo inválida.")
    return "content:" + digest


class Ledger:
    def __init__(self, collections=None):
        self.data = {name: {} for name in COLLECTIONS}
        for name, rows in (collections or {}).items():
            for row in rows:
                self.add(name, row)

    def add(self, collection: str, value: dict) -> str:
        check_record(collection, value)
        existing = self.data[collection].get(value["id"])
        if existing is not None and existing != value:
            raise SemanticError("Colisão de identidade canônica.")
        self.data[collection][value["id"]] = value
        return value["id"]

    def put(self, collection: str, **fields) -> str:
        return self.add(collection, record(collection, **fields))

    def rows(self, collection: str):
        return [self.data[collection][key] for key in sorted(self.data[collection])]

    def get(self, collection: str, identity: str) -> dict:
        try:
            return self.data[collection][identity]
        except KeyError as exc:
            raise SemanticError(f"Referência de {collection} não encontrada.") from exc

    def counts(self):
        return {name: len(rows) for name, rows in self.data.items()}
