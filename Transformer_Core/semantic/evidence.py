"""Stable anchors coexist with historical occurrence and structural identities."""

import hashlib

from .model import Ledger, SemanticError, content_id


def attach(ledger: Ledger, document: dict, upstream_id: str) -> tuple[str, dict]:
    source = document["source"]
    occurrence = ledger.put("occurrences", source_occurrence_id=source["id"],
                            content_id=content_id(source["sha256"]), upstream_id=upstream_id,
                            document=document)
    bindings = {}
    for node in document["nodes"]:
        anchor = ledger.put("anchors", content_id=content_id(source["sha256"]), kind=node["kind"],
                            locator=node["locator"], protocol={"route": document["protocol"]["route"],
                            "version": document["protocol"]["version"]})
        binding = ledger.put("bindings", anchor_id=anchor, occurrence_id=occurrence, node_id=node["id"])
        bindings[node["id"]] = binding
    return occurrence, bindings


def node_for(ledger: Ledger, binding_id: str) -> tuple[dict, dict]:
    binding = ledger.get("bindings", binding_id)
    occurrence = ledger.get("occurrences", binding["occurrence_id"])
    node = next((node for node in occurrence["document"]["nodes"] if node["id"] == binding["node_id"]), None)
    if node is None:
        raise SemanticError("Nó estrutural da evidência não encontrado.")
    return occurrence, node


def texts(node: dict, views=None):
    properties = node["properties"]
    for key in ("text", "value"):
        if isinstance(properties.get(key), str) and properties[key]:
            yield key, properties[key]
        elif key == "value" and key in properties and properties[key] is not None:
            from .views import scalar
            yield key, scalar(properties[key])
    for key in ("fields", "cells"):
        for index, value in enumerate(properties.get(key, [])):
            if isinstance(value, str) and value:
                yield f"{key}.{index}", value
    if views and node["id"] in views.records:
        for prop, view in views.properties(node["id"]).items():
            yield prop, view["text"]


def reference(ledger: Ledger, binding_id: str, property_name: str, text: str, start=0, end=None) -> dict:
    end = len(text) if end is None else end
    binding = ledger.get("bindings", binding_id)
    quote = text[start:end]
    return {"anchor_id": binding["anchor_id"], "binding_id": binding_id, "node_id": binding["node_id"],
            "property": property_name, "char_start": start, "char_end": end,
            "quote": quote, "quote_sha256": hashlib.sha256(quote.encode()).hexdigest()}


def resolve(ledger: Ledger, evidence: dict) -> dict:
    if set(evidence) != {"anchor_id", "binding_id", "node_id", "property", "char_start", "char_end", "quote", "quote_sha256"}:
        raise SemanticError("Campos inesperados na referência de evidência.")
    binding = ledger.get("bindings", evidence["binding_id"])
    if evidence["anchor_id"] != binding["anchor_id"] or evidence["node_id"] != binding["node_id"]:
        raise SemanticError("Evidência associada a outra origem.")
    anchor = ledger.get("anchors", binding["anchor_id"])
    occurrence, node = node_for(ledger, binding["id"])
    prop = evidence["property"]
    if prop.startswith("observation:"):
        observation = ledger.get("observations", prop.removeprefix("observation:"))
        if observation["binding_id"] != binding["id"]:
            raise SemanticError("Observação multimodal de outra origem.")
        text = observation["text"]
    else:
        from .views import for_occurrence
        view = for_occurrence(ledger, occurrence) if prop.startswith("projection:") else None
        projection = view.resolve_property(node["id"], prop) if view else None
        text = projection["text"] if projection else dict(texts(node)).get(prop)
    start, end = evidence["char_start"], evidence["char_end"]
    if not isinstance(text, str) or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        raise SemanticError("Span de evidência fora da propriedade de origem.")
    quote = text[start:end]
    if quote != evidence["quote"] or hashlib.sha256(quote.encode()).hexdigest() != evidence["quote_sha256"]:
        raise SemanticError("Texto da evidência diverge da origem.")
    result = {"content_id": anchor["content_id"], "source_occurrence_id": occurrence["source_occurrence_id"],
            "source_name": occurrence["document"]["source"]["name"], "node_id": node["id"],
            "anchor_id": anchor["id"], "locator": node["locator"], "property": prop, "quote": quote,
            "upstream_id": occurrence["upstream_id"]}
    if prop.startswith("observation:"):
        result["derived_observation"] = observation
    elif prop.startswith("projection:"):
        result["structural_projection"] = projection
    upstream = ledger.get("upstreams", occurrence["upstream_id"])
    for run in sorted(ledger.rows("inference_runs"), key=lambda row: row["timestamp"]):
        metadata = run["parameters"].get("source_metadata", {})
        if run["input_generation"] == upstream["bn_manifest"]["generation"] and anchor["content_id"] in metadata:
            result["external_source"] = metadata[anchor["content_id"]]
            break
    return result
