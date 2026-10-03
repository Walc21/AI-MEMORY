"""Deterministic record views of G_P, with every component's original locator.

These are semantic projections, never edits to the structural document. No joins
cross a file, array record, table or worksheet. Formulae are never evaluated.
"""

from collections import defaultdict
import json
import re


PROPERTY = "projection:record-v1"
FIELD_PREFIX = "projection:field-v1:"


def scalar(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)


class DocumentViews:
    def __init__(self, document):
        self.records = {}
        nodes = document["nodes"]
        self.children = defaultdict(list)
        for node in nodes:
            self.children[node["parent_id"]].append(node)
        route = document["protocol"]["route"]
        if route == "json":
            for node in nodes:
                if node["kind"] == "json_object":
                    fields, components = {}, []
                    self._json(node, "", fields, components)
                    self._put(node, node["locator"]["pointer"] or "/", fields, components)
        elif route in {"csv", "docx"}:
            tables = defaultdict(list)
            for node in nodes:
                if node["kind"] == "table_row":
                    tables[node["parent_id"]].append(node)
            for rows in tables.values():
                self._table(rows, "fields" if route == "csv" else "cells")
        elif route == "xlsx":
            for sheet in nodes:
                if sheet["kind"] != "worksheet":
                    continue
                rows = defaultdict(dict)
                for node in self.children[sheet["id"]]:
                    match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", node["locator"].get("coordinate", ""))
                    if match:
                        rows[int(match[2])][match[1]] = node
                if not rows:
                    continue
                header_number = min(rows)
                header = rows[header_number]
                labels = [n["properties"]["value"] for n in header.values()]
                if not self._header(labels):
                    continue
                for number in sorted(rows):
                    if number == header_number:
                        continue
                    fields, components = {}, []
                    for column, node in rows[number].items():
                        if column not in header:
                            continue
                        value = node["properties"]["value"]
                        # An absent cached result is unknown, even if a formula
                        # exists. A formula's source text is not its value.
                        if value is not None:
                            fields[header[column]["properties"]["value"]] = value
                            components.extend([header[column], node])
                    representative = next(iter(rows[number].values()))
                    self._put(representative, f"{sheet['properties']['name']} / row {number}", fields, components)

    def _json(self, node, prefix, fields, components):
        for child in self.children[node["id"]]:
            token = child["locator"]["pointer"].rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
            key = prefix + token
            if child["kind"] == "json_value":
                fields[key] = child["properties"]["value"]
                components.append(child)
            elif child["kind"] == "json_object":
                self._json(child, key + ".", fields, components)
            # Arrays define a record boundary; siblings are never flattened.

    @staticmethod
    def _header(labels):
        return len(labels) >= 2 and all(isinstance(v, str) and v.strip() and
            not re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", v.strip()) for v in labels) and len(set(labels)) == len(labels)

    def _table(self, rows, key):
        labels = rows[0]["properties"][key]
        if not self._header(labels):
            return
        for row in rows[1:]:
            values = row["properties"][key]
            if len(values) == len(labels):
                self._put(row, f"row {row['locator']['row']}", dict(zip(labels, values)), [rows[0], row])

    def _put(self, node, path, fields, components):
        if not fields:
            return
        text = "Registro " + path + "\n" + json.dumps(fields, ensure_ascii=False, allow_nan=False)
        self.records[node["id"]] = {"text": text, "path": path, "fields": fields,
            "method": "structural-record-v1", "components": [
                {"node_id": n["id"], "locator": n["locator"], "properties": n["properties"]}
                for n in {n["id"]: n for n in components}.values()]}

    def properties(self, node_id):
        record = self.records.get(node_id)
        if not record:
            return {}
        output = {PROPERTY: record}
        identity_keys = {"id", "name", "nome", "project", "projeto", "sensor", "item", "product", "produto", "person", "pessoa", "user", "usuario", "code", "codigo"}
        from .model import normalized
        identifiers = {k: v for k, v in record["fields"].items() if normalized(k).rsplit(".", 1)[-1] in identity_keys}
        if not identifiers:
            first = next(iter(record["fields"]))
            identifiers = {first: record["fields"][first]}
        for key, value in record["fields"].items():
            fields = {**identifiers, key: value}
            output[FIELD_PREFIX + key.encode("utf-8").hex()] = {**record,
                "text": "Registro " + record["path"] + "\n" + json.dumps(fields, ensure_ascii=False, allow_nan=False),
                "fields": fields, "target_field": key, "target_value": value}
        return output


def for_occurrence(ledger, occurrence):
    cache = getattr(ledger, "_record_views", None)
    if cache is None:
        cache = ledger._record_views = {}
    if occurrence["id"] not in cache:
        cache[occurrence["id"]] = DocumentViews(occurrence["document"])
    return cache[occurrence["id"]]
