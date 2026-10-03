"""Exercise the actual MCP SDK, subprocess transport and authenticated HTTP."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

from mimir import Memory
from mimir.mcp_server import token_create, _token
from Transformer_Core.semantic.model import SemanticError

HAS_MCP = importlib.util.find_spec("mcp") is not None
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(HAS_MCP, "Install [mcp] for protocol acceptance tests")
class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "memory"
        self.memory = Memory(self.base)
        self.memory.episode("Alice works at Acme.")

    def args(self, role="memory", *extra):
        return ["-m", "mimir", "--memory-dir", str(self.base), "mcp", "serve", "--role", role, *extra]

    async def test_stdio_memory_readonly_real_queries_explanations_and_resource(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryFile(mode="w+") as errors:
            async with stdio_client(StdioServerParameters(command=sys.executable, args=self.args(), cwd=str(ROOT)), errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.serverInfo.name, "Mimir memory")
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    self.assertIn("memory_query", names)
                    self.assertNotIn("memory_remember", names)
                    self.assertNotIn("io_receive", names)
                    result = await session.call_tool("memory_query", {"question": "Where does Alice work?", "budget_chars": 2000})
                    self.assertFalse(result.isError)
                    data = result.structuredContent
                    self.assertLessEqual(data["context"]["chars"], 2000)
                    self.assertFalse(data["abstained"])
                    for question in ["What is Alice favorite color?", "When was Alice born?", "Onde Bob trabalha?"]:
                        absent = await session.call_tool("memory_query", {"question": question})
                        self.assertFalse(absent.isError)
                        self.assertTrue(absent.structuredContent["abstained"])
                        self.assertEqual(absent.structuredContent["claims"], [])
                        self.assertEqual(absent.structuredContent["context"]["untrusted_evidence"], [])
                    explained = await session.call_tool("memory_explain", {"assertion_id": data["claims"][0]["assertion_id"]})
                    self.assertTrue(explained.structuredContent["evidence"])
                    resource = await session.read_resource("mimir://working")
                    self.assertTrue(resource.contents)
                    invalid = await session.call_tool("memory_query", {"question": "Alice", "budget_chars": 999999})
                    self.assertTrue(invalid.isError)

    async def test_stdio_io_accepts_system_output_and_restarts_with_same_queue(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        identity = None
        for attempt in range(2):
            with tempfile.TemporaryFile(mode="w+") as errors:
                async with stdio_client(StdioServerParameters(command=sys.executable, args=self.args("io"), cwd=str(ROOT)), errlog=errors) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        names = {tool.name for tool in (await session.list_tools()).tools}
                        self.assertIn("io_receive", names)
                        self.assertNotIn("memory_query", names)
                        job = await session.call_tool("io_receive", {"payload": {"answer": "Alice works at Acme"}})
                        self.assertFalse(job.isError)
                        if attempt == 0:
                            identity = job.structuredContent["id"]
                        self.assertEqual(identity, job.structuredContent["id"])
                        pending = await session.call_tool("io_pending", {})
                        self.assertEqual(len(pending.structuredContent["jobs"]), 1)

    async def test_explicit_write_mode_persists_harness_episode(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryFile(mode="w+") as errors:
            async with stdio_client(StdioServerParameters(command=sys.executable, args=self.args("memory", "--allow-write"), cwd=str(ROOT)), errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("memory_remember", {"text": "Bob works at Beta."})
                    self.assertFalse(result.isError)
        self.assertFalse(self.memory.query("Where does Bob work?")["abstained"])
        self.assertTrue(self.memory.verify()["verified"])

    async def test_streamable_http_requires_token_and_serves_real_mcp(self):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        token_path = self.root / "access.token"
        token_create(token_path)
        token = _token(token_path)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with tempfile.TemporaryFile(mode="w+") as errors:
            process = subprocess.Popen([sys.executable, *self.args("memory", "--transport", "streamable-http", "--port", str(port), "--token-file", str(token_path))], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                url = f"http://127.0.0.1:{port}/mcp"
                async with httpx.AsyncClient(trust_env=False) as client:
                    for attempt in range(100):
                        if process.poll() is not None:
                            errors.seek(0)
                            self.fail(errors.read())
                        try:
                            response = await client.get(url)
                            break
                        except httpx.ConnectError:
                            await asyncio.sleep(.05)
                    else:
                        self.fail("HTTP MCP did not start")
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual((await client.get(url, headers={"Authorization": "Bearer wrong"})).status_code, 401)
                    self.assertEqual((await client.get(url, headers={"Authorization": b"Bearer \xff"})).status_code, 401)
                async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
                    bad_host = await client.post(url, headers={"Host": "evil.example"}, json={})
                    self.assertIn(bad_host.status_code, (400, 421))
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

    def test_http_rejects_missing_public_or_unsafe_credentials(self):
        from mimir.mcp_server import build_server
        with self.assertRaises(SemanticError):
            _token(None)
        with self.assertRaises(SemanticError):
            build_server(self.memory, host="0.0.0.0")
        path = self.root / "token"
        token_create(path)
        path.chmod(0o644)
        with self.assertRaises(SemanticError):
            _token(path)

    def test_generated_harness_config_contains_absolute_paths_and_real_commands(self):
        result = subprocess.run([sys.executable, "-m", "mimir", "--memory-dir", str(self.base), "mcp", "config"], cwd=ROOT, check=True, text=True, capture_output=True)
        config = json.loads(result.stdout)
        for name, settings in config["mcpServers"].items():
            self.assertEqual(settings["command"], sys.executable)
            self.assertIn(str(self.base), settings["args"])
            self.assertIn(name.removeprefix("mimir-"), settings["args"])


if __name__ == "__main__":
    unittest.main()
