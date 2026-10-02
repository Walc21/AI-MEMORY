"""Offline demo: linked evidence, conflicts, historical updates and reflection."""

import argparse
import json
from pathlib import Path
import tempfile

from mimir import Memory


def demonstrate(root):
    memory = Memory(root)
    memory.episode("Alice works at Acme in 2020.\nAcme lives in Paris.\n")
    memory.episode("Maria nasceu em 1992.\nMaria nasceu em 1993.\n")
    memory.working({"objective": "Responder com fontes verificáveis", "constraints": ["Preservar conflitos e histórico"]})
    memory.consolidate(max_items=2)
    result = memory.query("Onde fica a empresa de Alice?", save=True)
    conflict = memory.query("Quando Maria nasceu?")
    before = memory.verify()
    memory.gc(apply=True)
    rebuilt = memory.query("Onde fica a empresa de Alice?")
    assert before == memory.verify()
    assert [row["id"] for row in result["hits"]] == [row["id"] for row in rebuilt["hits"]]
    assert conflict["conflicts"]
    return {"verified": before, "multi_hop": result, "contradiction": conflict,
            "explanation": memory.explain(result["claims"][0]["assertion_id"]), "index_rebuild_equivalent": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-dir", type=Path)
    args = parser.parse_args()
    if args.memory_dir:
        result = demonstrate(args.memory_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="mimir-demo-") as temporary:
            result = demonstrate(Path(temporary) / "memory")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
