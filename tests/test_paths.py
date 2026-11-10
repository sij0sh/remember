"""Recorded paths resolve against Windows, UNC and POSIX bases."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from helper.profiles import resolve_path  # noqa: E402


class ResolvePathTests(unittest.TestCase):
    def test_windows_base_makes_relative_paths_windows(self):
        # A drive-letter cwd decides the flavour even when the tool recorded a relative path;
        # the posixpath fallback would leave every Windows edit unresolved.
        base = r"C:\Users\runneradmin\repo"
        self.assertEqual(resolve_path("a.py", base, base), ("a.py", "repository"))
        self.assertEqual(resolve_path("a.py", base, None), ("C:/Users/runneradmin/repo/a.py", "external"))

    def test_unc_and_posix_bases(self):
        self.assertEqual(resolve_path("a.py", r"\\server\share\repo", None),
                         ("//server/share/repo/a.py", "external"))
        self.assertEqual(resolve_path("/home/dev/repo/a.py", "/home/dev/repo", "/home/dev/repo"),
                         ("a.py", "repository"))


if __name__ == "__main__":
    unittest.main()
