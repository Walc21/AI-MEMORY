"""Canonical generations, append-only history, ACL and optional signatures."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from BN1_2.buffer import ensure_directories, write_atomic
from Transformer_Core.Hot_Hub.hub import HotHub, _digest, _open_regular, _sync_directory
from Transformer_Core.structural.model import canonical, fingerprint
from .graph import graph_projection
from .model import COLLECTIONS, Ledger, SemanticError, now
from .validation import validate


def read_json(path):
    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise SemanticError("Chave JSON duplicada.")
            output[key] = value
        return output
    with _open_regular(path) as stream:
        return json.load(stream, object_pairs_hook=pairs)


def manifest_fingerprint(manifest):
    return fingerprint({k: v for k, v in manifest.items() if k not in {"fingerprint", "signature"}})


class Store:
    def __init__(self, root: Path, namespace="default"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", namespace):
            raise SemanticError("Namespace deve ser um nome local simples.")
        self.base = Path(root).absolute()
        self.namespace = namespace
        self.root = self.base / namespace
        self.generations = self.root / "generations"
        self.sources = self.root / "sources"
        self.indexes = self.root / "indexes"
        self.manifest = self.root / "manifest.json"
        self.policy = self.root / "policy.json"

    def _layout(self):
        for target in (self.generations, self.sources, self.indexes):
            ensure_directories(self.base, target)
        if not self.policy.exists():
            write_atomic(self.policy, {"schema": "mimir.acl.v1", "owner_uid": os.getuid(),
                                      "readers": [], "writers": [], "signing_public_key": None, "signed_after": None})

    def authorize(self, write=False):
        policy = read_json(self.policy)
        if set(policy) != {"schema", "owner_uid", "readers", "writers", "signing_public_key", "signed_after"} or policy["schema"] != "mimir.acl.v1":
            raise SemanticError("Política de acesso inválida.")
        if type(policy["owner_uid"]) is not int or policy["owner_uid"] < 0 or any(not isinstance(policy[key], list) or any(type(uid) is not int or uid < 0 for uid in policy[key]) for key in ("readers", "writers")):
            raise SemanticError("UIDs da política de acesso inválidos.")
        uid = os.getuid()
        allowed = {policy["owner_uid"], *policy["writers"], *([] if write else policy["readers"])}
        if uid not in allowed:
            raise SemanticError("Usuário POSIX sem acesso ao namespace.")
        return policy

    @contextmanager
    def locked(self, write=False):
        self._layout()
        descriptor = os.open(self.root / "memory.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise SemanticError("Bloqueio de memória inválido.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self.authorize(write)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def source_path(self, identity):
        if not re.fullmatch(r"content:[0-9a-f]{64}", identity):
            raise SemanticError("ID de bytes inválido.")
        return self.sources / (identity.removeprefix("content:") + ".bin")

    def store_source(self, data: bytes, identity: str):
        path = self.source_path(identity)
        digest = identity.removeprefix("content:")
        if hashlib.sha256(data).hexdigest() != digest:
            raise SemanticError("Bytes não correspondem ao content_id.")
        if path.exists() or path.is_symlink():
            if path.is_symlink() or _digest(path) != digest:
                raise SemanticError("Arquivo canônico já existente está corrompido.")
            return
        descriptor, temporary = tempfile.mkstemp(prefix=".incoming-", dir=self.sources)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _sync_directory(self.sources)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _check_manifest(self, manifest):
        fields = {"schema", "generation", "parent", "upstream", "profile", "operation", "timestamp",
                  "collections", "projections", "contents", "summary", "fingerprint", "signature"}
        if set(manifest) != fields or manifest["schema"] != "mimir.semantic-generation.v1" or not re.fullmatch(r"[0-9a-f]{32}", manifest["generation"]):
            raise SemanticError("Manifesto semântico fora do contrato.")
        if manifest["fingerprint"] != manifest_fingerprint(manifest):
            raise SemanticError("Fingerprint da geração divergente.")
        policy = self.authorize()
        signature = manifest["signature"]
        if policy["signed_after"] is not None and manifest["timestamp"] >= policy["signed_after"] and signature is None:
            raise SemanticError("Namespace com assinatura obrigatória: informe a chave configurada.")
        if signature is not None:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            if set(signature) != {"algorithm", "public_key", "value"} or signature["algorithm"] != "ed25519" or signature["public_key"] != policy["signing_public_key"]:
                raise SemanticError("Assinatura sem chave pública confiável no namespace.")
            try:
                Ed25519PublicKey.from_public_bytes(bytes.fromhex(signature["public_key"])).verify(bytes.fromhex(signature["value"]), bytes.fromhex(manifest["fingerprint"]))
            except Exception as exc:
                raise SemanticError("Assinatura inválida.") from exc

    def _read_generation(self, manifest):
        self._check_manifest(manifest)
        folder = self.generations / manifest["generation"]
        expected = {"manifest.json", "gm.json", "gs.json", *(name + ".jsonl" for name in COLLECTIONS)}
        if HotHub._tree_files(folder) != expected:
            raise SemanticError("Geração semântica incompleta, extra ou com links simbólicos.")
        if read_json(folder / "manifest.json") != manifest:
            raise SemanticError("Manifesto publicado diverge da geração arquivada.")
        if set(manifest["collections"]) != set(COLLECTIONS) or set(manifest["projections"]) != {"gm", "gs"}:
            raise SemanticError("Inventário semântico inválido.")
        collections = {}
        for name in COLLECTIONS:
            descriptor = manifest["collections"][name]
            path = folder / (name + ".jsonl")
            if descriptor["file"] != path.name or _digest(path) != descriptor["sha256"]:
                raise SemanticError("Registro semântico alterado.")
            with _open_regular(path) as stream:
                rows = [json.loads(line) for line in stream]
            if len(rows) != descriptor["count"] or len({row["id"] for row in rows}) != len(rows):
                raise SemanticError("Contagem ou identidade repetida no ledger.")
            collections[name] = rows
        ledger = Ledger(collections)
        for name, graph in zip(("gm", "gs"), graph_projection(ledger)):
            path = folder / (name + ".json")
            if manifest["projections"][name]["file"] != path.name or _digest(path) != manifest["projections"][name]["sha256"] or read_json(path) != graph:
                raise SemanticError("Projeção de grafo divergente.")
        if manifest["summary"] != ledger.counts():
            raise SemanticError("Resumo do ledger divergente.")
        return ledger

    def load(self, verify_sources=True):
        if not self.manifest.exists():
            if self.manifest.is_symlink():
                raise SemanticError("Manifesto é um link simbólico.")
            return None, Ledger()
        try:
            manifest = read_json(self.manifest)
            ledger = self._read_generation(manifest)
            current, child = manifest, ledger
            seen = {manifest["generation"]}
            while current["parent"] is not None:
                parent = current["parent"]
                if set(parent) != {"generation", "fingerprint"} or not re.fullmatch(r"[0-9a-f]{32}", parent["generation"]) or parent["generation"] in seen:
                    raise SemanticError("Cadeia de gerações inválida ou cíclica.")
                previous = read_json(self.generations / parent["generation"] / "manifest.json")
                if previous["fingerprint"] != parent["fingerprint"]:
                    raise SemanticError("Histórico da memória foi alterado.")
                old = self._read_generation(previous)
                for name in COLLECTIONS:
                    if any(child.data[name].get(key) != value for key, value in old.data[name].items()):
                        raise SemanticError("Geração removeu ou reescreveu memória anterior.")
                child, current = old, previous
                seen.add(previous["generation"])
            paths = {row["content_id"]: self.source_path(row["content_id"]) for row in ledger.rows("occurrences")}
            if sorted(paths) != manifest["contents"]:
                raise SemanticError("Inventário de conteúdo divergente.")
            if verify_sources:
                for identity, path in paths.items():
                    if _digest(path) != identity.removeprefix("content:"):
                        raise SemanticError("Bytes canônicos corrompidos.")
            validate(ledger, paths if verify_sources else None)
            upstream = ledger.get("upstreams", manifest["upstream"]["upstream_id"])
            if manifest["upstream"] != {"upstream_id": upstream["id"], "generation": upstream["bn_manifest"]["generation"], "fingerprint": fingerprint(upstream["bn_manifest"])}:
                raise SemanticError("Geração não depende do BN1_2 declarado.")
            return manifest, ledger
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise SemanticError("Memória semântica inválida ou corrompida.") from exc

    def publish(self, ledger: Ledger, upstream: dict, profile: dict, operation="ingest", signing_key=None):
        parent, old = self.load()
        for name in COLLECTIONS:
            if any(ledger.data[name].get(key) != value for key, value in old.data[name].items()):
                raise SemanticError("Publicação precisa preservar o ledger anterior.")
        paths = {row["content_id"]: self.source_path(row["content_id"]) for row in ledger.rows("occurrences")}
        validate(ledger, paths)
        generation = uuid.uuid4().hex
        folder = self.generations / generation
        folder.mkdir(mode=0o700)
        collections, projections = {}, {}
        for name in COLLECTIONS:
            path = folder / (name + ".jsonl")
            with path.open("xb") as stream:
                for row in ledger.rows(name):
                    stream.write(canonical(row) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            collections[name] = {"file": path.name, "sha256": _digest(path), "count": len(ledger.data[name])}
        for name, graph in zip(("gm", "gs"), graph_projection(ledger)):
            path = folder / (name + ".json")
            write_atomic(path, graph)
            projections[name] = {"file": path.name, "sha256": _digest(path)}
        manifest = {"schema": "mimir.semantic-generation.v1", "generation": generation,
                    "parent": {"generation": parent["generation"], "fingerprint": parent["fingerprint"]} if parent else None,
                    "upstream": {"upstream_id": upstream["id"], "generation": upstream["bn_manifest"]["generation"], "fingerprint": fingerprint(upstream["bn_manifest"])},
                    "profile": profile, "operation": operation, "timestamp": now(), "collections": collections,
                    "projections": projections, "contents": sorted(paths), "summary": ledger.counts(),
                    "fingerprint": None, "signature": None}
        manifest["fingerprint"] = manifest_fingerprint(manifest)
        if signing_key:
            self._sign(manifest, signing_key)
        write_atomic(folder / "manifest.json", manifest)
        self._read_generation(manifest)
        _sync_directory(folder)
        _sync_directory(self.generations)
        write_atomic(self.manifest, manifest)
        return manifest

    def _sign(self, manifest, signing_key):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        with _open_regular(Path(signing_key)) as stream:
            key = serialization.load_pem_private_key(stream.read(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise SemanticError("A chave de assinatura deve ser Ed25519.")
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        policy = self.authorize(write=True)
        if policy["signing_public_key"] not in {None, public}:
            raise SemanticError("Chave diferente da âncora de confiança configurada.")
        if policy["signing_public_key"] is None:
            if policy["owner_uid"] != os.getuid():
                raise SemanticError("Somente o proprietário pode configurar assinatura.")
            write_atomic(self.policy, {**policy, "signing_public_key": public, "signed_after": manifest["timestamp"]})
        manifest["signature"] = {"algorithm": "ed25519", "public_key": public,
                                 "value": key.sign(bytes.fromhex(manifest["fingerprint"])).hex()}
