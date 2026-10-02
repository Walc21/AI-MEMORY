"""Run the existing four-format fixtures through Curadoria/G_P and BN1_2."""

import argparse
import json
from pathlib import Path
import tempfile

from BN1_1.Pacote.cache import Pacote
from examples.hot_hub_four_formats import create_examples


def run(runtime: Path) -> dict:
    pacote = Pacote(runtime)
    pacote.open()
    sources = create_examples(runtime / "demo-inputs")
    pacote.add(sources)
    manifest = pacote.transform(strict=True)
    pacote.verify_transform()
    print(json.dumps({"generation": manifest["generation"], "summary": manifest["summary"]}, indent=2))
    print(json.dumps(pacote.inspect_transform(), indent=2))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, help="Preserva o ciclo neste diretório novo")
    args = parser.parse_args()
    if args.runtime:
        run(args.runtime)
    else:
        with tempfile.TemporaryDirectory(prefix="mimir-demo-") as temporary:
            run(Path(temporary))


if __name__ == "__main__":
    main()
