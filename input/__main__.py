"""The sole public entry point for starting an AI MEMORY Input."""

import argparse
import json
import os
from pathlib import Path

from BN1_1.Pacote.cache import CacheError, Pacote
from Transformer_Core.Hot_Hub.hub import HubError
from Transformer_Core.structural.model import Limits, PIPELINE_VERSION, StructuralError


def main() -> int:
    parser = argparse.ArgumentParser(description="Janela manual de Input do AI MEMORY")
    parser.add_argument("--version", action="version", version=f"AI MEMORY / Mimir {PIPELINE_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("open", help="Abre um novo Input")
    add = commands.add_parser("add", help="Copia arquivos reais para o Pacote")
    add.add_argument("files", nargs="+", type=Path)
    commands.add_parser("close", help="Fecha e publica os chunks de bytes no Hot Hub")
    commands.add_parser("status", help="Mostra o estado sem abrir arquivos")
    transform = commands.add_parser("transform", help="Avança do Hot Hub até Curadoria/G_P e BN1_2")
    transform.add_argument("--strict", action="store_true", help="Recusa qualquer resultado opaco")
    transform.add_argument("--force", action="store_true", help="Publica uma nova geração mesmo com saída íntegra")
    transform.add_argument("--max-file-bytes", type=int, default=Limits().max_file_bytes)
    transform.add_argument("--max-nodes", type=int, default=Limits().max_nodes)
    commands.add_parser("verify", help="Verifica Pacote, Hot Hub, objetos e G_P do BN1_2")
    inspect = commands.add_parser("inspect", help="Lista protocolos ou mostra os objetos de uma origem")
    inspect.add_argument("--source", help="Nome canônico, por exemplo 1_aB7.pdf")
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
            print(f"Input fechado. n={pacote.close()}; Hot Hub verificado, representações publicadas e cache do BBN1_1 liberado.")
            counts = pacote.status()["representations"]
            print(f"Arquivos: {counts['files']}; bytes: {counts['bytes']}; chunks: {counts['chunks']}.")
        elif args.command == "transform":
            limits = Limits(max_file_bytes=args.max_file_bytes, max_nodes=args.max_nodes)
            manifest = pacote.transform(limits, args.strict, args.force)
            print(json.dumps({"status": "BN1_2_READY", "generation": manifest["generation"],
                              "summary": manifest["summary"]}, ensure_ascii=False))
        elif args.command == "verify":
            manifest = pacote.verify_transform()
            print(json.dumps({"verified": True, "generation": manifest["generation"],
                              "summary": manifest["summary"]}, ensure_ascii=False))
        elif args.command == "inspect":
            print(json.dumps(pacote.inspect_transform(args.source), ensure_ascii=True, indent=2))
        else:
            print(json.dumps(pacote.status(), ensure_ascii=False))
    except (CacheError, HubError, StructuralError, OSError, ValueError) as exc:
        parser.exit(1, f"Erro: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
