"""Error signatures: exact final line and normalized family (D128)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from helper import error_signatures


class SignatureTests(unittest.TestCase):
    def test_traceback_final_line_and_family_normalization(self):
        text = ("Traceback (most recent call last):\n  File \"/tmp/x1/a.py\", line 3, in f\n"
                "KeyError: 'name' at 0x7f00ab /tmp/pytest-12/out.json:14:2")
        (exact, family), = error_signatures.signature(text)
        self.assertTrue(exact.startswith("KeyError: 'name'"))
        self.assertEqual(family, "KeyError: 'name' at <n> <tmp>")
        other = error_signatures.signature("KeyError: 'name' at 0x1 /tmp/pytest-99/out.json:7")[0][1]
        self.assertEqual(family, other)

    def test_codes_and_cap(self):
        text = "\n".join(["error: one", "src/a.ts(1,2): TS2304 cannot find", "E501 line too long",
                          "fatal: four", "ValueError: five"])
        self.assertEqual(len(error_signatures.signature(text)), 3)
        self.assertEqual(error_signatures.signature("all good"), [])


if __name__ == "__main__":
    unittest.main()
