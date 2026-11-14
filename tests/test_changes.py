import datetime as dt
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from defter.changes import delta
from defter.store import read, refresh


def at(hour):
    return dt.datetime(2026, 11, 14, hour, tzinfo=dt.timezone.utc)


class ChangesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(tempfile.mkdtemp())
        self.vault, self.db = self.temporary / "vault", self.temporary / "cfv.sqlite"
        self.notes = self.vault / "notes" / "topics"
        self.notes.mkdir(parents=True)
        for name in ("alpha", "beta", "gamma"):
            (self.notes / f"{name}.md").write_text(f"# {name.title()}\n\nThe {name} plan.\n")

    def tearDown(self):
        shutil.rmtree(self.temporary)

    def changes(self, since):
        return {c["reference"]: c["change"] for c in delta(self.db, since=since)["changes"]}

    def test_refresh_logs_added_changed_and_removed_records(self):
        refresh(self.db, self.vault, now=at(1))
        self.assertEqual(set(self.changes("2000-01-01").values()), {"initial"})
        self.assertEqual(self.changes("2026-11-14T01:00:00Z"), {})  # first-refresh rows keep their own dates
        (self.notes / "alpha.md").write_text("# Alpha\n\nThe alpha plan changed.\n")
        (self.notes / "beta.md").unlink()
        (self.notes / "delta.md").write_text("# Delta\n\nA new plan.\n")
        refresh(self.db, self.vault, now=at(2))
        self.assertEqual(self.changes("2026-11-14T02:00:00+00:00"),
                         {"note:topics/alpha": "changed", "note:topics/beta": "removed", "note:topics/delta": "added"})
        removed = [c for c in delta(self.db, since="2026-11-14T02:00")["changes"] if c["change"] == "removed"][0]
        self.assertEqual((removed["title"], removed["timestamp"]), ("Beta", None))
        refresh(self.db, self.vault, now=at(3))
        self.assertEqual(self.changes("2026-11-14T03:00:00Z"), {})
        (self.notes / "beta.md").write_text("# Beta\n\nBack again.\n")
        refresh(self.db, self.vault, now=at(4))
        self.assertEqual(self.changes("2026-11-14T06:30:00+03:00"), {"note:topics/beta": "added"})  # 03:30 UTC

    def test_cursor_pages_through_every_change_once(self):
        refresh(self.db, self.vault, now=at(1))
        seen, cursor, result = [], None, delta(self.db, since="2000-01-01", limit=2)
        while True:
            seen += [c["reference"] for c in result["changes"]]
            if not result["has_more"]:
                break
            result = delta(self.db, cursor=result["next_cursor"], limit=2)
        self.assertEqual(sorted(seen), ["note:topics/alpha", "note:topics/beta", "note:topics/gamma"])
        later = result["next_cursor"]
        self.assertEqual(delta(self.db, cursor=later)["changes"], [])
        (self.notes / "gamma.md").write_text("# Gamma\n\nRevised.\n")
        refresh(self.db, self.vault, now=at(2))
        self.assertEqual([c["reference"] for c in delta(self.db, cursor=later)["changes"]], ["note:topics/gamma"])
        quiet = delta(self.db, since="2030-01-01")
        self.assertEqual((quiet["changes"], quiet["next_cursor"]), ([], "2030-01-01T00:00:00Z|"))
        for bad in ({"cursor": "yesterday"}, {}):
            with self.assertRaises(ValueError):
                delta(self.db, **bad)

    def test_curated_notes_take_their_last_git_commit_date(self):
        (self.notes / "İzmir-ofis.md").write_text("# Ofis\n\nKira.\n")
        git = ["git", "-C", str(self.vault), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        env = {**os.environ, "GIT_COMMITTER_DATE": "2026-09-01T12:00:00+00:00",
               "GIT_AUTHOR_DATE": "2026-09-01T12:00:00+00:00"}
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "notes"], check=True, env=env)
        (self.notes / "draft.md").write_text("# Draft\n\nNot committed.\n")
        refresh(self.db, self.vault, now=at(1))
        office = read(self.db, "note:topics/İzmir-ofis")
        self.assertEqual((office["reference"], office["timestamp"]), ("note:topics/%C4%B0zmir-ofis", "2026-09-01T00:00:00Z"))
        initial = {c["reference"]: c["changed_at"] for c in delta(self.db, since="2000-01-01")["changes"]}
        self.assertEqual(initial["note:topics/%C4%B0zmir-ofis"], "2026-09-01T12:00:00Z")
        self.assertNotEqual(read(self.db, "topics/draft")["timestamp"][:10], "2026-09-01")

    def test_moved_vault_replaces_its_records_instead_of_duplicating_them(self):
        refresh(self.db, self.vault, now=at(1))
        moved = self.temporary / "elsewhere"
        shutil.move(self.vault, moved)
        refresh(self.db, moved, now=at(2))  # the same references from a new path would collide if both were kept
        self.assertEqual(self.changes("2026-11-14T02:00:00Z"), {})
        self.assertEqual(read(self.db, "topics/alpha")["source"], str((moved / "notes/topics/alpha.md").resolve()))

    def test_read_returns_full_text_by_reference_or_path_in_windows(self):
        (self.notes / "long.md").write_text("# Long\n\n" + "word " * 2000)
        refresh(self.db, self.vault, now=at(1))
        by_path = read(self.db, "notes/topics/long.md", limit=4000)
        self.assertEqual((by_path["reference"], by_path["total_chars"], by_path["has_more"]),
                         ("note:topics/long", 10008, True))
        rest = read(self.db, "note:topics/long", offset=4000, limit=50000)
        self.assertEqual(by_path["text"] + rest["text"], read(self.db, "topics/long", limit=50000)["text"])
        self.assertFalse(rest["has_more"])
        with self.assertRaises(ValueError):
            read(self.db, "note:topics/missing")


if __name__ == "__main__":
    unittest.main()
