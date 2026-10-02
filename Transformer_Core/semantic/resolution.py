"""Conservative identity, explicit overrides and bitemporal normalization."""

from datetime import date
import re

from .model import Ledger, SemanticError, normalized


def valid_time(start=None, end=None) -> dict:
    if start is None and end is None:
        return {"from": None, "to": None, "precision": "unknown"}
    precision = "year"
    for value in (start, end):
        if value is None:
            continue
        if not isinstance(value, str):
            raise SemanticError("Tempo de validade deve ser textual.")
        if re.fullmatch(r"[0-9]{4}", value):
            if not 1 <= int(value) <= 9999:
                raise SemanticError("Ano inválido.")
        else:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise SemanticError("Data deve usar o formato YYYY-MM-DD.")
            try:
                date.fromisoformat(value)
                precision = "day"
            except ValueError as exc:
                raise SemanticError("Tempo deve ser ano ou data ISO.") from exc
    if start is not None and end is not None and lower(start) > upper(end):
        raise SemanticError("Intervalo temporal invertido.")
    return {"from": start, "to": end, "precision": precision}


def lower(value):
    return value + "-01-01" if value is not None and len(value) == 4 else value


def upper(value):
    return value + "-12-31" if value is not None and len(value) == 4 else value


def overlaps(first: dict, second: dict) -> bool:
    return (lower(first["from"]) or "0001-01-01") <= (upper(second["to"]) or "9999-12-31") and (lower(second["from"]) or "0001-01-01") <= (upper(first["to"]) or "9999-12-31")


def active_at(interval: dict, at: str | None):
    if at is None:
        return True
    valid_time(at)
    return (lower(interval["from"]) or "0001-01-01") <= upper(at) and (upper(interval["to"]) or "9999-12-31") >= lower(at)


def entity(ledger: Ledger, label: str, entity_type="candidate") -> str:
    if not isinstance(label, str) or not label.strip() or len(label) > 1000:
        raise SemanticError("Nome de entidade inválido.")
    # Exact normalized names only. Ambiguous aliases are not automatically merged.
    return ledger.put("entities", name=normalized(label), entity_type=entity_type)


def latest_resolutions(ledger: Ledger, as_of=None):
    output = {}
    for row in sorted(ledger.rows("resolutions"), key=lambda row: (row["transaction_time"], row["id"])):
        if not as_of or row["transaction_time"] <= as_of:
            output[row["mention_id"]] = row
    return output
