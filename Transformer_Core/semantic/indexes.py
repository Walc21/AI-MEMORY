"""Disposable SQLite FTS5/entity/temporal/vector projections of the ledger."""

import hashlib
from importlib import metadata
import json
import math
from pathlib import Path
import re
import sqlite3
import struct
import tempfile

from BN1_2.buffer import ensure_directories, write_atomic
from Transformer_Core.Hot_Hub.hub import _digest
from Transformer_Core.structural.model import fingerprint
from .evidence import node_for, reference, texts
from .model import SemanticError, normalized

STOP = {"a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "em", "um", "uma", "na", "no", "que", "qual", "quem", "quando", "onde", "como", "the", "a", "an", "of", "in", "at", "is", "and", "to", "who", "what", "when", "where", "how", "was"}


def tokens(text):
    return [term for term in re.findall(r"\w+", normalized(text)) if term not in STOP]


class VectorIndex:
    def __init__(self, model=None, revision=None):
        self.model = None
        self.dimension = 256
        if model:
            from sentence_transformers import SentenceTransformer
            path = Path(model)
            if not path.is_dir() and (not revision or not re.fullmatch(r"[0-9a-f]{40}", revision)):
                raise SemanticError("Modelo remoto requer revisão imutável de 40 caracteres.")
            self.model = SentenceTransformer(model, revision=revision, local_files_only=True, device="cpu")
            self.dimension = self.model.get_sentence_embedding_dimension()
            if path.is_dir():
                digest = hashlib.sha256()
                for file in sorted(path.rglob("*")):
                    if file.is_symlink():
                        raise SemanticError("Modelo local não pode conter symlinks.")
                    if file.is_file():
                        digest.update(file.relative_to(path).as_posix().encode())
                        digest.update(bytes.fromhex(_digest(file)))
                revision = digest.hexdigest()
            self.profile = {"model": model, "revision": revision, "dimension": self.dimension,
                            "implementation": "sentence-transformers", "version": metadata.version("sentence-transformers"), "normalization": "l2"}
        else:
            self.profile = {"model": "mimir-hash-subword", "revision": "1", "dimension": self.dimension,
                            "implementation": "stdlib-feature-hashing", "version": "1", "normalization": "l2"}

    def encode(self, text):
        if self.model:
            return [float(value) for value in self.model.encode(text, normalize_embeddings=True)]
        vector = [0.0] * self.dimension
        for term in tokens(text):
            features = [("w:" + term, 2.0)]
            bounded = "^" + term + "$"
            features += [("c:" + bounded[index:index + 3], 0.5) for index in range(max(0, len(bounded) - 2))]
            for feature, weight in features:
                digest = hashlib.sha256(feature.encode()).digest()
                vector[int.from_bytes(digest[:4], "big") % self.dimension] += weight * (1 if digest[4] & 1 else -1)
        magnitude = math.sqrt(sum(value * value for value in vector)) or 1
        return [value / magnitude for value in vector]


def passages(ledger, chunk_size=2000, overlap=200):
    assertions = {}
    for row in ledger.rows("assertions"):
        for ev in row["evidence"]:
            assertions.setdefault((ev["binding_id"], ev["property"]), []).append((row["id"], ev["char_start"], ev["char_end"]))
    known_at = {}
    for run in ledger.rows("inference_runs"):
        key = run["input_generation"]
        known_at[key] = min(known_at.get(key, run["timestamp"]), run["timestamp"])
    observations = {}
    for row in ledger.rows("observations"):
        observations.setdefault(row["binding_id"], []).append(row)
    for binding in ledger.rows("bindings"):
        occurrence, node = node_for(ledger, binding["id"])
        upstream = ledger.get("upstreams", occurrence["upstream_id"])
        transaction_time = known_at[upstream["bn_manifest"]["generation"]]
        values = list(texts(node))
        values.extend(("observation:" + row["id"], row["text"]) for row in observations.get(binding["id"], []))
        for prop, text in values:
            known = transaction_time
            if prop.startswith("observation:"):
                observation = ledger.get("observations", prop.removeprefix("observation:"))
                known = ledger.get("inference_runs", observation["inference_run_id"])["timestamp"]
            start = 0
            while start < len(text):
                end = min(start + chunk_size, len(text))
                ev = reference(ledger, binding["id"], prop, text, start, end)
                yield {"id": "passage:" + fingerprint(ev), "text": ev["quote"], "evidence": ev,
                       "transaction_time": known, "assertion_ids": [key for key, lo, hi in assertions.get((binding["id"], prop), []) if lo < end and hi > start],
                       "content_id": occurrence["content_id"]}
                if end == len(text):
                    break
                start = end - overlap


class Index:
    def __init__(self, store, manifest, ledger, model=None, revision=None, force=False):
        self.vector = VectorIndex(model, revision)
        self.profile = {"schema": "mimir.index-profile.v1", "semantic_generation": manifest["generation"],
            "semantic_fingerprint": manifest["fingerprint"], "embedding": self.vector.profile,
            "chunking_profile": {"chars": 2000, "overlap": 200}, "index_algorithm": "sqlite-fts5+cosine+rrf+ppr",
            "index_version": 1, "sqlite_version": sqlite3.sqlite_version}
        self.root = store.indexes / manifest["generation"] / fingerprint(self.profile)
        self.path = self.root / "catalog.sqlite3"
        self.manifest = self.root / "manifest.json"
        ensure_directories(store.base, self.root)
        rebuild = True
        if self.manifest.exists() and not self.manifest.is_symlink() and self.path.exists() and not self.path.is_symlink():
            from .storage import read_json
            try:
                value = read_json(self.manifest)
                rebuild = value.get("profile") != self.profile or value.get("sha256") != _digest(self.path)
            except (ValueError, OSError, SemanticError):
                rebuild = True
        if rebuild or force:
            self.build(ledger)

    def build(self, ledger):
        if self.path.is_symlink() or self.manifest.is_symlink():
            raise SemanticError("Índice não pode ser link simbólico.")
        import os
        descriptor, temporary = tempfile.mkstemp(prefix=".catalog-", suffix=".sqlite3", dir=self.root)
        os.close(descriptor)
        try:
            with sqlite3.connect(temporary) as connection:
                connection.executescript("""
                    CREATE TABLE passages(id TEXT PRIMARY KEY,text TEXT NOT NULL,evidence TEXT NOT NULL,
                      transaction_time TEXT NOT NULL,assertion_ids TEXT NOT NULL,content_id TEXT NOT NULL,vector BLOB NOT NULL);
                    CREATE VIRTUAL TABLE lexical USING fts5(id UNINDEXED,text,tokenize='unicode61 remove_diacritics 2');
                    CREATE TABLE entities(id TEXT PRIMARY KEY,name TEXT NOT NULL);
                    CREATE TABLE adjacency(subject TEXT,predicate TEXT,object TEXT);
                    CREATE INDEX known_at ON passages(transaction_time);
                    CREATE INDEX entity_name ON entities(name);
                """)
                count = 0
                for row in passages(ledger):
                    vector = self.vector.encode(row["text"])
                    if len(vector) != self.vector.dimension or any(not math.isfinite(value) for value in vector):
                        raise SemanticError("Embedding fora do perfil.")
                    connection.execute("INSERT INTO passages VALUES(?,?,?,?,?,?,?)", (row["id"], row["text"], json.dumps(row["evidence"]),
                        row["transaction_time"], json.dumps(row["assertion_ids"]), row["content_id"], struct.pack("<" + "f" * len(vector), *vector)))
                    connection.execute("INSERT INTO lexical(id,text) VALUES(?,?)", (row["id"], row["text"]))
                    count += 1
                connection.executemany("INSERT INTO entities VALUES(?,?)", [(row["id"], row["name"]) for row in ledger.rows("entities")])
                from .graph import graph_projection
                _, gs = graph_projection(ledger)
                connection.executemany("INSERT INTO adjacency VALUES(?,?,?)", [(row["subject"], row["predicate"], row["object"]) for row in gs["edges"]])
            with open(temporary, "rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            write_atomic(self.manifest, {"profile": self.profile, "file": self.path.name,
                                        "sha256": _digest(self.path), "records": count})
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def connect(self):
        return sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True)

    def candidates(self, query, k=100, as_of=None):
        terms = list(dict.fromkeys(tokens(query)))[:64]
        lexical = []
        with self.connect() as connection:
            if terms:
                expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                lexical = [row[0] for row in connection.execute("SELECT id FROM lexical WHERE lexical MATCH ? ORDER BY bm25(lexical), id LIMIT ?", (expression, k))]
            rows = {}
            vector_scores = []
            query_vector = self.vector.encode(query)
            for identity, text, evidence, known_at, assertion_ids, content, vector in connection.execute("SELECT * FROM passages ORDER BY id"):
                if as_of and known_at > as_of:
                    continue
                score = sum(a * b for a, b in zip(query_vector, struct.unpack("<" + "f" * self.vector.dimension, vector)))
                rows[identity] = {"id": identity, "text": text, "evidence": json.loads(evidence), "transaction_time": known_at,
                                  "assertion_ids": json.loads(assertion_ids), "content_id": content, "vector_score": score}
                if score >= (0.30 if self.vector.model else 0.18):
                    vector_scores.append((score, identity))
            dense = [identity for _, identity in sorted(vector_scores, key=lambda item: (-item[0], item[1]))[:k]]
            names = [(identity, name) for identity, name in connection.execute("SELECT id,name FROM entities ORDER BY id") if name in normalized(query) or set(tokens(name)) <= set(terms)]
        return rows, [identity for identity in lexical if identity in rows], dense, names
