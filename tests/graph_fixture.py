"""Disposable source repositories shared by graph and graph-feature tests."""
import tempfile
import unittest
from pathlib import Path

from support import invoke
from helper.graph_queries import Graph
from helper.graph_store import Store


class GraphCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.cache = self.home / "cache"
        self.store = Store(self.repo, self.cache)

    def write(self, path, text):
        dest = self.repo / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, newline="")
        return dest

    def graph(self, **kwargs):
        pin, body = self.store.snapshot(**kwargs)
        return Graph(pin, body)

    def cli(self, *args, success=True, **env):
        proc = invoke([*args, "--repo", str(self.repo), "--cache", str(self.cache)], env=env)
        self.assertEqual(proc.returncode, 0 if success else 2, proc.stderr + proc.stdout)
        self.assertEqual(proc.stderr, "")
        return proc.json()

    def fixture(self):
        self.write("core.py", "def save(value):\n    return value\n")
        self.write("service.py", "from core import save\ndef work(value):\n    return save(value)\n")
        self.write("api.py", "from service import work\ndef request(value):\n    return work(value)\n")
        self.write("unrelated.py", "def other():\n    return 42\n")
        self.write("tests/test_service.py", "import service\nimport unrelated\ndef test_work():\n    assert service.work(1) == 1\n")
