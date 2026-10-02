"""G_M/G_S projections, conservative conflicts and associative PageRank."""

from collections import defaultdict

from .resolution import overlaps

# Generic descriptions ("is an engineer", "is Brazilian") can coexist.
FUNCTIONAL = {"born_in"}


def conflicts(ledger, transaction_time):
    rows = ledger.rows("assertions")
    groups = defaultdict(list)
    for assertion in rows:
        proposition = ledger.get("propositions", assertion["proposition_id"])
        groups[(proposition["subject"], proposition["predicate"])].append((assertion, proposition))
    for group in groups.values():
        for index, (first, p1) in enumerate(group):
            for second, p2 in group[index + 1:]:
                if not overlaps(first["valid_time"], second["valid_time"]):
                    continue
                opposite = p1["id"] == p2["id"] and first["polarity"] != second["polarity"]
                exclusive = p1["predicate"] in FUNCTIONAL and p1["object"] != p2["object"] and first["polarity"] == second["polarity"] == "positive"
                if opposite or exclusive:
                    left, right = sorted((first["id"], second["id"]))
                    # The identity is stable: conflict discovery does not rewrite
                    # assertions or continually add timestamp-only duplicates.
                    ledger.put("relations", subject=left, predicate="conflicts_with", object=right,
                               method="polarity_or_functional_v1", transaction_time=max(first["transaction_time"], second["transaction_time"]))


def graph_projection(ledger, as_of=None, allowed_assertions=None):
    gm = []
    gs = []
    for mention in ledger.rows("mentions"):
        gm.append({"subject": mention["id"], "predicate": "grounded_in", "object": mention["evidence"]["anchor_id"]})
    for resolution in ledger.rows("resolutions"):
        if as_of and resolution["transaction_time"] > as_of:
            continue
        gm.append({"subject": resolution["mention_id"], "predicate": "candidate_member_of", "object": resolution["entity_id"], "resolution_id": resolution["id"]})
    for assertion in ledger.rows("assertions"):
        if as_of and assertion["transaction_time"] > as_of or allowed_assertions is not None and assertion["id"] not in allowed_assertions:
            continue
        proposition = ledger.get("propositions", assertion["proposition_id"])
        gs.extend([{"subject": assertion["id"], "predicate": "asserts", "object": proposition["id"]},
                   {"subject": proposition["id"], "predicate": "has_subject", "object": proposition["subject"]}])
        if proposition["object"]["kind"] == "entity":
            gs.append({"subject": proposition["id"], "predicate": "has_object", "object": proposition["object"]["value"]})
    from .resolution import latest_resolutions
    from .model import normalized
    by_name = {row["name"]: row["id"] for row in ledger.rows("entities")}
    for resolution in latest_resolutions(ledger, as_of).values():
        mention = ledger.get("mentions", resolution["mention_id"])
        candidate = by_name.get(normalized(mention["text"]))
        if candidate and candidate != resolution["entity_id"]:
            gs.append({"subject": candidate, "predicate": "candidate_resolved_as",
                       "object": resolution["entity_id"], "resolution_id": resolution["id"]})
    gs.extend({"subject": row["subject"], "predicate": row["predicate"], "object": row["object"]} for row in ledger.rows("relations") if not as_of or row["transaction_time"] <= as_of)
    return {"schema": "mimir.mention-graph.v1", "edges": gm}, {"schema": "mimir.semantic-graph.v1", "edges": gs}


def adjacency(ledger, as_of=None, allowed_assertions=None):
    links = defaultdict(set)
    _, graph = graph_projection(ledger, as_of, allowed_assertions)
    for edge in graph["edges"]:
        links[edge["subject"]].add(edge["object"])
        links[edge["object"]].add(edge["subject"])
    return links


def personalized_pagerank(ledger, seeds, steps=20, damping=0.85, as_of=None, allowed_assertions=None):
    graph = adjacency(ledger, as_of, allowed_assertions)
    seeds = {key: value for key, value in seeds.items() if key in graph and value > 0}
    total = sum(seeds.values())
    if not total:
        return {}
    restart = {key: value / total for key, value in seeds.items()}
    ranks = dict(restart)
    for _ in range(steps):
        next_ranks = {key: (1 - damping) * value for key, value in restart.items()}
        for node, weight in ranks.items():
            neighbours = graph.get(node, set())
            if neighbours:
                for neighbour in neighbours:
                    next_ranks[neighbour] = next_ranks.get(neighbour, 0) + damping * weight / len(neighbours)
            else:
                for key, value in restart.items():
                    next_ranks[key] = next_ranks.get(key, 0) + damping * weight * value
        ranks = next_ranks
    return ranks
