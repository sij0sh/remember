"""M2 retrieval units: features, probe, schedule hits."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper import evidence_candidates as ec
from helper.recording_scan import schedule


class FeatureTests(unittest.TestCase):
    def test_paths_shas_and_errors(self):
        root = "/work/repo"
        text = ("Edit /work/repo/src/app.py and ./lib/util.ts, not /etc/hosts or https://x.io/a.js; "
                "see commit 3f9ab21 not 1234567. ValueError: bad")
        found = ec.features(text, root)
        self.assertLessEqual({("path", "src/app.py"), ("path", "lib/util.ts"), ("sha", "3f9ab21")}, found)
        self.assertIn(("error_exact", " ".join(text.split())), found)
        self.assertEqual(ec.features("see ~nosuchuser9/x.py", root), set())  # unknown ~user: no crash
        self.assertFalse({v for k, v in found if k == "path"} & {"/etc/hosts", "etc/hosts", "a.js"})
        self.assertNotIn(("sha", "1234567"), found)

    def test_needles_skip_short_shas_and_words(self):
        seeds = [ec.seed("path", "src/app.py", "path"), ec.seed("sha", "abc12", "prior_commit"),
                 ec.seed("error_family", "KeyError: <n>", "error")]
        self.assertEqual(ec.needles(seeds), [b"src/app.py"])


class ProbeTests(unittest.TestCase):
    def test_needle_across_chunk_boundary_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a.jsonl"), Path(tmp, "b.jsonl")
            a.write_bytes(b"x" * 10 + b"SRC/APP.PY" + b"y" * 10)
            b.write_bytes(b"z" * 40)
            with mock.patch.object(ec, "CHUNK", 13):
                hits, cov = ec.probe([(str(a), 30, 2), (str(b), 40, 1)], [b"src/app.py"], 1000)
            self.assertEqual(hits, {str(a): 1})
            self.assertEqual((cov["files_probed"], cov["bytes_probed"]), (2, 70))
            hits, cov = ec.probe([(str(a), 30, 2), (str(b), 40, 1)], [b"src/app.py"], 30)
            self.assertEqual((cov["files_probed"], cov["files_unprobed"]), (1, 1))

    def test_schedule_puts_hits_first_in_match_lane(self):
        groups = [("pi", ["n1", "n2", "n3", "old"], {})]
        order, _ = schedule(groups, 4, hits={"old": 2, "n3": 1})
        self.assertEqual([p for _, p, lane in order if lane == "match"], ["old", "n3"])
        self.assertEqual(schedule(groups, 4)[0][0], ("pi", "n1", "match"))


if __name__ == "__main__":
    unittest.main()
