"""Fail-closed answer selection, separate from similarity and candidate ranking.

Proof here means explicit source support in a closed relation grammar or an exact
record field. It does not mean that a source's report is true in the world.
"""

import re

from .evidence import node_for, reference, resolve, texts
from .extraction import rule_claims
from .language import spans, terms, uncertain
from .model import normalized
from .resolution import latest_resolutions
from .views import FIELD_PREFIX, PROPERTY, for_occurrence

RELATION_TERMS = {"works_at": {"work"}, "lives_in": {"live"},
                  "born_in": {"born"}, "responsible_for": {"responsible"}}
QUESTION = r"\b(?:qual|quais|quem|quando|onde|como|quanto|quantos|what|which|who|when|where|how|does|did|can)\b"
UNSUPPORTED = r"\b(?:compare|comparar|diferenca|difference|maior|menor|largest|smallest|average|media|total|soma|sum|todos|todas|all|quantos|quantas|why|porque|por que)\b"


def intent(query):
    query_terms = terms(query)
    value = normalized(query)
    relation = next((p for p, words in RELATION_TERMS.items() if words & query_terms), None)
    if not relation and (re.search(r"^quem\s+.+\s+e\??$", value) or re.search(r"^(?:who|what)\s+is\s+\w+[?]?$", value)):
        relation = "is"
    temporal = bool(re.search(r"\b(?:quando|when|data|date|ano|year)\b", value))
    if temporal:
        query_terms -= {"data", "date", "ano", "year"}
    return {"question": bool(re.search(QUESTION, value) or "?" in query),
            "terms": query_terms, "predicate": relation,
            "time": temporal,
            "place": bool(re.search(r"\b(?:onde|where|lugar|place|cidade|city)\b", value)),
            "reverse": bool(re.search(r"^(?:quem|who)\b", value)),
            "unsupported": bool(re.search(UNSUPPORTED, value)),
            "multi_hop": bool(re.search(r"(?:onde.*empresa.*de|where.*(?:company|employer).*of)\b", value))}


def aliases(ledger, query, as_of):
    """Explicit, temporally available overrides; never substring name matching."""
    substitute = query
    by_name = {e["id"]: e["name"] for e in ledger.rows("entities")}
    for row in latest_resolutions(ledger, as_of).values():
        if not row["human_override"]:
            continue
        name = by_name[row["entity_id"]]
        original = ledger.get("mentions", row["mention_id"])["text"]
        substitute = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", original, normalized(substitute))
    return substitute


def proven_claim(ledger, assertion):
    proposition = ledger.get("propositions", assertion["proposition_id"])
    subject = ledger.get("entities", proposition["subject"])["name"]
    obj = ledger.get("entities", proposition["object"]["value"])["name"] if proposition["object"]["kind"] == "entity" else proposition["object"]["value"]
    for evidence in assertion["evidence"]:
        for parsed in rule_claims(evidence["quote"]):
            if (normalized(parsed["subject"]) == subject and parsed["predicate"] == proposition["predicate"] and
                normalized(parsed["object"]) == normalized(obj) and parsed["object_type"] == proposition["object"]["kind"] and
                parsed["polarity"] == assertion["polarity"] and
                parsed["valid_from"] == assertion["valid_time"]["from"] and parsed["valid_to"] == assertion["valid_time"]["to"]):
                return parsed
    return None


def matches_claim(request, parsed):
    if re.search(r"\b(?:unknown|unavailable|desconhecid[oa]|indispon[ií]vel|n[aã]o\s+(?:informad[oa]|registrad[oa]))\b", parsed["object"], re.IGNORECASE):
        return False
    if parsed["polarity"] == "negative" and (request["place"] or request["time"]):
        return False
    if request["predicate"] and parsed["predicate"] != request["predicate"]:
        return False
    if request["question"]:
        subject_terms = terms(parsed["subject"])
        if request["predicate"]:
            if not subject_terms <= request["terms"] and not request["reverse"]:
                return False
        elif parsed["predicate"] != "is" or request["terms"] != subject_terms or re.search(r"\b(?:and|e|or|ou)\b|;", parsed["subject"], re.IGNORECASE):
            return False
    if request["time"]:
        if parsed["predicate"] == "born_in":
            if not parsed["valid_from"] and not re.fullmatch(r"\d{4}(?:-\d{2}-\d{2})?", parsed["object"]):
                return False
        elif not parsed["valid_from"]:
            return False
    if request["place"] and parsed["predicate"] == "born_in" and re.fullmatch(r"\d{4}(?:-\d{2}-\d{2})?", parsed["object"]):
        return False
    # Every requested content term must be in the *same* statement, including
    # requested object names. A name-only hit cannot establish another field.
    return bool(request["terms"]) and request["terms"] <= terms(parsed["quote"])


