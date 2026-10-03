"""CLI for channels, Drive and portable MCP harness configurations."""

import json
import os
from pathlib import Path
import sys
import time

from Transformer_Core.semantic.model import SemanticError
from Transformer_Core.semantic.storage import read_json
from . import Channels


def parsers(commands, extraction):
    io = commands.add_parser("io", help="Canais persistentes de entrada e saída")
    sub = io.add_subparsers(dest="io_command", required=True)
    sub.add_parser("status")
    cancel = sub.add_parser("cancel-download", help="Cancela download interrompido antes do processamento")
    cancel.add_argument("input_key")
    configure = sub.add_parser("configure", help="Vincula metadados verificados pelo plugin Drive")
    configure.add_argument("metadata", type=Path, help="JSON com root, incoming, outgoing e account_email")
    ingest = sub.add_parser("ingest", help="Ingere arquivo já materializado em staging")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--before", type=Path, required=True)
    ingest.add_argument("--after", type=Path, required=True)
    extraction(ingest)
    receive = sub.add_parser("receive", help="Recebe JSON de saída de outro sistema")
    receive.add_argument("payload", type=Path)
    receive.add_argument("--topic", default="system")
    pending = sub.add_parser("pending")
    pending.add_argument("--limit", type=int, default=100)
    ack = sub.add_parser("acknowledge")
    ack.add_argument("output_id")
    ack.add_argument("metadata", type=Path)
    sub.add_parser("export-memory")
    drive = commands.add_parser("drive", help="API oficial Google Drive com OAuth próprio")
    sub = drive.add_subparsers(dest="drive_command", required=True)
    auth = sub.add_parser("auth")
    auth.add_argument("--client-secrets", type=Path, required=True)
    auth.add_argument("--token-file", type=Path)
    auth.add_argument("--port", type=int, default=0)
    auth.add_argument("--no-browser", action="store_true")
    for name in ("bootstrap", "sync"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--token-file", type=Path)
        if name == "sync":
            cmd.add_argument("--watch", action="store_true")
            cmd.add_argument("--interval", type=int, default=30)
            cmd.add_argument("--reprocess", action="store_true")
            extraction(cmd)
    mcp = commands.add_parser("mcp", help="MCPs separados de I/O e memória para harnesses")
    sub = mcp.add_subparsers(dest="mcp_command", required=True)
    server = sub.add_parser("serve")
    server.add_argument("--role", choices=["io", "memory"], default="memory")
    server.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    server.add_argument("--allow-write", action="store_true")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--token-file", type=Path)
    token = sub.add_parser("token")
    token.add_argument("output", type=Path)
    config = sub.add_parser("config", help="Gera comandos absolutos para um harness instalado")
    config.add_argument("--client", choices=["json", "codex"], default="json")
    config.add_argument("--output", type=Path)
    config.add_argument("--allow-write", action="store_true")


def dispatch(args, memory):
    from ..__main__ import _settings
    channels = Channels(memory)
    if args.command == "io":
        cmd = args.io_command
        if cmd == "status":
            return channels.status()
        if cmd == "cancel-download":
            return channels.cancel_download(args.input_key)
        if cmd == "configure":
            values = read_json(args.metadata)
            required = {"root", "incoming", "outgoing", "account_email"}
            if (not isinstance(values, dict) or not required <= values.keys()
                    or any(not isinstance(values[key], dict) for key in ("root", "incoming", "outgoing"))
                    or not isinstance(values["account_email"], str)):
                raise SemanticError("Configuração requer root, incoming e outgoing como objetos, e account_email como texto.")
            return channels.configure(values["root"], values["incoming"], values["outgoing"], values["account_email"])
        if cmd == "ingest":
            return channels.ingest_file(args.path, read_json(args.before), read_json(args.after), **_settings(args))
        if cmd == "receive":
            return channels.enqueue(read_json(args.payload), args.topic)
        if cmd == "pending":
            return {"jobs": channels.pending(args.limit)}
        if cmd == "acknowledge":
            return channels.acknowledge(args.output_id, read_json(args.metadata))
        return channels.export_memory()
    if args.command == "drive":
        from .drive import DriveAPI, DriveSync, authorize
        if args.drive_command == "auth":
            return authorize(channels, args.client_secrets, args.token_file, args.port, not args.no_browser)
        sync = DriveSync(channels, DriveAPI.from_channels(channels, args.token_file))
        if args.drive_command == "bootstrap":
            return sync.bootstrap()
        if args.interval < 1:
            raise SemanticError("Intervalo de sincronização deve ser positivo.")
        if not args.watch:
            return sync.sync(force=args.reprocess, **_settings(args))
        try:
            while True:
                try:
                    result = sync.sync(force=args.reprocess, **_settings(args))
                except (SemanticError, OSError) as exc:
                    result = {"complete": False, "error": str(exc)}
                print(json.dumps(result, ensure_ascii=True, allow_nan=False), flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return {"stopped": True}
    from ..mcp_server import serve, token_create
    if args.mcp_command == "serve":
        serve(memory, args.role, args.transport, args.allow_write, args.host, args.port, args.token_file)
        return None
    if args.mcp_command == "token":
        return token_create(args.output)
    servers = {}
    for role in ("io", "memory"):
        argv = ["-m", "mimir", "--memory-dir", str(memory.store.base), "--namespace", memory.store.namespace,
                "mcp", "serve", "--role", role]
        if args.allow_write and role == "memory":
            argv.append("--allow-write")
        servers["mimir-" + role] = {"command": sys.executable, "args": argv}
    if args.client == "json":
        result = {"mcpServers": servers}
        if not args.output:
            return result
        text = json.dumps(result, ensure_ascii=True, indent=2) + "\n"
    else:
        # JSON's surrogate-pair escapes are not valid TOML Unicode scalars.
        # Literal UTF-8 retains paths containing emoji and other non-BMP text.
        text = "\n".join(f"[mcp_servers.{name}]\ncommand = {json.dumps(value['command'], ensure_ascii=False)}\nargs = {json.dumps(value['args'], ensure_ascii=False)}\n" for name, value in servers.items())
    if args.output:
        descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        return {"config_file": str(args.output.absolute()), "client": args.client}
    print(text)
    return None
