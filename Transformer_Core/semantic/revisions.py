"""Current-document policy for explicitly identified external source revisions."""


def current_bindings(ledger, as_of=None, history=False):
    if history:
        return {b["id"] for b in ledger.rows("bindings")}
    latest, origins = {}, {}
    for run in ledger.rows("inference_runs"):
        if as_of and run["timestamp"] > as_of:
            continue
        for content, metadata in run["parameters"].get("source_metadata", {}).items():
            if not metadata.get("provider") or not metadata.get("file_id") or not metadata.get("revision"):
                continue
            key = (metadata["provider"], metadata.get("account_fingerprint"), metadata["file_id"])
            position = (metadata.get("modified_time") or metadata.get("modifiedTime") or run["timestamp"], run["timestamp"])
            origins[(run["input_generation"], content)] = (key, metadata["revision"])
            if key not in latest or position > latest[key][0]:
                latest[key] = (position, metadata["revision"])
    active = set()
    for binding in ledger.rows("bindings"):
        occurrence = ledger.get("occurrences", binding["occurrence_id"])
        upstream = ledger.get("upstreams", occurrence["upstream_id"])
        origin = origins.get((upstream["bn_manifest"]["generation"], occurrence["content_id"]))
        if origin is None or origin[1] == latest[origin[0]][1]:
            active.add(binding["id"])
    return active
