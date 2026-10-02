"""Semantic firewall: no publication before every reference is resolvable."""

import hashlib
import json
import math
import re

from BN1_1.contracts import validate_names
from Transformer_Core.Hot_Hub.hub import CHUNK_SIZE, SCHEMA as BYTE_SCHEMA, marker
from Transformer_Core.structural.curator import validate as validate_structural
from Transformer_Core.structural.model import canonical, fingerprint
from .evidence import node_for, resolve
from .graph import graph_projection
from .model import COLLECTIONS, Ledger, SemanticError, check_record, content_id, timestamp
from .resolution import valid_time

FIELDS = {
    "upstreams": {"bn_manifest", "hot_hub_manifest"},
    "occurrences": {"source_occurrence_id", "content_id", "upstream_id", "document"},
    "anchors": {"content_id", "kind", "locator", "protocol"},
    "bindings": {"anchor_id", "occurrence_id", "node_id"},
    "observations": {"binding_id", "text", "locator", "method", "confidence", "inference_run_id"},
    "mentions": {"mention_type", "text", "evidence", "inference_run_id"},
    "entities": {"name", "entity_type"},
    "resolutions": {"mention_id", "entity_id", "method", "score", "features", "human_override", "transaction_time", "inference_run_id"},
    "events": {"event_type", "participants", "valid_time", "evidence", "assertion_id", "inference_run_id"},
    "propositions": {"subject", "predicate", "object"},
    "assertions": {"proposition_id", "polarity", "valid_time", "transaction_time", "evidence", "epistemic_status", "inference_run_id", "mention_id"},
    "inference_runs": {"extractor_name", "extractor_version", "model_id", "model_revision", "prompt_template_hash", "parameters", "code_version", "code_commit", "code_fingerprint", "input_generation", "input_fingerprint", "output_hash", "timestamp"},
    "relations": {"subject", "predicate", "object", "method", "transaction_time"},
    "episodes": {"episode_type", "text", "evidence", "transaction_time", "metadata"},
    "reflections": {"summary", "dependencies", "method", "transaction_time", "level", "memory_type"},
    "reports": {"inference_run_id", "structural", "multimodal_errors", "capabilities", "usage"},
}


def run_output_hash(ledger: Ledger, run_id: str) -> str:
    return fingerprint({name: [row for row in ledger.rows(name) if row.get("inference_run_id") == run_id]
                        for name in COLLECTIONS if name != "inference_runs"})


