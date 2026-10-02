"""Build a portable local plugin ZIP with real installed commands and private state paths."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-dir", type=Path, required=True)
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-write", action="store_true")
    args = parser.parse_args()
    template = Path(__file__).resolve().parents[1] / "plugins/mimir-memory"
    if args.output.resolve().is_relative_to(template.resolve()):
        parser.exit(1, "Erro: salve o ZIP fora da pasta do plugin para não incluir o próprio arquivo.\n")
    command = [sys.executable, "-m", "mimir", "--memory-dir", str(args.memory_dir.absolute()),
               "--namespace", args.namespace, "mcp", "config"]
    if args.allow_write:
        command.append("--allow-write")
    generated = subprocess.run(command, text=True, capture_output=True)
    if generated.returncode:
        parser.exit(1, generated.stderr or "Erro: não foi possível gerar a configuração MCP.\n")
    config = generated.stdout
    config = json.loads(config)
    config["$schema"] = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    for server in config["mcpServers"].values():
        server["type"] = "stdio"
    try:
        with zipfile.ZipFile(args.output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(template.rglob("*")):
                if path.is_file():
                    name = path.relative_to(template).as_posix()
                    archive.writestr("mimir-memory/" + name, json.dumps(config, indent=2) if name == "mcp.json" else path.read_bytes())
    except OSError as exc:
        parser.exit(1, f"Erro: {exc}\n")
    print(json.dumps({"plugin_zip": str(args.output.absolute()), "credential_free": True}))


if __name__ == "__main__":
    main()
