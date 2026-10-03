"""Query-time coverage of source time, without rewriting canonical history."""

import re
from .resolution import lower, upper


PAST = r"\b(?:trabalhava|trabalhou|worked|entrou|joined|morava|morou|lived|era|was)\b"


def covers_source_time(claim, at):
    if at is None or claim["predicate"] == "born_in":
        return True
    start, end, quote = claim["valid_from"], claim["valid_to"], claim["quote"]
    if end:
        return True  # active_at has already checked this explicit interval.
    if start and re.search(r"\b(?:desde|since)\s+" + re.escape(start) + r"\b", quote, re.IGNORECASE):
        if not re.search(PAST, quote, re.IGNORECASE):
            return True  # Explicit ongoing state in the source.
    if start:
        return lower(start) <= upper(at) and upper(start) >= lower(at)
    return not re.search(PAST, quote, re.IGNORECASE)


def covers_excerpt_time(text, at):
    from .extraction import rule_claims
    parsed = rule_claims(text)
    if parsed:
        return all(covers_source_time(claim, at) for claim in parsed)
    date = re.search(r"\b(?:em|in|desde|since|entre|between)\s+(\d{4}(?:-\d{2}-\d{2})?)(?:\s+(?:até|to|e|and)\s+(\d{4}(?:-\d{2}-\d{2})?))?", text, re.IGNORECASE)
    return covers_source_time({"quote": text, "predicate": "attribute", "valid_from": date[1] if date else None,
                               "valid_to": date[2] if date else None}, at)
