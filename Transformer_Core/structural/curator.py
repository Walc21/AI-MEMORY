"""Curadoria + G_P. Only observed membership, sequence and origin are allowed."""

from collections import defaultdict
import re

from .model import SCHEMA, PROVENANCE_SCHEMA, ProtocolResult, StructuralError, fingerprint
from .router import route

KINDS = {"file", "text_line", "json_object", "json_array", "json_value", "table_row",
         "paragraph", "table", "embedded_media", "worksheet", "cell", "page", "image",
         "audio_stream", "video_stream", "audio_frame", "video_frame"}
LOCATORS = {"bytes", "json_pointer", "csv_row", "ooxml", "archive_member", "pdf_page",
            "pdf_xobject", "image_frame", "sample_range", "media_stream", "media_frame"}
ROUTE_KINDS = {
    "text": {"text_line"}, "json": {"json_object", "json_array", "json_value"},
    "csv": {"table_row"}, "docx": {"paragraph", "table", "table_row", "embedded_media"},
    "xlsx": {"worksheet", "cell"}, "pdf": {"page", "image"}, "image": {"image"},
    "wav": {"audio_stream"}, "media": {"audio_stream", "video_stream", "audio_frame", "video_frame"},
    "unknown": set(),
}


def _properties(node: dict, protocol_route: str) -> None:
    kind = node["kind"]
    props = node["properties"]
    fields = {
        "file": {"sha256", "byte_length"}, "text_line": {"text"}, "paragraph": {"text"},
        "json_object": {"length"}, "json_array": {"length"}, "json_value": {"value"},
        "table": set(), "table_row": {"fields"} if protocol_route == "csv" else {"cells"},
        "embedded_media": {"byte_length", "sha256"}, "worksheet": {"name"},
        "cell": {"cell_type", "value", "formula"}, "page": {"width_points", "height_points", "text"},
        "image": {"width", "height"} if protocol_route == "pdf" else {"width", "height", "mode", "format"},
        "audio_stream": {"sample_rate", "sample_width", "channels", "sample_count", "duration"} if protocol_route == "wav" else {"codec", "time_base"},
        "video_stream": {"codec", "time_base"},
        "video_frame": {"pts", "time_base", "width", "height", "pixel_format"},
        "audio_frame": {"pts", "time_base", "sample_count", "sample_rate", "layout"},
    }
    if set(props) != fields[kind]:
        raise StructuralError("Propriedades fora do contrato do protocolo.")
    if "text" in props and not isinstance(props["text"], str):
        raise StructuralError("Texto estrutural inválido.")
    if kind == "table_row" and any(not isinstance(value, str) for value in props[next(iter(fields[kind]))]):
        raise StructuralError("Campos de tabela inválidos.")
    for key in {"width", "height", "length", "byte_length", "sample_rate", "sample_width", "channels", "sample_count"} & set(props):
        if type(props[key]) is not int or props[key] < 0:
            raise StructuralError("Dimensão estrutural inválida.")
    if kind == "json_value" and isinstance(props["value"], (dict, list)):
        raise StructuralError("Valor JSON deve ser escalar.")


def _locator_type(kind: str, protocol_route: str) -> str:
    if kind in {"file", "text_line"}:
        return "bytes"
    if kind.startswith("json_"):
        return "json_pointer"
    if kind == "table_row" and protocol_route == "csv":
        return "csv_row"
    if kind in {"paragraph", "table", "table_row", "worksheet", "cell"}:
        return "ooxml"
    if kind == "embedded_media":
        return "archive_member"
    if kind == "page":
        return "pdf_page"
    if kind == "image":
        return "pdf_xobject" if protocol_route == "pdf" else "image_frame"
    if kind == "audio_stream" and protocol_route == "wav":
        return "sample_range"
    return "media_frame" if kind.endswith("_frame") else "media_stream"


def _location(locator: dict) -> None:
    kind = locator["type"]
    if kind not in LOCATORS:
        raise StructuralError("Tipo de localização desconhecido.")
    if kind in {"ooxml", "archive_member"}:
        member = locator.get("member")
        if not isinstance(member, str) or not member or member.startswith("/") or ".." in member.split("/") or "\\" in member:
            raise StructuralError("Localização de membro inválida.")
    if kind == "json_pointer":
        pointer = locator.get("pointer")
        if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
            raise StructuralError("JSON Pointer inválido.")
    for key, value in locator.items():
        if key not in {"type", "member", "pointer", "coordinate", "name"} and (type(value) is not int or value < 0):
            raise StructuralError("Coordenada estrutural inválida.")
    required = {"bytes": {"start", "end"}, "json_pointer": {"pointer"},
                "csv_row": {"row", "line_start", "line_end"}, "ooxml": {"member"},
                "archive_member": {"member"}, "pdf_page": {"page"},
                "pdf_xobject": {"page", "name"}, "image_frame": {"frame"},
                "sample_range": {"start", "end"}, "media_stream": {"stream"},
                "media_frame": {"stream", "frame"}}
    allowed = {"bytes": {"start", "end", "line"}, "ooxml": {"member", "body_index", "row", "sheet", "coordinate"}}
    fields = set(locator) - {"type"}
    if not required[kind] <= fields or not fields <= allowed.get(kind, required[kind]):
        raise StructuralError("Campos de localização inválidos.")
    if kind == "ooxml" and "coordinate" in locator and not re.fullmatch(r"[A-Z]+[1-9][0-9]*", locator["coordinate"]):
        raise StructuralError("Coordenada de célula inválida.")


