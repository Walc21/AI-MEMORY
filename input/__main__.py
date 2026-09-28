"""The sole public entry point for starting an AI MEMORY Input."""

import argparse
import json
import os
from pathlib import Path

from BN1_1.Pacote.cache import CacheError, Pacote


def main() -> int:
    parser = argparse.ArgumentParser(description="Janela manual de Input do AI MEMORY")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("open", help="Abre um novo Input")
    add = commands.add_parser("add", help="Copia arquivos reais para o Pacote")
    add.add_argument("files", nargs="+", type=Path)
    commands.add_parser("close", help="Fecha, nomeia, classifica e armazena no BBN1_1")
    commands.add_parser("status", help="Mostra o estado sem abrir arquivos")
    args = parser.parse_args()

    runtime = Path(os.environ.get("MIMIR_RUNTIME_DIR", Path(__file__).resolve().parents[1] / ".mimir-runtime"))
    pacote = Pacote(runtime)
    try:
        if args.command == "open":
            pacote.open()
            print("Input aberto.")
        elif args.command == "add":
            print(f"Arquivos recebidos no ciclo: {pacote.add(args.files)}")
        elif args.command == "close":
            print(f"Input fechado. n={pacote.close()}; arquivos organizados no BBN1_1 por extensão.")
        else:
            print(json.dumps(pacote.status(), ensure_ascii=False))
    except (CacheError, OSError) as exc:
        parser.exit(1, f"Erro: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