def record_digest(document: dict, blob_path) -> str:
    source = document["source"]
    name, length = source["name"], source["byte_length"]
    label, identity = marker(name), source["id"]
    hasher = hashlib.sha256()
    def line(value):
        hasher.update((json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode())
    line({"kind": "file", "schema": BYTE_SCHEMA, "source_name": name, "source_id": identity,
          "marker": label, "byte_length": length, "sha256": source["sha256"],
          "chunk_size": CHUNK_SIZE, "chunk_count": (length + CHUNK_SIZE - 1) // CHUNK_SIZE})
    with blob_path.open("rb") as stream:
        index = 0
        for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
            line({"kind": "chunk", "marker": label, "source_id": identity, "index": index,
                  "valid_length": len(block), "values": list(block) + [0] * (CHUNK_SIZE - len(block))})
            index += 1
    return hasher.hexdigest()


def validate(ledger: Ledger, source_paths: dict | None = None):
    try:
        for name in COLLECTIONS:
            for row in ledger.rows(name):
                if set(row) != FIELDS[name] | {"schema", "id"}:
                    raise SemanticError(f"Campos inesperados em {name}.")
                check_record(name, row)
                for key in ("transaction_time", "timestamp"):
                    if key in row and timestamp(row[key]) != row[key]:
                        raise SemanticError("Timestamp do ledger deve ser UTC canônico.")
        occurrence_map = {}
        for row in ledger.rows("occurrences"):
            document, source = row["document"], row["document"]["source"]
            validate_names([source["name"]])
            if row["source_occurrence_id"] != source["id"] or row["content_id"] != content_id(source["sha256"]):
                raise SemanticError("Ocorrência associada a outros bytes.")
            validate_structural(document, source)
            upstream = ledger.get("upstreams", row["upstream_id"])
            bn, hot = upstream["bn_manifest"], upstream["hot_hub_manifest"]
            if bn["upstream"] != {"generation": hot["generation"], "fingerprint": fingerprint(hot)} or source["hot_hub_generation"] != hot["generation"] or source["id"] != hot["generation"] + ":" + source["name"]:
                raise SemanticError("Vínculo BN1_2/Hot Hub divergente.")
            if source["sha256"] != hot["sources"][source["name"]] or source["record_sha256"] != hot["records"][source["name"]]["sha256"]:
                raise SemanticError("Inventário de bytes divergente.")
            if hashlib.sha256(canonical(document) + b"\n").hexdigest() != bn["records"][source["name"]]["sha256"]:
                raise SemanticError("Snapshot estrutural diverge do BN1_2.")
            if source_paths is not None:
                blob = source_paths[row["content_id"]]
                if blob.is_symlink() or not blob.is_file() or blob.stat().st_size != source["byte_length"]:
                    raise SemanticError("Bytes canônicos ausentes ou inválidos.")
                if record_digest(document, blob) != source["record_sha256"]:
                    raise SemanticError("Os bytes não reconstroem o registro Hot Hub arquivado.")
            occurrence_map.setdefault(row["upstream_id"], set()).add(source["name"])
        for upstream in ledger.rows("upstreams"):
            bn, hot = upstream["bn_manifest"], upstream["hot_hub_manifest"]
            if bn["schema"] != "mimir.bn1_2.v1" or hot["schema"] != BYTE_SCHEMA or occurrence_map.get(upstream["id"], set()) != set(bn["records"]) or set(bn["records"]) != set(hot["sources"]):
                raise SemanticError("Snapshot upstream incompleto.")
        bound_nodes, bound_anchors = {}, set()
        for binding in ledger.rows("bindings"):
            occurrence, node = node_for(ledger, binding["id"])
            anchor = ledger.get("anchors", binding["anchor_id"])
            if anchor["content_id"] != occurrence["content_id"] or anchor["kind"] != node["kind"] or anchor["locator"] != node["locator"] or anchor["protocol"] != {"route": occurrence["document"]["protocol"]["route"], "version": occurrence["document"]["protocol"]["version"]}:
                raise SemanticError("Anchor não corresponde ao nó estrutural.")
            bound_nodes.setdefault(occurrence["id"], set()).add(node["id"])
            bound_anchors.add(anchor["id"])
        if bound_anchors != set(ledger.data["anchors"]) or any(bound_nodes.get(row["id"], set()) != {node["id"] for node in row["document"]["nodes"]} for row in ledger.rows("occurrences")):
            raise SemanticError("Cobertura de anchors/bindings incompleta.")
        for observation in ledger.rows("observations"):
            occurrence, node = node_for(ledger, observation["binding_id"])
            run = ledger.get("inference_runs", observation["inference_run_id"])
            bn = ledger.get("upstreams", occurrence["upstream_id"])["bn_manifest"]
            if run["input_generation"] != bn["generation"] or run["input_fingerprint"] != fingerprint(bn):
                raise SemanticError("Observação deriva de outro upstream que o run.")
            if not isinstance(observation["text"], str) or not observation["text"] or observation["method"] not in {"ocr", "asr", "vision"} or not isinstance(observation["locator"], dict) or type(observation["confidence"]) not in {int, float} or not 0 <= observation["confidence"] <= 1:
                raise SemanticError("Observação derivada inválida.")
            loc = observation["locator"]
            if observation["method"] == "ocr":
                fields = {"type", "left", "top", "width", "height", "image_width", "image_height"}
                if set(loc) != fields or loc["type"] != "ocr_box" or any(type(loc[key]) is not int or loc[key] < 0 for key in fields - {"type"}) or not loc["width"] or not loc["height"] or loc["left"] + loc["width"] > loc["image_width"] or loc["top"] + loc["height"] > loc["image_height"]:
                    raise SemanticError("Caixa OCR fora da imagem.")
            elif observation["method"] == "asr":
                fields = {"type", "start_seconds", "end_seconds", "sample_start", "sample_end", "sample_rate", "coordinate_space"}
                if set(loc) != fields or loc["type"] != "audio_time" or loc["coordinate_space"] != "resampled_pcm" or loc["sample_rate"] != 16000 or any(type(loc[key]) not in {int, float} or not math.isfinite(loc[key]) for key in ("start_seconds", "end_seconds")) or not 0 <= loc["start_seconds"] < loc["end_seconds"] or loc["sample_start"] != int(loc["start_seconds"] * 16000) or loc["sample_end"] != int(loc["end_seconds"] * 16000):
                    raise SemanticError("Coordenadas ASR inválidas.")
            elif loc != {"type": "image_region", "scope": "whole_image"}:
                raise SemanticError("Localização da observação visual inválida.")
        for mention in ledger.rows("mentions"):
            run = ledger.get("inference_runs", mention["inference_run_id"])
            resolve(ledger, mention["evidence"])
            occurrence, _ = node_for(ledger, mention["evidence"]["binding_id"])
            bn = ledger.get("upstreams", occurrence["upstream_id"])["bn_manifest"]
            if run["input_generation"] != bn["generation"] or run["input_fingerprint"] != fingerprint(bn):
                raise SemanticError("Menção de outro upstream que o run.")
            if mention["mention_type"] not in {"entity", "date", "relation"} or mention["text"] != mention["evidence"]["quote"]:
                raise SemanticError("Menção sem evidência exata.")
        for entity in ledger.rows("entities"):
            if not isinstance(entity["name"], str) or not entity["name"] or entity["entity_type"] not in {"candidate", "person", "organization", "place"}:
                raise SemanticError("Entidade inválida.")
        for resolution in ledger.rows("resolutions"):
            ledger.get("mentions", resolution["mention_id"])
            ledger.get("entities", resolution["entity_id"])
            if resolution["inference_run_id"] is not None:
                ledger.get("inference_runs", resolution["inference_run_id"])
            timestamp(resolution["transaction_time"])
            if type(resolution["human_override"]) is not bool or type(resolution["score"]) not in {int, float} or not 0 <= resolution["score"] <= 1 or not isinstance(resolution["features"], dict):
                raise SemanticError("Resolução inválida.")
        for proposition in ledger.rows("propositions"):
            ledger.get("entities", proposition["subject"])
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", proposition["predicate"]) or set(proposition["object"]) != {"kind", "value"} or proposition["object"]["kind"] not in {"entity", "literal"}:
                raise SemanticError("Proposição inválida.")
            if proposition["object"]["kind"] == "entity":
                ledger.get("entities", proposition["object"]["value"])
            elif not isinstance(proposition["object"]["value"], str):
                raise SemanticError("Literal inválido.")
        for assertion in ledger.rows("assertions"):
            proposition = ledger.get("propositions", assertion["proposition_id"])
            ledger.get("inference_runs", assertion["inference_run_id"])
            mention = ledger.get("mentions", assertion["mention_id"])
            timestamp(assertion["transaction_time"])
            if assertion["polarity"] not in {"positive", "negative"} or assertion["epistemic_status"] != "reported" or not isinstance(assertion["evidence"], list) or not assertion["evidence"]:
                raise SemanticError("Afirmação sem evidência ou status explícito.")
            if assertion["valid_time"] != valid_time(assertion["valid_time"]["from"], assertion["valid_time"]["to"]):
                raise SemanticError("Tempo de validade inválido.")
            for ev in assertion["evidence"]:
                resolve(ledger, ev)
            if mention["mention_type"] != "relation" or mention["inference_run_id"] != assertion["inference_run_id"] or mention["evidence"] not in assertion["evidence"]:
                raise SemanticError("Afirmação não corresponde à menção/run declarados.")
            from .model import normalized
            surface = normalized(" ".join(ev["quote"] for ev in assertion["evidence"]))
            subject_name = ledger.get("entities", proposition["subject"])["name"]
            object_name = ledger.get("entities", proposition["object"]["value"])["name"] if proposition["object"]["kind"] == "entity" else normalized(proposition["object"]["value"])
            if subject_name not in surface or object_name not in surface:
                raise SemanticError("Afirmação contém entidade/valor fora da evidência.")
        for event in ledger.rows("events"):
            ledger.get("assertions", event["assertion_id"])
            ledger.get("inference_runs", event["inference_run_id"])
            for identity in event["participants"]:
                ledger.get("entities", identity)
            if event["valid_time"] != valid_time(event["valid_time"]["from"], event["valid_time"]["to"]):
                raise SemanticError("Evento temporal inválido.")
            for ev in event["evidence"]:
                resolve(ledger, ev)
        for run in ledger.rows("inference_runs"):
            timestamp(run["timestamp"])
            if any(not isinstance(run[field], str) or not re.fullmatch(r"[0-9a-f]{64}", run[field]) for field in ("prompt_template_hash", "code_fingerprint", "input_fingerprint", "output_hash")) or not isinstance(run["parameters"], dict):
                raise SemanticError("Perfil de inferência inválido.")
            if run["output_hash"] != run_output_hash(ledger, run["id"]):
                raise SemanticError("Output de inferência diverge do run arquivado.")
            upstream = next((row for row in ledger.rows("upstreams") if row["bn_manifest"]["generation"] == run["input_generation"] and fingerprint(row["bn_manifest"]) == run["input_fingerprint"]), None)
            if upstream is None:
                raise SemanticError("Run de inferência sem upstream verificado.")
        for report in ledger.rows("reports"):
            ledger.get("inference_runs", report["inference_run_id"])
            if not isinstance(report["multimodal_errors"], list) or not isinstance(report["capabilities"], dict) or not isinstance(report["usage"], dict):
                raise SemanticError("Relatório de cobertura inválido.")
        all_ids = {identity for rows in ledger.data.values() for identity in rows}
        supersession = {}
        for relation in ledger.rows("relations"):
            if relation["subject"] not in all_ids or relation["object"] not in all_ids or relation["subject"] == relation["object"] or relation["predicate"] not in {"conflicts_with", "supersedes", "retracts", "invalidates"}:
                raise SemanticError("Relação semântica inválida.")
            timestamp(relation["transaction_time"])
            if relation["predicate"] in {"conflicts_with", "supersedes", "retracts"}:
                ledger.get("assertions", relation["subject"])
                ledger.get("assertions", relation["object"])
            if relation["predicate"] == "supersedes":
                supersession.setdefault(relation["subject"], set()).add(relation["object"])
            if relation["predicate"] == "invalidates":
                ledger.get("reflections", relation["object"])
        for start in supersession:
            pending, seen = list(supersession[start]), set()
            while pending:
                item = pending.pop()
                if item == start:
                    raise SemanticError("Supersession cíclica.")
                if item not in seen:
                    seen.add(item)
                    pending.extend(supersession.get(item, set()))
        for episode in ledger.rows("episodes"):
            timestamp(episode["transaction_time"])
            if not isinstance(episode["text"], str) or not episode["evidence"] or not isinstance(episode["metadata"], dict):
                raise SemanticError("Episódio sem evidência.")
            for ev in episode["evidence"]:
                resolve(ledger, ev)
        for reflection in ledger.rows("reflections"):
            timestamp(reflection["transaction_time"])
            if not reflection["dependencies"] or reflection["memory_type"] not in {"reflective", "procedural", "community"} or type(reflection["level"]) is not int or reflection["level"] <= 0:
                raise SemanticError("Reflexão sem dependências.")
            if any(identity not in all_ids or identity == reflection["id"] for identity in reflection["dependencies"]):
                raise SemanticError("Reflexão com dependências inválidas.")
            if any(ledger.data["reflections"][identity]["level"] >= reflection["level"] for identity in reflection["dependencies"] if identity in ledger.data["reflections"]):
                raise SemanticError("Hierarquia de reflexão inválida.")
        graph_projection(ledger)
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration) as exc:
        raise SemanticError("Ledger semântico malformado ou incompleto.") from exc
    return True
