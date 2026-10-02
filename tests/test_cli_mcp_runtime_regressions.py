"""Regressions for portable harness configuration and executable MCP boundaries."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import zipfile

from mimir import Memory
from mimir.mcp_server import _token, token_create
from Transformer_Core.semantic.model import SemanticError

ROOT = Path(__file__).resolve().parents[1]
HAS_MCP = importlib.util.find_spec("mcp") is not None


class CLIRuntimeRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "🧠 memória"

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "mimir", "--memory-dir", str(self.base),
                               "--namespace", "audit-test", *args], cwd=ROOT,
                              text=True, capture_output=True, timeout=30)

    def test_malformed_drive_configuration_is_a_reported_error_without_traceback(self):
        config = self.root / "folders.json"
        for value in ({}, [], {"root": {}}, {"root": "folder", "incoming": {}, "outgoing": {}, "account_email": "a@example.test"},
                      {"root": {}, "incoming": {}, "outgoing": {}, "account_email": "a@example.test"}):
            with self.subTest(value=value):
                config.write_text(json.dumps(value), encoding="utf-8")
                result = self.cli("io", "configure", str(config))
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("Erro:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse((self.base / "audit-test/IO/drive.json").exists())

    def test_harness_config_preserves_unicode_and_namespace_in_json_and_toml(self):
        for client in ("json", "codex"):
            with self.subTest(client=client):
                target = self.root / (client + ".config")
                result = self.cli("mcp", "config", "--client", client, "--output", str(target), "--allow-write")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
                text = target.read_text(encoding="utf-8")
                if client == "json":
                    servers = json.loads(text)["mcpServers"]
                else:
                    self.assertNotIn("\\ud83e", text)
                    # Python 3.10 has no tomllib. These JSON literals are also
                    # TOML literals; 3.11+ additionally validates the full file.
                    if importlib.util.find_spec("tomllib") is not None:
                        import tomllib
                        servers = tomllib.loads(text)["mcp_servers"]
                    else:
                        servers = {}
                        for block in text.strip().split("\n\n"):
                            lines = block.splitlines()
                            name = lines[0].removeprefix("[mcp_servers.").removesuffix("]")
                            servers[name] = {key: json.loads(value) for key, value in (line.split(" = ", 1) for line in lines[1:])}
                self.assertEqual(set(servers), {"mimir-io", "mimir-memory"})
                for name, server in servers.items():
                    argv = server["args"]
                    self.assertEqual(server["command"], sys.executable)
                    self.assertEqual(argv[argv.index("--memory-dir") + 1], str(self.base))
                    self.assertEqual(argv[argv.index("--namespace") + 1], "audit-test")
                    self.assertEqual("--allow-write" in argv, name == "mimir-memory")
                original = target.read_bytes()
                repeat = self.cli("mcp", "config", "--client", client, "--output", str(target))
                self.assertEqual(repeat.returncode, 1)
                self.assertNotIn("Traceback", repeat.stderr)
                self.assertEqual(target.read_bytes(), original)

    def test_http_token_is_never_silently_truncated_or_decoded_as_another_value(self):
        path = self.root / "access.token"
        token_create(path)
        self.assertEqual(_token(path), path.read_text().strip())
        for data in (b"a" * 256 + b"different-suffix", b"a" * 40 + b"\xff"):
            with self.subTest(data_length=len(data)):
                path.write_bytes(data)
                with self.assertRaises(SemanticError):
                    _token(path)
                result = self.cli("mcp", "serve", "--transport", "streamable-http", "--token-file", str(path))
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)

    @unittest.skipUnless(HAS_MCP, "Install [mcp] for runtime validation")
    def test_invalid_mcp_port_fails_before_starting_the_server(self):
        path = self.root / "access.token"
        token_create(path)
        for port in (-1, 65536):
            result = self.cli("mcp", "serve", "--transport", "streamable-http", "--port", str(port), "--token-file", str(path))
            self.assertEqual(result.returncode, 1)
            self.assertIn("Porta MCP", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def package(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "tools/package_mimir_plugin.py"),
                               "--memory-dir", str(self.base), *args], cwd=ROOT,
                              text=True, capture_output=True, timeout=30)

    def test_packaged_plugin_contains_real_config_and_reports_invalid_namespace(self):
        output = self.root / "plugin.zip"
        invalid = self.package("--namespace", "../other", "--output", str(output))
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("Namespace", invalid.stderr)
        self.assertNotIn("Traceback", invalid.stderr)
        self.assertFalse(output.exists())
        result = self.package("--namespace", "audit-test", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(output) as archive:
            self.assertIsNone(archive.testzip())
            config = json.loads(archive.read("mimir-memory/mcp.json"))
            self.assertEqual(config["mcpServers"]["mimir-memory"]["type"], "stdio")
            self.assertIn(str(self.base), config["mcpServers"]["mimir-memory"]["args"])
        before = output.read_bytes()
        repeat = self.package("--output", str(output))
        self.assertEqual(repeat.returncode, 1)
        self.assertNotIn("Traceback", repeat.stderr)
        self.assertEqual(output.read_bytes(), before)

    def test_plugin_cannot_accidentally_archive_itself(self):
        checkout = self.root / "checkout"
        script = checkout / "tools/package_mimir_plugin.py"
        script.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "tools/package_mimir_plugin.py", script)
        template = checkout / "plugins/mimir-memory"
        template.mkdir(parents=True)
        (template / "plugin.json").write_text("{}")
        output = template / "plugin.zip"
        result = subprocess.run([sys.executable, str(script), "--memory-dir", str(self.base), "--output", str(output)],
                                cwd=ROOT, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(output.exists())


@unittest.skipUnless(HAS_MCP, "Install [mcp] for protocol regressions")
class MCPRuntimeRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "🧠 memória"
        self.memory = Memory(self.base, "audit-test")
        self.memory.episode("Alice works at Acme.")

    async def test_generated_harness_launches_from_other_directory_and_recovers_after_tool_error(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        result = subprocess.run([sys.executable, "-m", "mimir", "--memory-dir", str(self.base), "--namespace", "audit-test",
                                 "mcp", "config"], cwd=ROOT, text=True, capture_output=True, check=True, timeout=30)
        config = json.loads(result.stdout)["mcpServers"]
        unrelated = self.root / "different-working-directory"
        unrelated.mkdir()
        for role in ("memory", "io"):
            server = config["mimir-" + role]
            with tempfile.TemporaryFile(mode="w+") as errors:
                async with stdio_client(StdioServerParameters(command=server["command"], args=server["args"], cwd=str(unrelated)), errlog=errors) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        if role == "memory":
                            bad = await session.call_tool("memory_query", {"question": "Alice", "budget_chars": 255})
                            self.assertTrue(bad.isError)
                            good = await session.call_tool("memory_query", {"question": "Where does Alice work?"})
                            self.assertFalse(good.isError)
                            self.assertEqual(good.structuredContent["claims"][0]["object"], "acme")
                        else:
                            bad = await session.call_tool("io_configure", {"root": {}, "incoming": {}, "outgoing": {}, "account_email": "a@example.test"})
                            self.assertTrue(bad.isError)
                            good = await session.call_tool("io_receive", {"payload": {"answer": "Auditoria concluída 🧠"}})
                            self.assertFalse(good.isError)
                            self.assertEqual(len((await session.call_tool("io_pending", {})).structuredContent["jobs"]), 1)
        self.assertFalse((self.base / "default").exists())
        self.assertFalse((unrelated / ".mimir-memory").exists())

    async def test_ipv6_http_origin_is_accepted_by_real_mcp_server(self):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        try:
            with socket.socket(socket.AF_INET6) as probe:
                probe.bind(("::1", 0))
                port = probe.getsockname()[1]
        except OSError:
            self.skipTest("IPv6 loopback unavailable")
        token_path = self.root / "access.token"
        token_create(token_path)
        args = [sys.executable, "-m", "mimir", "--memory-dir", str(self.base), "--namespace", "audit-test", "mcp", "serve",
                "--transport", "streamable-http", "--host", "::1", "--port", str(port), "--token-file", str(token_path)]
        with tempfile.TemporaryFile(mode="w+") as errors:
            process = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                url = f"http://[::1]:{port}/mcp"
                headers = {"Authorization": "Bearer " + _token(token_path), "Origin": f"http://[::1]:{port}"}
                async with httpx.AsyncClient(trust_env=False, headers=headers) as client:
                    for _ in range(100):
                        if process.poll() is not None:
                            errors.seek(0)
                            self.fail(errors.read())
                        try:
                            await client.get(url)
                            break
                        except httpx.ConnectError:
                            await asyncio.sleep(0.05)
                    else:
                        self.fail("MCP IPv6 did not start")
                    async with streamable_http_client(url, http_client=client) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool("memory_query", {"question": "Alice"})
                            self.assertFalse(result.isError)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    unittest.main()
