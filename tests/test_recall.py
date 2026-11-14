import hashlib
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from defter.recall import embed, recall
from defter.store import ROOT, refresh

SYNONYMS = {"port": "harbour"}


def fake_embedder(calls=None):
    """Bag of words hashed into 64 dimensions; 'port' and 'harbour' share one, standing in for meaning."""
    def run(texts):
        if calls is not None:
            calls.append(len(texts))
        vectors = []
        for text in texts:
            vector = [0.0] * 64
            for word in re.findall(r"\w+", text.lower()):
                word = SYNONYMS.get(word, word)
                vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % 64] += 1
            vectors.append(vector)
        return vectors
    return run


def failing_embedder(texts):
    raise OSError("connection refused")


class RecallTest(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(tempfile.mkdtemp())
        self.vault, self.db = self.temporary / "vault", self.temporary / "cfv.sqlite"
        notes = self.vault / "notes"
        (notes / "topics").mkdir(parents=True)
        (notes / "log").mkdir()
        (notes / "topics" / "harbour.md").write_text("# Harbour pilot\n\nThe harbour pilot runs weekly audits at the dock.\n")
        (notes / "topics" / "canteen.md").write_text("# Canteen\n\nLunch menus rotate every Monday.\n")
        (notes / "topics" / "izmir.md").write_text(
            "---\ntitle: İzmir ofisi\nupdated: 2026-11-02\n---\nİzmir ofisinin kira sözleşmesi Aralık'ta yenilenir.\n")
        (notes / "log" / "old-audit.md").write_text(
            "---\nid: abc123\ndate: 2026-10-01\ntitle: Old audit\n---\n# Old audit\n\nThe audit schedule moved to Fridays.\n")
        filler = " ".join(f"filler{i}" for i in range(300))
        (notes / "topics" / "long.md").write_text(f"# Long page\n\n{filler} the secret dock timetable is posted.\n")
        refresh(self.db, self.vault)

    def tearDown(self):
        shutil.rmtree(self.temporary)

    def test_keyword_recall_works_before_embedding(self):
        result = recall(self.db, "Which weekly audits does the pilot run?")
        self.assertFalse(result["semantic"])
        self.assertIn("run embed", result["warning"])
        top = result["evidence"][0]
        self.assertEqual((top["path"], top["reference"], top["matched"]), ("topics/harbour", "note:topics/harbour", ["keyword"]))

    def test_turkish_words_match_across_dotless_i_diacritics_and_suffixes(self):
        top = recall(self.db, "ızmir kira sozlesmesi ne zaman?")["evidence"][0]
        self.assertEqual((top["path"], top["title"]), ("topics/izmir", "İzmir ofisi"))

    def test_embedding_is_cached_by_passage_across_refreshes(self):
        calls = []
        first = embed(self.db, fake_embedder(calls), model="fake")
        self.assertEqual(first["embedded"], first["passages"])
        self.assertEqual(first["passages"], 6)  # five notes, the 300-word page split into two windows
        calls.clear()
        refresh(self.db, self.vault)  # notes get new event ids; vectors are keyed by passage text
        self.assertEqual(embed(self.db, fake_embedder(calls), model="fake")["embedded"], 0)
        self.assertEqual(calls, [])
        (self.vault / "notes" / "topics" / "canteen.md").unlink()
        refresh(self.db, self.vault)
        self.assertEqual(embed(self.db, fake_embedder(calls), model="fake")["pruned"], 1)

    def test_meaning_finds_a_note_without_shared_words_and_cites_its_best_passage(self):
        embed(self.db, fake_embedder(), model="bge-m3")
        result = recall(self.db, "port", embedder=fake_embedder())
        self.assertTrue(result["semantic"])
        top = result["evidence"][0]
        self.assertEqual((top["path"], top["matched"]), ("topics/harbour", ["semantic"]))
        long = recall(self.db, "secret dock timetable", embedder=fake_embedder())["evidence"][0]
        self.assertEqual((long["path"], long["matched"]), ("topics/long", ["keyword", "semantic"]))
        self.assertIn("secret dock timetable", long["excerpt"])
        self.assertNotIn("filler0 ", long["excerpt"])

    def test_failed_embedding_server_falls_back_to_keywords(self):
        embed(self.db, fake_embedder(), model="bge-m3")
        result = recall(self.db, "weekly audits", embedder=failing_embedder)
        self.assertFalse(result["semantic"])
        self.assertIn("OSError", result["warning"])
        self.assertEqual(result["evidence"][0]["path"], "topics/harbour")

    def test_dates_filter_both_rankings(self):
        embed(self.db, fake_embedder(), model="bge-m3")
        result = recall(self.db, "audit schedule", start="2026-10-01", end="2026-10-01", embedder=fake_embedder())
        self.assertEqual([r["reference"] for r in result["evidence"]], ["note:abc123"])

    def test_notes_come_before_conversations(self):
        trace = self.temporary / "traces"
        trace.mkdir()
        shutil.copy(ROOT / "fixtures/demo-session.jsonl", trace)
        (self.vault / "notes" / "topics" / "review.md").write_text("# Reviews\n\nThe review moved.\n")
        refresh(self.db, self.vault, sessions=trace)
        kinds = [r["kind"] for r in recall(self.db, "demo review moved to Monday")["evidence"]]
        self.assertIn("brain_session", kinds)
        self.assertEqual(kinds, sorted(kinds, key=lambda kind: kind != "brain_note"))
        with self.assertRaises(ValueError):
            recall(self.db, "x", start="2026-11-02", end="2026-11-01")


if __name__ == "__main__":
    unittest.main()
