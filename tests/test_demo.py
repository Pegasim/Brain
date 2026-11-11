import asyncio
import os
import sqlite3
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from brain_demo.store import ROOT, TOOLS, lookup, refresh

COMMAND = [sys.executable, "-m", "brain_demo"]


class DemoTest(unittest.TestCase):
    def test_distiller_uses_bundled_writer_and_selected_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            script = """
import runpy
writer = runpy.run_path('third_party/second-brain/tools/distill/brain_distill.py')
entry = dict(kind='decision', topic='demo', title='Use Monday reviews',
             context='A schedule was changed.', result='Reviews use Mondays.',
             evidence='session:codex:demo-review-update', next='')
ok, details = writer['brain_add'](entry, 'demo.md', 'demo', date='2026-10-30')
assert ok, details
"""
            subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True,
                           env={**os.environ, "BRAIN_REPO": str(vault)}, capture_output=True)
            self.assertEqual(len(list((vault / "inbox").glob("*.md"))), 1)

    def test_existing_agent_can_retrieve_linked_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "demo.sqlite"
            command = [*COMMAND, "load", "--db", str(db)]
            subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
            before = asyncio.run(self.read_demo(db))
            subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
            self.assertEqual(asyncio.run(self.read_demo(db)), before)

    async def read_demo(self, db):
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "brain_demo", "serve", "--db", str(db)], cwd=ROOT,
        )
        async with Client(params) as client:
            tools = (await client.list_tools()).tools
            self.assertTrue(all(tool.annotations.read_only_hint for tool in tools))
            names = {tool.name for tool in tools}
            self.assertEqual(names, {"lookup", "search", "when", "timeline", "digest", "people", "photos", "week_page"})
            main = await self.call(client, "demo review", "2026-10-30")
            by_kind = {row["kind"]: row for row in main}
            self.assertEqual(len(main), 3)
            self.assertTrue(by_kind["calendar"]["reference"].endswith("demo-review%402026-10-30T07%3A00%3A00%2B00%3A00"))
            self.assertIn("Monday, November 2", by_kind["brain_session"]["excerpt"])
            self.assertEqual(by_kind["brain_note"]["supports"], ["session:codex:demo-review-update"])
            linked = await self.call(client, "scheduling decision", "2026-10-30")
            self.assertEqual({row["kind"] for row in linked}, {"brain_note", "brain_session"})
            calendar_only = await self.call(client, "release review", "2026-11-06")
            self.assertEqual([row["kind"] for row in calendar_only], ["calendar"])
            native = await client.call_tool("search", {"query": "demo review"})
            self.assertFalse(native.is_error)
            self.assertIn("brain_session", str(native.content))
            return main

    async def call(self, client, topic, day):
        result = await client.call_tool("lookup", {"topic": topic, "start": day, "end": day})
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(result.structured_content["missing_references"], [])
        return result.structured_content["evidence"]

    def test_redacted_archive_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            extractor = runpy.run_path(str(TOOLS / "extract/brain_extract.py"))
            session = extractor["parse_codex"](ROOT / "fixtures/demo-session.jsonl")
            archive = temporary / "archive"
            archive.mkdir()
            (archive / "conversation.md").write_text(extractor["render"](session)[0])
            db = temporary / "cfv.sqlite"
            refresh(db, temporary / "vault", archive=archive, source_name="archive")
            result = lookup(db, "demo review")["evidence"]
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["kind"], "brain_conversation")
            self.assertEqual(result[0]["reference"], "session:archive:codex:demo-review-update")

    def test_load_preserves_other_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "existing.sqlite"
            db.write_text("keep this file")
            result = subprocess.run([*COMMAND, "load", "--db", str(db)], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to replace", result.stderr)
            self.assertEqual(db.read_text(), "keep this file")

    def test_maintenance_to_cfv_and_failed_refresh_preserves_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            db, vault = temporary / "cfv.sqlite", temporary / "vault"
            trace = temporary / "traces"
            trace.mkdir()
            fixture = ROOT / "fixtures/demo-session.jsonl"
            (trace / fixture.name).write_bytes(fixture.read_bytes())
            add = [*COMMAND, "memory", "--vault", str(vault), "add",
                   "--kind", "decision", "--topic", "demo", "--title", "Monday scheduling decision",
                   "--context", "A review schedule changed.", "--result", "Future demo reviews will be on Mondays.",
                   "--evidence", "session:codex:demo-review-update", "--date", "2026-10-30"]
            for _ in range(2):
                subprocess.run(add, cwd=ROOT, check=True, capture_output=True)
            self.assertEqual(len(list((vault / "inbox").glob("*.md"))), 1)
            subprocess.run([*COMMAND, "memory", "--vault", str(vault), "consolidate"],
                           cwd=ROOT, check=True, capture_output=True)
            refresh(db, vault, [ROOT / "fixtures/demo-calendar.ics"], trace)
            before = lookup(db, "scheduling decision")
            self.assertEqual({r["kind"] for r in before["evidence"]}, {"brain_note", "brain_session"})
            refresh(db, vault, sessions=trace)
            self.assertEqual(lookup(db, "scheduling decision"), before)
            self.assertEqual(len(lookup(db, "release review")["evidence"]), 1)
            # A duplicate session ID aborts the staged import, preserving the live snapshot.
            (trace / "duplicate.jsonl").write_bytes(fixture.read_bytes())
            with self.assertRaises(sqlite3.IntegrityError):
                refresh(db, vault, sessions=trace)
            self.assertEqual(lookup(db, "scheduling decision"), before)
            # A private refreshed database cannot be replaced by the fixture loader.
            result = subprocess.run([*COMMAND, "load", "--db", str(db)], cwd=ROOT, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            # Removing the source leaves a visibly unresolved note citation.
            (trace / "duplicate.jsonl").unlink()
            (trace / fixture.name).unlink()
            refresh(db, vault, sessions=trace)
            self.assertEqual(lookup(db, "scheduling decision")["missing_references"],
                             ["session:codex:demo-review-update"])


if __name__ == "__main__":
    unittest.main()
