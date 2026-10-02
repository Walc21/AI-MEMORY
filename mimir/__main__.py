"""Complete local ingestion, semantic memory, query and audit workflow."""

import argparse
import json
import os
from pathlib import Path

from BN1_1.Pacote.cache import CacheError, Pacote
from Transformer_Core.Hot_Hub.hub import HubError
from Transformer_Core.structural.model import StructuralError
from Transformer_Core.semantic.model import COLLECTIONS, SemanticError
from . import Memory, __version__


def _extraction(parser):
    parser.add_argument("--extractor", choices=["rules", "ollama"], default="rules")
    parser.add_argument("--model", help="Modelo local do Ollama")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--ocr-language", default="eng")
    parser.add_argument("--asr-model", help="Diretório local de modelo faster-whisper")
    parser.add_argument("--vision-model", help="Modelo de visão local do Ollama")
    parser.add_argument("--video-stride", type=int, default=30, help="Processa um frame a cada N frames")
    parser.add_argument("--strict-multimodal", action="store_true")
    parser.add_argument("--signing-key", type=Path)


def _settings(args):
    return {key: getattr(args, key) for key in ("extractor", "model", "endpoint", "ocr", "ocr_language", "asr_model", "vision_model", "video_stride", "strict_multimodal", "signing_key")}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mimir: bytes → evidência → memória semântica → resposta auditável")
    parser.add_argument("--version", action="version", version=f"AI MEMORY / Mimir {__version__}")
    parser.add_argument("--runtime", type=Path, default=Path(os.environ.get("MIMIR_RUNTIME_DIR", ".mimir-runtime")))
    parser.add_argument("--memory-dir", type=Path, default=Path(os.environ.get("MIMIR_MEMORY_DIR", ".mimir-memory")))
    parser.add_argument("--namespace", default=os.environ.get("MIMIR_NAMESPACE", "default"))
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Executa todo o pipeline em um ciclo novo")
    run.add_argument("files", nargs="+", type=Path)
    run.add_argument("--query", help="Consulta a memória após a ingestão e salva a saída")
    _extraction(run)
    semantic = commands.add_parser("semantic", help="Avança um BN1_2 existente até Semantic Core")
    semantic.add_argument("--force", action="store_true")
    _extraction(semantic)
    query = commands.add_parser("query", help="Busca híbrida com evidências e contexto limitado")
    query.add_argument("question")
    query.add_argument("--at", dest="valid_at")
    query.add_argument("--as-of")
    query.add_argument("--history", action="store_true")
    query.add_argument("--audit", action="store_true")
    query.add_argument("--k", type=int, default=8)
    query.add_argument("--budget-chars", type=int, default=8000)
    query.add_argument("--embedding-model")
    query.add_argument("--embedding-revision")
    query.add_argument("--save", action="store_true")
    query.add_argument("--text", action="store_true")
    commands.add_parser("verify", help="Verifica ledger, histórico, evidências e bytes")
    inspect = commands.add_parser("inspect", help="Inspeciona uma coleção canônica")
    inspect.add_argument("collection", choices=list(COLLECTIONS), nargs="?", default="assertions")
    explain = commands.add_parser("explain", help="Percorre assertion → evidência → fonte")
    explain.add_argument("assertion_id")
    index = commands.add_parser("reindex", help="Reconstrói índices sem modificar a memória")
    index.add_argument("--embedding-model")
    index.add_argument("--embedding-revision")
    episode = commands.add_parser("episode", help="Registra interação como evidência persistente")
    episode.add_argument("text")
    episode.add_argument("--type", default="interaction")
    _extraction(episode)
    consolidation = commands.add_parser("consolidate", help="Cria reflexões hierárquicas sem apagar origens")
    consolidation.add_argument("--type", choices=["reflective", "procedural", "community"], default="reflective")
    consolidation.add_argument("--max-items", type=int, default=20)
    consolidation.add_argument("--signing-key", type=Path)
    update = commands.add_parser("update", help="Registra supersession ou retratação explícita")
    update.add_argument("new_assertion")
    update.add_argument("old_assertion")
    update.add_argument("--relation", choices=["supersedes", "retracts"], default="supersedes")
    update.add_argument("--signing-key", type=Path)
    resolution = commands.add_parser("resolve", help="Acrescenta resolução humana reversível")
    resolution.add_argument("mention_id")
    resolution.add_argument("entity_name")
    resolution.add_argument("--type", choices=["candidate", "person", "organization", "place"], default="candidate")
    resolution.add_argument("--signing-key", type=Path)
    working = commands.add_parser("working", help="Lê ou configura Working Memory")
    working.add_argument("--json", dest="values", help="Objeto JSON com identity, objective, project, constraints etc.")
    source = commands.add_parser("export-source", help="Recupera os bytes originais por content_id")
    source.add_argument("content_id")
    source.add_argument("output", type=Path)
    gc = commands.add_parser("gc", help="Lista órfãos e índices descartáveis; preserva histórico canônico")
    gc.add_argument("--apply", action="store_true")
    policy = commands.add_parser("policy", help="Inspeciona/configura ACL por UID POSIX")
    policy.add_argument("--readers", nargs="*", type=int)
    policy.add_argument("--writers", nargs="*", type=int)
    keys = commands.add_parser("keygen", help="Cria chave Ed25519 local; não mostra a chave")
    keys.add_argument("output", type=Path)
    evaluation = commands.add_parser("eval", help="Executa benchmark local ou dataset JSON")
    evaluation.add_argument("--dataset", type=Path)
    args = parser.parse_args(argv)
    try:
        memory = Memory(args.memory_dir, args.namespace)
        if args.command == "run":
            pacote = Pacote(args.runtime)
            pacote.open()
            pacote.add(args.files)
            result = memory.ingest(args.runtime, **_settings(args))
            if args.query:
                result = memory.query(args.query, save=True)
        elif args.command == "semantic":
            result = memory.ingest(args.runtime, force=args.force, **_settings(args))
        elif args.command == "query":
            result = memory.query(args.question, save=args.save, k=args.k, valid_at=args.valid_at, as_of=args.as_of,
                                  history=args.history, audit=args.audit, budget_chars=args.budget_chars,
                                  model=args.embedding_model, revision=args.embedding_revision)
            if args.text:
                print(result["answer"])
                for index, hit in enumerate(result["hits"], 1):
                    print(f"[{index}] {hit['citation']['source_name']} — {json.dumps(hit['citation']['locator'], ensure_ascii=False)}")
                return 0
        elif args.command == "verify":
            result = memory.verify()
        elif args.command == "inspect":
            result = memory.inspect(args.collection)
        elif args.command == "explain":
            result = memory.explain(args.assertion_id)
        elif args.command == "reindex":
            result = memory.reindex(args.embedding_model, args.embedding_revision)
        elif args.command == "episode":
            result = memory.episode(args.text, args.type, **_settings(args))
        elif args.command == "consolidate":
            result = memory.consolidate(args.type, args.max_items, args.signing_key)
        elif args.command == "update":
            result = memory.relate(args.new_assertion, args.old_assertion, args.relation, args.signing_key)
        elif args.command == "resolve":
            result = memory.resolve_entity(args.mention_id, args.entity_name, args.type, args.signing_key)
        elif args.command == "working":
            result = memory.working(json.loads(args.values) if args.values else None)
        elif args.command == "export-source":
            result = memory.source(args.content_id, args.output)
        elif args.command == "gc":
            result = memory.gc(args.apply)
        elif args.command == "policy":
            result = memory.policy(args.readers, args.writers)
        elif args.command == "keygen":
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            key = Ed25519PrivateKey.generate()
            descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            result = {"algorithm": "ed25519", "private_key_file": str(args.output)}
        else:
            from Transformer_Core.semantic.evaluation import evaluate
            result = evaluate(args.dataset)
        print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
        return 0
    except (CacheError, HubError, StructuralError, SemanticError, OSError, ValueError, ImportError) as exc:
        parser.exit(1, f"Erro: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
