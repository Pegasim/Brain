import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from brain_demo.store import ROOT


class DemoTest(unittest.TestCase):
    def test_existing_agent_can_retrieve_linked_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "demo.sqlite"
            command = [sys.executable, "-m", "brain_demo", "load", "--db", str(db)]
            subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
            before = asyncio.run(self.read_demo(db))
            subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
            self.assertEqual(asyncio.run(self.read_demo(db)), before)

    async def read_demo(self, db):
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "brain_demo", "serve", "--db", str(db)],
            cwd=ROOT,
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
                self.assertEqual([tool.name for tool in tools], ["lookup"])
                self.assertTrue(tools[0].annotations.readOnlyHint)
                main = await self.lookup(client, "demo review", "2026-10-30")
                by_kind = {row["kind"]: row for row in main}
                self.assertEqual(len(main), 3)
                self.assertEqual(by_kind["calendar_plan"]["reference"], "calendar:demo-review")
                self.assertIn("Monday, November 2", by_kind["user_statement"]["excerpt"])
                self.assertEqual(by_kind["curated_decision"]["supports"], ["session:demo-review-update"])
                linked = await self.lookup(client, "scheduling decision", "2026-10-30")
                self.assertEqual({row["kind"] for row in linked}, {"curated_decision", "user_statement"})
                calendar_only = await self.lookup(client, "release review", "2026-11-06")
                self.assertEqual([row["kind"] for row in calendar_only], ["calendar_plan"])
                return main

    async def lookup(self, client, topic, day):
        result = await client.call_tool("lookup", {"topic": topic, "start": day, "end": day})
        self.assertFalse(result.isError, result.content)
        self.assertEqual(result.structuredContent["missing_references"], [])
        return result.structuredContent["evidence"]

    def test_load_preserves_other_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "existing.sqlite"
            db.write_text("keep this file")
            result = subprocess.run(
                [sys.executable, "-m", "brain_demo", "load", "--db", str(db)],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to replace", result.stderr)
            self.assertEqual(db.read_text(), "keep this file")
