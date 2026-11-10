import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ENTRYPOINTS = {"remember.py"}


class LayoutTests(unittest.TestCase):
    def test_only_direct_clis_are_executable(self):
        found = {path.relative_to(SCRIPTS).as_posix() for path in SCRIPTS.rglob("*.py")
                 if '__name__ == "__main__"' in path.read_text()}
        self.assertEqual(found, ENTRYPOINTS)

    def test_reference_leaves_stay_small(self):
        leaves = {path.name: path.stat().st_size for path in (ROOT / "references").glob("*.md")}
        self.assertEqual(set(leaves), {"reader.md", "runtime.md", "graph.md", "history.md", "architecture.md"})
        limits = {"history.md": 8 * 1024}  # D153: new evidence behavior needs words; guard still pins regrowth
        for name, size in leaves.items():
            self.assertLessEqual(size, limits.get(name, 6 * 1024), name)
        self.assertLessEqual(sum(leaves.values()), 24 * 1024)

    def test_helpers_are_a_package(self):
        self.assertTrue((SCRIPTS / "helper" / "__init__.py").is_file())
        self.assertFalse(any("__main__" in path.read_text()
                             for path in (SCRIPTS / "helper").glob("*.py")))


if __name__ == "__main__":
    unittest.main()
