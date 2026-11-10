"""Cache eviction: idle entries expire, recent reads survive, sweeps are throttled."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper.graph_store import CACHE_TTL_SECONDS, SCHEMA, Store, prune


class CacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "cache"
        (Path(tmp.name) / "repo").mkdir()
        self.store = Store(Path(tmp.name) / "repo", self.base)

    def age(self, key, seconds):
        path = self.store.path / (key.replace(":", "-") + ".json")
        old = time.time() - seconds
        os.utime(path, (old, old))
        return path

    def test_idle_entries_expire_and_empty_workspaces_go(self):
        key = self.store.save("snapshot", {"x": 1})
        path = self.age(key, CACHE_TTL_SECONDS + 60)
        self.assertEqual(prune(self.base, now=time.time() + 2 * 86400), 1)
        self.assertFalse(path.exists())
        self.assertFalse(self.store.path.exists())
        with self.assertRaisesRegex(ValueError, "superseded"):
            self.store.load(key)

    def test_read_refreshes_age(self):
        key = self.store.save("snapshot", {"schema": SCHEMA, "x": 2})
        path = self.age(key, CACHE_TTL_SECONDS + 60)
        self.store.load_stored(key)
        prune(self.base, now=time.time() + 2 * 86400)
        self.assertTrue(path.exists())

    def test_new_capture_supersedes_older_snapshot(self):
        repo = Path(self.store.root)
        (repo / "a.py").write_text("x = 1\n")
        old, _ = self.store.snapshot()
        (repo / "a.py").write_text("x = 2\n")
        new, _ = self.store.snapshot()
        self.assertNotEqual(old, new)
        self.assertEqual(len(list(self.store.path.glob("snapshot-*.json"))), 1)
        with self.assertRaisesRegex(ValueError, "superseded"):
            self.store.load(old)
        self.store.load(new)

    def test_old_schema_reference_fails_and_current_capture_rebuilds(self):
        repo = Path(self.store.root)
        (repo / "a.py").write_text("x = 1\n")
        key, _ = self.store.snapshot()
        stale = self.store.save("snapshot", {"schema": 0})
        with self.assertRaisesRegex(ValueError, "old schema"):
            self.store.load(stale)
        lookup = next(self.store.path.glob("source-*.json"))
        lookup.write_text('{"snapshot": "%s"}' % stale)
        rebuilt, body = self.store.snapshot()
        self.assertNotEqual(rebuilt, stale)
        self.assertEqual(body["schema"], SCHEMA)
        self.store.load(rebuilt)

    def test_sweep_runs_at_most_once_per_interval(self):
        self.store.save("snapshot", {"x": 3})  # first save sweeps and stamps the marker
        key = self.store.save("snapshot", {"x": 4})
        self.age(key, CACHE_TTL_SECONDS + 60)
        self.assertEqual(prune(self.base), 0)


if __name__ == "__main__":
    unittest.main()
