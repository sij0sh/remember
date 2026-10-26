import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ENTRYPOINTS = {"sessions.py", "rows.py", "remember.py"}


class LayoutTests(unittest.TestCase):
    def test_only_direct_clis_are_executable(self):
        found = {path.relative_to(SCRIPTS).as_posix() for path in SCRIPTS.rglob("*.py")
                 if '__name__ == "__main__"' in path.read_text()}
        self.assertEqual(found, ENTRYPOINTS)

    def test_entrypoints_do_not_import_entrypoints(self):
        stems = {Path(name).stem for name in ENTRYPOINTS}
        for name in ENTRYPOINTS:
            text = (SCRIPTS / name).read_text()
            for stem in stems - {Path(name).stem}:
                self.assertIsNone(re.search(rf"(?m)^(?:from|import)\s+{stem}\b", text),
                                  f"{name} imports entrypoint {stem}")

    def test_helpers_are_a_package(self):
        self.assertTrue((SCRIPTS / "helper" / "__init__.py").is_file())
        self.assertFalse(any("__main__" in path.read_text()
                             for path in (SCRIPTS / "helper").glob("*.py")))


if __name__ == "__main__":
    unittest.main()
