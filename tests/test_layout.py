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


class FlagParityTests(unittest.TestCase):
    """Both CLIs take the same paging, projection, format and repository flags."""

    def options(self, parser):
        import argparse
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        return {name: {o for a in q._actions for o in a.option_strings} for name, q in sub.choices.items()}

    def test_sessions_row_commands_match_remember(self):
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import remember
        import sessions
        graph = self.options(remember.parser())
        shared = set(sessions.SHARED)
        for name, flags in graph.items():
            self.assertLessEqual(shared, flags, name)
        for name, flags in self.options(sessions.parser()).items():
            expected = shared if name in sessions.ROWS else {"--format", "--fields"}
            self.assertLessEqual(expected, flags, name)