def node_id(node: dict) -> str:
    return "obj:" + fingerprint({key: value for key, value in node.items() if key != "id"})


def graph(nodes: list[dict], source_id: str) -> dict:
    edges = []
    siblings = defaultdict(list)
    for node in nodes:
        edges.append({"subject": node["id"], "predicate": "derived_from", "object": source_id})
        if node["parent_id"] is not None:
            edges.append({"subject": node["parent_id"], "predicate": "contains", "object": node["id"]})
            siblings[node["parent_id"]].append(node["id"])
    for ids in siblings.values():
        for previous, following in zip(ids, ids[1:]):
            edges.append({"subject": previous, "predicate": "precedes", "object": following})
    return {"schema": PROVENANCE_SCHEMA, "edges": edges}


def assemble(source: dict, result: ProtocolResult) -> dict:
    """Frankenstein combines protocol observations in one structural schema."""
    nodes = []
    root = {"source_id": source["id"], "sequence": 0, "kind": "file", "parent_id": None,
            "locator": {"type": "bytes", "start": 0, "end": source["byte_length"]},
            "properties": {"sha256": source["sha256"], "byte_length": source["byte_length"]}}
    root["id"] = node_id(root)
    nodes.append(root)
    for index, unit in enumerate(result.units):
        if unit.parent is not None and (type(unit.parent) is not int or not 0 <= unit.parent < index):
            raise StructuralError("Pai do objeto deve preceder o filho.")
        parent = nodes[0 if unit.parent is None else unit.parent + 1]["id"]
        node = {"source_id": source["id"], "sequence": index + 1, "kind": unit.kind,
                "parent_id": parent, "locator": unit.locator, "properties": unit.properties}
        node["id"] = node_id(node)
        nodes.append(node)
    document = {"schema": SCHEMA, "source": source, "protocol": {
        "route": result.route, "version": 1, "status": result.status,
        "reason": result.reason, "dependencies": result.dependencies},
        "nodes": nodes, "provenance": graph(nodes, source["id"])}
    validate(document, source)
    return document


def validate(document: dict, source: dict) -> None:
    try:
        if set(document) != {"schema", "source", "protocol", "nodes", "provenance"}:
            raise StructuralError("Campos fora do contrato estrutural.")
        if document["schema"] != SCHEMA or document["source"] != source:
            raise StructuralError("Origem ou esquema divergente.")
        protocol = document["protocol"]
        if set(protocol) != {"route", "version", "status", "reason", "dependencies"} or type(protocol["version"]) is not int or protocol["version"] != 1:
            raise StructuralError("Contrato do protocolo inválido.")
        if protocol["status"] not in {"complete", "opaque"} or not isinstance(protocol["dependencies"], dict):
            raise StructuralError("Estado do protocolo inválido.")
        if protocol["route"] != route(source["name"]):
            raise StructuralError("Rota do protocolo diverge da origem.")
        if protocol["route"] == "unknown" and protocol["status"] == "complete":
            raise StructuralError("Rota desconhecida deve ser opaca.")
        if (protocol["status"] == "complete" and protocol["reason"] is not None) or (protocol["status"] == "opaque" and not isinstance(protocol["reason"], str)):
            raise StructuralError("Motivo do protocolo inválido.")
        nodes = document["nodes"]
        if not isinstance(nodes, list) or not nodes or (protocol["status"] == "opaque" and len(nodes) != 1):
            raise StructuralError("Inventário estrutural inválido.")
        seen = set()
        for index, node in enumerate(nodes):
            if set(node) != {"id", "source_id", "sequence", "kind", "parent_id", "locator", "properties"}:
                raise StructuralError("Campos inesperados no objeto.")
            if type(node["sequence"]) is not int or node["sequence"] != index or node["source_id"] != source["id"]:
                raise StructuralError("Objeto fora de ordem ou de outra origem.")
            if node["kind"] not in KINDS:
                raise StructuralError("Tipo estrutural inválido.")
            if not isinstance(node["properties"], dict) or not isinstance(node["locator"], dict) or not node["locator"].get("type"):
                raise StructuralError("Objeto sem propriedades ou localização observável.")
            _location(node["locator"])
            if index and node["kind"] not in ROUTE_KINDS[protocol["route"]]:
                raise StructuralError("Tipo de objeto fora do contrato da rota.")
            _properties(node, protocol["route"])
            if node["locator"]["type"] != _locator_type(node["kind"], protocol["route"]):
                raise StructuralError("Localização incompatível com o tipo do objeto.")
            if index == 0:
                if node["kind"] != "file" or node["parent_id"] is not None or node["locator"] != {"type": "bytes", "start": 0, "end": source["byte_length"]} or node["properties"] != {"sha256": source["sha256"], "byte_length": source["byte_length"]}:
                    raise StructuralError("Raiz do arquivo inválida.")
            elif node["parent_id"] not in seen:
                raise StructuralError("Objeto órfão ou relação cíclica.")
            if node["locator"]["type"] == "bytes":
                start, end = node["locator"]["start"], node["locator"]["end"]
                if type(start) is not int or type(end) is not int or not 0 <= start <= end <= source["byte_length"]:
                    raise StructuralError("Intervalo de bytes inválido.")
            if node["id"] != node_id(node) or node["id"] in seen:
                raise StructuralError("Identidade estrutural divergente ou repetida.")
            seen.add(node["id"])
        if document["provenance"] != graph(nodes, source["id"]):
            raise StructuralError("G_P contém relações ausentes, semânticas ou divergentes.")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise StructuralError("Documento estrutural inválido.") from exc
