"""Bounded, versioned data exchange; no credentials in public envelopes."""

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import re

from Transformer_Core.semantic.model import SemanticError
from Transformer_Core.structural.model import fingerprint


class IOError(SemanticError):
    """An input/output operation cannot complete with verified data."""


@dataclass(frozen=True)
class IOLimits:
    max_file_bytes: int = 64 * 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024
    max_sync_files: int = 10000

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in asdict(self).values()):
            raise ValueError("Limites de I/O devem ser inteiros positivos.")


NATIVE_EXPORTS = {
    "application/vnd.google-apps.document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
}


def remote_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value) or value == "root":
        raise IOError("ID do Drive deve ser um ID real de arquivo/pasta.")
    return value


def account_fingerprint(email):
    if not isinstance(email, str) or "@" not in email or len(email) > 320:
        raise IOError("A identidade da conta requer email verificado pelo provedor.")
    return hashlib.sha256(email.strip().casefold().encode()).hexdigest()


def revision(metadata):
    remote_id(metadata["id"])
    if not metadata.get("version") or not metadata.get("modifiedTime"):
        raise IOError("Metadados devem incluir version e modifiedTime.")
    return fingerprint({key: metadata.get(key) for key in ("id", "version", "modifiedTime", "mimeType", "size", "md5Checksum", "sha256Checksum")})


def export_spec(metadata):
    kind = metadata.get("mimeType")
    if kind in NATIVE_EXPORTS:
        return NATIVE_EXPORTS[kind]
    if not isinstance(kind, str) or kind.startswith("application/vnd.google-apps."):
        raise IOError("Pasta, atalho ou formato nativo sem exportação suportada.")
    return None, None


def local_name(metadata):
    name = metadata.get("name")
    if not isinstance(name, str) or not name or len(name.encode()) > 240 or name in {".", ".."} or any(char in name for char in ("/", "\\", "\0")):
        raise IOError("Nome remoto inválido para ingestão local.")
    _, suffix = export_spec(metadata)
    return Path(name).stem + suffix if suffix else name


def verify_bytes(data, metadata, limit):
    if len(data) > limit:
        raise IOError("Arquivo excede max_file_bytes.")
    native = metadata.get("mimeType") in NATIVE_EXPORTS
    if not native and (metadata.get("size") is None or not (metadata.get("sha256Checksum") or metadata.get("md5Checksum"))):
        raise IOError("Arquivo binário requer tamanho e checksum fornecidos pelo Drive.")
    if not native and metadata.get("size") is not None and len(data) != int(metadata["size"]):
        raise IOError("Tamanho recebido diverge do Drive.")
    for key, algorithm in (("sha256Checksum", "sha256"), ("md5Checksum", "md5")):
        if not native and metadata.get(key) and hashlib.new(algorithm, data).hexdigest() != metadata[key]:
            raise IOError("Checksum recebido diverge do Drive.")
    return hashlib.sha256(data).hexdigest()