def select(ledger, eligible, rows, query, as_of=None, audit=False):
    request = intent(aliases(ledger, query, as_of))
    accepted, claim_ids = {}, set()
    if request["unsupported"]:
        return accepted, claim_ids, {"status": "unsupported", "reason": "comparison_aggregation_or_exhaustive_request"}
    verified = {key: proven_claim(ledger, value) for key, value in eligible.items()}
    verified = {key: value for key, value in verified.items() if value}
    blocked = {}
    if not audit:
        for assertion in ledger.rows("assertions"):
            if assertion["id"] not in eligible and (not as_of or assertion["transaction_time"] <= as_of):
                for ev in assertion["evidence"]:
                    blocked.setdefault((ev["binding_id"], ev["property"]), []).append((ev["char_start"], ev["char_end"]))
    if request["multi_hop"]:
        # Exactly one permitted two-edge relation chain; PPR alone is no proof.
        subject_terms = request["terms"] - {"work", "live", "company", "employer", "fica"}
        for first, a in verified.items():
            if a["predicate"] != "works_at" or a["polarity"] != "positive" or not subject_terms or not subject_terms <= terms(a["subject"]):
                continue
            for second, b in verified.items():
                if b["predicate"] == "lives_in" and b["polarity"] == "positive" and normalized(a["object"]) == normalized(b["subject"]):
                    claim_ids.update([first, second])
    else:
        claim_ids = {key for key, parsed in verified.items() if matches_claim(request, parsed)}
    # Attach only the verified assertion's exact sentence, not an arbitrary
    # chunk containing other subjects or predicates.
    for identity, row in rows.items():
        matched = [key for key in row["active_assertion_ids"] if key in claim_ids]
        evidence = []
        for key in matched:
            evidence.extend(eligible[key]["evidence"])
        ev = row["evidence"]
        occurrence, node = node_for(ledger, ev["binding_id"])
        views = for_occurrence(ledger, occurrence)
        if ev["property"].startswith("projection:"):
            view = views.properties(node["id"])[ev["property"]]
            if not request["question"] and ev["property"] == PROPERTY:
                if request["terms"] and request["terms"] <= terms(view["text"]):
                    evidence.append(reference(ledger, ev["binding_id"], ev["property"], view["text"]))
            elif request["question"] and ev["property"].startswith(FIELD_PREFIX):
                target = view["target_value"]
                field_terms = terms(view["target_field"])
                if target is not None and target != "" and not uncertain(str(target)) and field_terms & request["terms"] and request["terms"] <= terms(view["text"]):
                    evidence.append(reference(ledger, ev["binding_id"], ev["property"], view["text"]))
        elif not request["multi_hop"]:
            if ev["property"].startswith("observation:"):
                full_text = ledger.get("observations", ev["property"].split(":", 1)[1])["text"]
            else:
                full_text = dict(texts(node)).get(ev["property"], "")
            for lo, hi in spans(full_text):
                if lo >= ev["char_end"] or hi <= ev["char_start"]:
                    continue
                if any(start < hi and end > lo for start, end in blocked.get((ev["binding_id"], ev["property"]), [])):
                    continue
                sentence = full_text[lo:hi]
                if not request["terms"] or not request["terms"] <= terms(sentence):
                    continue
                if not request["question"]:
                    evidence.append(reference(ledger, ev["binding_id"], ev["property"], full_text, lo, hi))
                elif not request["predicate"] and not sentence.endswith("?") and not uncertain(sentence):
                    # Generic factual excerpts need a declarative value marker;
                    # mere keyword co-occurrence or a heading is insufficient.
                    marker = re.search(r"\b(?:is|are|was|tem|possui|custa|mede|equivale|ser[aá]|é)\b|[:=]", sentence, re.IGNORECASE)
                    tail = sentence[marker.end():].strip(" .") if marker else ""
                    missing = re.search(r"\b(?:unknown|unavailable|desconhecid[oa]|indispon[ií]vel|n[aã]o\s+(?:informad[oa]|registrad[oa]|dispon[ií]vel)|not\s+(?:known|recorded|available))\b", tail, re.IGNORECASE)
                    prefix = sentence[:marker.start()] if marker else ""
                    compound = re.search(r"\b(?:and|e|or|ou)\b|;", prefix, re.IGNORECASE)
                    if marker and terms(prefix) == request["terms"] and terms(tail) - request["terms"] and not missing and not compound:
                        evidence.append(reference(ledger, ev["binding_id"], ev["property"], full_text, lo, hi))
        if evidence:
            unique = {(e["binding_id"], e["property"], e["char_start"], e["char_end"]): e for e in evidence}
            accepted[identity] = {"evidence": list(unique.values()), "assertion_ids": matched}
    reason = "explicit_source_support" if accepted else "requested_fact_not_supported"
    return accepted, claim_ids, {"status": "supported" if accepted else "insufficient", "reason": reason,
        "predicate": request["predicate"], "answer_mode": "extractive", "grammar": "pt-en-closed-v1"}
