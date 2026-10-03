"""Public local memory API: verified BN1_2 → canonical semantic generations."""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from BN1_1.Pacote.cache import Pacote
from BN1_2.buffer import BBN1_2, write_atomic
from Transformer_Core.Hot_Hub.hub import HotHub
from Transformer_Core.structural.model import fingerprint
from .evidence import attach, reference, resolve, texts
from .extraction import extractor_profile, local_request, materialize
from .graph import conflicts
from .model import Ledger, SemanticError, SemanticLimits, VERSION, content_id, now, record
from .multimodal import observations, profile as multimodal_profile
from .resolution import entity
from .storage import Store, read_json
from .validation import run_output_hash
from .worker import execute


def code_profile():
    folder = Path(__file__).parent
    hasher = hashlib.sha256()
    for path in sorted(folder.glob("*.py")):
        hasher.update(path.name.encode())
        hasher.update(path.read_bytes())
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=folder,
                                         stderr=subprocess.DEVNULL, timeout=2, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        commit = "unavailable"
    return {"code_version": VERSION, "code_commit": commit, "code_fingerprint": hasher.hexdigest()}


class Memory:
    def __init__(self, root: Path, namespace="default"):
        self.store = Store(root, namespace)

    def ingest(self, runtime: Path, extractor="rules", model=None, endpoint="http://127.0.0.1:11434",
               limits=None, force=False, ocr=False, ocr_language="eng", asr_model=None,
               strict_multimodal=False, signing_key=None, episode=None, vision_model=None, video_stride=30,
               source_metadata=None):
        settings = locals().copy()
        del settings["self"], settings["runtime"]
        # The same namespace admission lock applies to CLI, MCP and Python.
        # Channel ingestion re-enters its lock; independent inputs cannot pass
        # an external cycle that is awaiting output or cleanup.
        from mimir.io import Channels
        with Channels(self).input_guard(Path(runtime)):
            return self._ingest(runtime, **settings)

    def _ingest(self, runtime: Path, extractor="rules", model=None, endpoint="http://127.0.0.1:11434",
               limits=None, force=False, ocr=False, ocr_language="eng", asr_model=None,
               strict_multimodal=False, signing_key=None, episode=None, vision_model=None, video_stride=30,
               source_metadata=None):
        limits = limits or SemanticLimits()
        extraction = extractor_profile(extractor, model, endpoint)
        modes = multimodal_profile(ocr, ocr_language, asr_model, strict_multimodal, vision_model, endpoint, video_stride)
        model_snapshots = {}
        for selected_model in {value for value in (model if extractor == "ollama" else None, vision_model) if value}:
            # Fingerprint the local model metadata (including its Modelfile
            # weight reference) before inference instead of recording only a tag.
            import urllib.request
            request = urllib.request.Request(endpoint.rstrip("/") + "/api/show", data=json.dumps({"model": selected_model}).encode(), headers={"Content-Type": "application/json"})
            try:
                description = json.loads(local_request(request, 10, 2 * 1024 * 1024))
                model_snapshots[selected_model] = fingerprint(description)
                if selected_model == model and extractor == "ollama":
                    extraction["model_revision"] = fingerprint(description)
                if selected_model == vision_model:
                    modes["vision_revision"] = fingerprint(description)
            except (OSError, ValueError) as exc:
                raise SemanticError("Ollama/modelo local indisponível.") from exc
        profile = {"schema": "mimir.semantic-profile.v1", **code_profile(), "extractor": extraction,
                   "multimodal": modes, "limits": limits.profile(), "episode": episode}
        if source_metadata is not None:
            if not isinstance(source_metadata, dict) or any(not isinstance(key, str) or not isinstance(value, dict) or content_id(key.removeprefix("content:")) != key for key, value in source_metadata.items()):
                raise SemanticError("source_metadata deve mapear content_id para metadados JSON.")
            profile["source_metadata"] = source_metadata
        pacote = Pacote(Path(runtime))
        # Existing verified BN1_2 is preserved. Only create it when absent.
        if not BBN1_2(Path(runtime)).manifest.exists():
            pacote.transform()
        with pacote._locked():
            state = pacote._state()
            if state["status"] != "HUB_READY":
                raise SemanticError("Semantic Core requer um ciclo HUB_READY.")
            pacote._count(state)
            expected = pacote._expected_sources(state)
            hub = HotHub(Path(runtime))
            snapshot = hub.verify(expected)
            if source_metadata and not set(source_metadata) <= {content_id(digest) for digest in snapshot["sources"].values()}:
                raise SemanticError("Metadados externos não correspondem aos bytes do upstream.")
            buffer = BBN1_2(Path(runtime))
            bn = buffer.verify(snapshot)
            with self.store.locked(write=True):
                previous, ledger = self.store.load()
                if not force and previous and previous["upstream"]["generation"] == bn["generation"] and previous["upstream"]["fingerprint"] == fingerprint(bn) and previous["profile"] == profile:
                    return previous
                upstream_id = ledger.put("upstreams", bn_manifest=bn, hot_hub_manifest=snapshot)
                upstream = ledger.get("upstreams", upstream_id)
                transaction_time = now()
                run = record("inference_runs", extractor_name=extraction["name"], extractor_version=extraction["version"],
                             model_id=extraction["model_id"], model_revision=extraction["model_revision"],
                             prompt_template_hash=extraction["prompt_template_hash"], parameters={"extractor": extraction["parameters"], "multimodal": modes},
                             **code_profile(), input_generation=bn["generation"], input_fingerprint=fingerprint(bn),
                             output_hash="0" * 64, timestamp=transaction_time)
                if source_metadata is not None:
                    run["parameters"]["source_metadata"] = source_metadata
                    # Recompute the run identity after adding provenance.
                    run = record("inference_runs", **{key: value for key, value in run.items() if key not in {"id", "schema"}})
                total_chars, total_assertions = 0, 0
                errors = []
                for name, document in buffer.documents(snapshot):
                    data = hub.reconstruct_snapshot(name, snapshot)
                    self.store.store_source(data, content_id(document["source"]["sha256"]))
                    occurrence, bindings = attach(ledger, document, upstream_id)
                    from .views import for_occurrence
                    views = for_occurrence(ledger, ledger.get("occurrences", occurrence))
                    passages = []
                    for node in document["nodes"]:
                        binding_id = bindings[node["id"]]
                        passages.extend((binding_id, prop, text) for prop, text in texts(node, views))
                        derived, error = observations(data, node, modes, limits)
                        if error:
                            errors.append({"source": name, "node_id": node["id"], "error": error})
                        for value in derived:
                            obs_id = ledger.put("observations", binding_id=binding_id,
                                inference_run_id=run["id"], **value)
                            passages.append((binding_id, "observation:" + obs_id, value["text"]))
                    del data
                    total_chars += sum(len(text) for _, _, text in passages)
                    if total_chars > limits.max_text_chars:
                        raise SemanticError("Lote semântico excede max_text_chars.")
                    if passages:
                        results = execute({"mode": "extract", "texts": [text for _, _, text in passages],
                                           "profile": extraction, "timeout": limits.worker_timeout}, limits)
                        if not isinstance(results, list) or len(results) != len(passages):
                            raise SemanticError("Output parcial do extractor.")
                        episode_refs = []
                        for (binding_id, prop, text), claims in zip(passages, results):
                            if not isinstance(claims, list):
                                raise SemanticError("Output do extractor deve conter listas de claims.")
                            for claim in claims:
                                total_assertions += 1
                                if total_assertions > limits.max_assertions:
                                    raise SemanticError("Lote excede max_assertions.")
                                materialize(ledger, claim, binding_id, prop, text, run["id"], transaction_time)
                            # Dates are observations even when no relation is inferred.
                            for match in re.finditer(r"\b\d{4}(?:-\d{2}-\d{2})?\b", text):
                                ev = reference(ledger, binding_id, prop, text, match.start(), match.end())
                                ledger.put("mentions", mention_type="date", text=match.group(), evidence=ev, inference_run_id=run["id"])
                            if episode:
                                episode_refs.append(reference(ledger, binding_id, prop, text))
                        if episode and episode_refs:
                            ledger.put("episodes", episode_type=episode.get("type", "interaction"),
                                       text="\n".join(text for _, _, text in passages), evidence=episode_refs,
                                       transaction_time=transaction_time, metadata=episode.get("metadata", {}))
                    if sum(ledger.counts().values()) > limits.max_records:
                        raise SemanticError("Ledger excede max_records.")
                run["output_hash"] = run_output_hash(ledger, run["id"])
                ledger.put("reports", inference_run_id=run["id"], structural=bn["summary"],
                           multimodal_errors=errors, capabilities=modes,
                           usage={"provider": extraction["name"], "tokens": None if extraction["name"] != "rules" else 0})
                run["output_hash"] = run_output_hash(ledger, run["id"])
                ledger.add("inference_runs", run)
                conflicts(ledger, transaction_time)
                self._invalidate(ledger)
                if hub.verify(expected) != snapshot or buffer.verify(snapshot) != bn:
                    raise SemanticError("Upstream mudou durante a inferência.")
                for selected_model, revision in model_snapshots.items():
                    request = urllib.request.Request(endpoint.rstrip("/") + "/api/show", data=json.dumps({"model": selected_model}).encode(), headers={"Content-Type": "application/json"})
                    try:
                        current = json.loads(local_request(request, 10, 2 * 1024 * 1024))
                    except (OSError, ValueError) as exc:
                        raise SemanticError("Modelo local indisponível na validação final.") from exc
                    if fingerprint(current) != revision:
                        raise SemanticError("Modelo local mudou durante a inferência.")
                # Coverage is an explicit report; unavailable multimodal models
                # are never represented as successful complete inference.
                return self.store.publish(ledger, upstream, profile, signing_key=signing_key)

    def verify(self):
        with self.store.locked():
            manifest, ledger = self.store.load()
            if manifest is None:
                raise SemanticError("Memória vazia. Execute semantic ou run primeiro.")
            return {"verified": True, "generation": manifest["generation"], "fingerprint": manifest["fingerprint"],
                    "summary": ledger.counts(), "signature": manifest["signature"] is not None}

    def inspect(self, collection="assertions"):
        if collection not in Ledger().data:
            raise SemanticError("Coleção desconhecida.")
        with self.store.locked():
            _, ledger = self.store.load()
            return ledger.rows(collection)

    def explain(self, assertion_id):
        with self.store.locked():
            manifest, ledger = self.store.load()
            assertion = ledger.get("assertions", assertion_id)
            return {"generation": manifest["generation"], "assertion": assertion,
                    "proposition": ledger.get("propositions", assertion["proposition_id"]),
                    "inference": ledger.get("inference_runs", assertion["inference_run_id"]),
                    "evidence": [resolve(ledger, evidence) for evidence in assertion["evidence"]]}

    def source(self, identity: str, output: Path):
        with self.store.locked():
            _, ledger = self.store.load()
            if identity not in {row["content_id"] for row in ledger.rows("occurrences")}:
                raise SemanticError("Conteúdo não pertence à memória.")
            output = Path(output)
            if output.exists() or output.is_symlink():
                raise SemanticError("Destino de exportação já existe.")
            with self.store.source_path(identity).open("rb") as source, output.open("xb") as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(chunk)
            return {"content_id": identity, "output": str(output)}

    def _update(self, callback, operation, signing_key=None):
        with self.store.locked(write=True):
            manifest, ledger = self.store.load()
            if manifest is None:
                raise SemanticError("A operação requer memória inicializada.")
            callback(ledger)
            self._invalidate(ledger)
            return self.store.publish(ledger, ledger.get("upstreams", manifest["upstream"]["upstream_id"]),
                                      manifest["profile"], operation, signing_key)

    def relate(self, new_assertion, old_assertion, predicate="supersedes", signing_key=None):
        if predicate not in {"supersedes", "retracts"} or new_assertion == old_assertion:
            raise SemanticError("Atualização deve relacionar duas assertions distintas.")
        def update(ledger):
            ledger.get("assertions", new_assertion)
            ledger.get("assertions", old_assertion)
            ledger.put("relations", subject=new_assertion, predicate=predicate, object=old_assertion,
                       method="explicit_user_update", transaction_time=now())
        return self._update(update, "update", signing_key)

    def resolve_entity(self, mention_id, label, entity_type="candidate", signing_key=None):
        def update(ledger):
            mention = ledger.get("mentions", mention_id)
            if mention["mention_type"] != "entity":
                raise SemanticError("Resolução de entidade requer uma menção do tipo entity.")
            identity = entity(ledger, label, entity_type)
            ledger.put("resolutions", mention_id=mention_id, entity_id=identity, method="human_override",
                       score=1.0, features={"label": label}, human_override=True,
                       transaction_time=now(), inference_run_id=None)
        return self._update(update, "resolve", signing_key)

    @staticmethod
    def _invalidate(ledger):
        invalid = {row["object"] for row in ledger.rows("relations") if row["predicate"] in {"supersedes", "retracts"}}
        invalid |= {row[key] for row in ledger.rows("relations") if row["predicate"] == "conflicts_with" for key in ("subject", "object")}
        changed = True
        while changed:
            changed = False
            for reflection in ledger.rows("reflections"):
                causes = sorted(set(reflection["dependencies"]) & invalid)
                if causes and reflection["id"] not in invalid:
                    ledger.put("relations", subject=causes[0], predicate="invalidates", object=reflection["id"],
                               method="dependency_invalidation_v1", transaction_time=now())
                    invalid.add(reflection["id"])
                    changed = True

    def consolidate(self, memory_type="reflective", max_items=20, signing_key=None, max_chars=1600):
        if memory_type not in {"reflective", "procedural", "community"}:
            raise SemanticError("Tipo de consolidação inválido.")
        if type(max_items) is not int or not 1 <= max_items <= 1000:
            raise SemanticError("max_items deve estar entre 1 e 1000.")
        if type(max_chars) is not int or not 256 <= max_chars <= 8000:
            raise SemanticError("max_chars deve estar entre 256 e 8000.")
        def update(ledger):
            from .language import spans
            from .model import normalized
            from .revisions import current_bindings
            from .views import for_occurrence
            invalid = {row["object"] for row in ledger.rows("relations") if row["predicate"] in {"invalidates", "supersedes", "retracts"}}
            invalid |= {row[k] for row in ledger.rows("relations") if row["predicate"] == "conflicts_with" for k in ("subject", "object")}
            bindings = current_bindings(ledger)
            units, seen = [], set()
            def add(identity, text):
                key = normalized(text)
                if key not in seen and len(text) <= max_chars:
                    seen.add(key)
                    units.append((identity, text))
            all_quotes = {normalized(ev["quote"]) for row in ledger.rows("assertions") for ev in row["evidence"]}
            for row in ledger.rows("assertions"):
                if row["id"] not in invalid and all(ev["binding_id"] in bindings for ev in row["evidence"]):
                    add(row["id"], row["evidence"][0]["quote"])
            for row in ledger.rows("episodes"):
                if any(ev["binding_id"] not in bindings for ev in row["evidence"]):
                    continue
                for lo, hi in spans(row["text"]):
                    text = row["text"][lo:hi]
                    if normalized(text) not in all_quotes:
                        add(row["id"], text)
            for binding in ledger.rows("bindings"):
                if binding["id"] not in bindings:
                    continue
                occurrence = ledger.get("occurrences", binding["occurrence_id"])
                view = for_occurrence(ledger, occurrence).records.get(binding["node_id"])
                if view:
                    add(binding["id"], view["text"])
                else:
                    node = next(n for n in occurrence["document"]["nodes"] if n["id"] == binding["node_id"])
                    text = node["properties"].get("text", "")
                    for lo, hi in spans(text):
                        sentence = text[lo:hi]
                        if normalized(sentence) not in all_quotes:
                            add(binding["id"], sentence)
            if not units:
                raise SemanticError("Não há episódios/afirmações para consolidar.")
            groups, group, size = [], [], 0
            for item in units:
                if group and (len(group) == max_items or size + len(item[1]) + 1 > max_chars):
                    groups.append(group)
                    group, size = [], 0
                group.append(item)
                size += len(item[1]) + 1
            if group:
                groups.append(group)
            previous = []
            for group in groups:
                summary = "\n".join(text for _, text in group)
                previous.append(ledger.put("reflections", summary=summary,
                    dependencies=list(dict.fromkeys(identity for identity, _ in group)), method="concise_extractive_hierarchy_v2",
                    transaction_time=now(), level=1, memory_type=memory_type))
            level = 2
            while len(previous) > 1:
                following = []
                for index in range(0, len(previous), max(2, max_items)):
                    group = previous[index:index + max(2, max_items)]
                    lines, size, seen_lines = [], 0, set()
                    for key in group:
                        line = ledger.get("reflections", key)["summary"]
                        if line not in seen_lines and size + len(line) + bool(lines) <= max_chars:
                            lines.append(line)
                            seen_lines.add(line)
                            size += len(line) + bool(len(lines) > 1)
                    following.append(ledger.put("reflections", summary="\n".join(lines),
                        dependencies=group, method="concise_extractive_hierarchy_v2", transaction_time=now(),
                        level=level, memory_type=memory_type))
                previous, level = following, level + 1
        return self._update(update, "consolidate", signing_key)

    def episode(self, text: str, episode_type="interaction", metadata=None, **settings):
        if not isinstance(text, str) or not text.strip():
            raise SemanticError("Episódio deve conter texto.")
        with tempfile.TemporaryDirectory(prefix="mimir-episode-") as temporary:
            root = Path(temporary)
            source = root / "episode.txt"
            source.write_text(text, encoding="utf-8")
            runtime = root / "runtime"
            pacote = Pacote(runtime)
            pacote.open()
            pacote.add([source])
            return self.ingest(runtime, episode={"type": episode_type, "metadata": metadata or {}}, **settings)

    def query(self, question, save=False, **settings):
        from .retrieval import search
        with self.store.locked(write=save):
            manifest, ledger = self.store.load()
            if manifest is None:
                raise SemanticError("Memória vazia. Execute run ou semantic.")
            working = self.store.root / "working.json"
            core = read_json(working) if working.exists() else None
            if core is not None and (not isinstance(core, dict) or core.get("schema") != "mimir.working-memory.v1" or not isinstance(core.get("fields"), dict)):
                raise SemanticError("Working Memory malformada.")
            budget = settings.get("budget_chars", 8000)
            core_chars = len(json.dumps(core["fields"], ensure_ascii=True)) if core else 0
            if budget - core_chars < 256:
                raise SemanticError("Working Memory excede o orçamento do contexto; aumente budget_chars.")
            settings["budget_chars"] = budget - core_chars
            result = search(self.store, manifest, ledger, question, **settings)
            if working.exists():
                result["context"]["working_memory"] = core
            result["context"]["chars"] += core_chars
            result["context"]["budget_chars"] = budget
            if save:
                folder = self.store.root / "Output_Storage"
                from BN1_2.buffer import ensure_directories
                ensure_directories(self.store.base, folder)
                write_atomic(folder / "output.json", {"schema": "mimir.output.v1",
                    "semantic_generation": manifest["generation"], "semantic_fingerprint": manifest["fingerprint"],
                    "result": result, "timestamp": now()})
            return result

    def reindex(self, model=None, revision=None):
        from .indexes import Index
        with self.store.locked(write=True):
            manifest, ledger = self.store.load()
            if manifest is None:
                raise SemanticError("Memória vazia.")
            index = Index(self.store, manifest, ledger, model, revision, force=True)
            return {"profile": index.profile, "path": str(index.path), "generation": manifest["generation"]}

    def working(self, values=None):
        fields = {"identity", "objective", "project", "constraints", "commitments", "open_problems", "critical_entities"}
        with self.store.locked(write=values is not None):
            path = self.store.root / "working.json"
            if values is None:
                return read_json(path) if path.exists() else {"schema": "mimir.working-memory.v1", "fields": {}}
            if not isinstance(values, dict) or not set(values) <= fields or any(not isinstance(value, (str, list)) or isinstance(value, list) and any(not isinstance(item, str) for item in value) for value in values.values()) or len(json.dumps(values)) > 8000:
                raise SemanticError("Working Memory aceita apenas campos declarados, com até 8000 caracteres.")
            result = {"schema": "mimir.working-memory.v1", "fields": values, "classification": "user_configuration"}
            write_atomic(path, result)
            return result

    def policy(self, readers=None, writers=None):
        with self.store.locked(write=True):
            policy = self.store.authorize(write=True)
            if policy["owner_uid"] != __import__("os").getuid():
                raise SemanticError("Somente o proprietário pode alterar a política.")
            if readers is None and writers is None:
                return policy
            for values in (readers, writers):
                if values is not None and (not isinstance(values, list) or any(type(value) is not int or value < 0 for value in values)):
                    raise SemanticError("ACL usa listas de UIDs POSIX não negativos.")
            if readers is not None:
                policy["readers"] = sorted(set(readers))
            if writers is not None:
                policy["writers"] = sorted(set(writers))
            write_atomic(self.store.policy, policy)
            return policy

    def gc(self, apply=False):
        import shutil
        with self.store.locked(write=apply):
            manifest, _ = self.store.load()
            referenced = set()
            current = manifest
            while current:
                referenced.add(current["generation"])
                parent = current["parent"]
                current = read_json(self.store.generations / parent["generation"] / "manifest.json") if parent else None
            candidates = []
            for folder in self.store.generations.iterdir():
                if folder.is_symlink():
                    raise SemanticError("GC encontrou symlink; limpeza recusada.")
                if re.fullmatch(r"[0-9a-f]{32}", folder.name) and folder.is_dir() and folder.name not in referenced:
                    candidates.append(folder)
            candidates.extend(folder for folder in self.store.indexes.iterdir() if folder.is_dir() and not folder.is_symlink())
            for folder in candidates:
                HotHub._tree_files(folder)
            result = {"dry_run": not apply, "paths": [str(folder) for folder in candidates], "canonical_generations_preserved": len(referenced)}
            if apply:
                for folder in candidates:
                    shutil.rmtree(folder)
            return result
