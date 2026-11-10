"""Behavioral graph fixtures; all repositories and caches are disposable."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from graph_fixture import GraphCase
from helper.graph_store import Store, capture, read_file
from helper.engine import SQLiteQuery
from helper.graph_queries import TABLES, impact, reference


class GraphTests(GraphCase):
    def test_directory_without_git_or_recordings(self):
        self.fixture()
        with patch("helper.graph_store.git", return_value=None):
            graph = self.graph()
        self.assertEqual(graph.body["source"]["inventory"]["mode"], "directory")
        consumers, limits = graph.walk(graph.seeds("core.py", "file"), depth=3, impact=True)
        self.assertEqual({r["path"] for r in consumers}, {"api.py", "service.py"})
        self.assertTrue(limits["complete_within_predicates"])

    def test_unknown_fields_fail_instead_of_blank_columns(self):
        self.fixture()
        error = self.cli("query", "SELECT path FROM files", "--fields", "path,bogus", success=False)
        self.assertIn("no column bogus", error["error"])
        self.assertTrue(all(set(r) <= {"path"} for r in self.cli("query", "SELECT path, 1 AS n FROM files", "--fields", "path")["results"]))

    def test_witnesses_expand_to_retained_source(self):
        self.fixture()
        graph = self.graph()
        rows, _ = graph.walk(graph.seeds("core.py", "file"), depth=3, impact=True)
        api = next(r for r in rows if r["path"] == "api.py")
        self.assertEqual(api["distance"], 2)
        self.assertEqual(len(api["witness"]), 2)
        for step in api["witness"]:
            expanded = self.cli("show", step["edge"], "--max-bytes", "20000")
            self.assertIn("source", expanded["results"][0])
            self.assertTrue(expanded["results"][0]["source"]["text"])

    def test_history_never_scanned_by_graph(self):
        self.fixture()
        self.write("sessions/session.jsonl", '{"message":"do not trust this"}')
        with patch("helper.discovery.candidate_files", side_effect=AssertionError("history scan")):
            graph = self.graph()
            self.assertTrue(graph.nodes)

    def test_unambiguous_imported_python_calls(self):
        self.fixture()
        graph = self.graph()
        rows, _ = graph.walk(graph.seeds("core.py::save", "symbol"), grain="symbol", predicates=["CALLS_STATIC"], depth=3)
        self.assertEqual({r["qualname"] for r in rows}, {"work", "request", "test_work"})

    def test_dynamic_and_shadowed_calls_do_not_get_false_edges(self):
        self.write("a.py", "def save(x):\n    return x\ndef caller(save):\n    return save(1)\ndef dynamic(obj):\n    return obj.save(1)\n")
        graph = self.graph()
        self.assertFalse([e for e in graph.body["edges"] if e["predicate"] == "CALLS_STATIC"])
        reasons = {g["reason"] for g in graph.body["gaps"]}
        self.assertIn("parameter", reasons)
        self.assertIn("dynamic_receiver", reasons)

    def test_assignment_and_duplicate_definitions_stay_unknown(self):
        self.write("a.py", "def save():\n    pass\ndef save():\n    pass\ndef f():\n    save()\n")
        graph = self.graph()
        self.assertFalse([e for e in graph.body["edges"] if e["predicate"] == "CALLS_STATIC"])
        self.assertIn("multiple_bindings", {g["reason"] for g in graph.body["gaps"]})

    def test_same_named_module_and_function_are_not_confused(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/save.py", "def save():\n    return 1\n")
        self.write("app.py", "from pkg import save\nsave()\nsave.save()\n")
        self.write("direct.py", "from pkg.save import save\nsave()\n")
        graph = self.graph()
        calls = [e for e in graph.body["edges"] if e["predicate"] == "CALLS_STATIC"]
        self.assertEqual({(e["evidence"]["path"], e["evidence"]["line"]) for e in calls}, {("app.py", 3), ("direct.py", 2)})

    def test_results_record_their_query_and_write_no_report(self):
        self.fixture()
        a = self.cli("find", "no-such-text-a")
        self.assertEqual((a["schema_version"], a["scope"]["text"], a["scope"]["retention"]), (2, "no-such-text-a", "transient"))
        self.assertFalse([p.name for p in self.cache.rglob("*.json") if p.name.startswith(("report-", "pending-"))])
        self.assertIn("Invalid snapshot reference", self.cli("show", "report:" + "a"*64, success=False)["error"])

    def test_targets_accept_returned_refs_and_path_tails(self):
        self.fixture()
        graph = self.graph()
        node = graph.public(graph.nodes[graph.unique("core.py::save")])
        self.assertEqual(graph.resolve(node["ref"]), [node["id"]])
        self.write("pkg/deep/core.py", "def keep():\n    return 1\n")
        graph = self.graph()
        self.assertEqual(graph.nodes[graph.unique("deep/core.py::keep")]["qualname"], "keep")

    def test_symbol_impact_reports_widening_to_file(self):
        self.fixture()
        value = impact(self.graph(), ["core.py::save"])
        self.assertEqual(value["scope"]["widened_targets"][0]["projected_to"], "core.py")
        self.assertIn(("target", "widened_to_file"), {(g["area"], g["reason"]) for g in value["gaps"]})

    def test_import_gaps_are_per_statement(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/a.py", "from .missing import one, two, three\nfrom requests import get, post\n")
        graph = self.graph()
        gaps = [g for g in graph.body["gaps"] if g["area"] == "imports"]
        self.assertEqual([(g["text"], g["bindings"]) for g in gaps], [(".missing", 3)])
        self.assertEqual(graph.body["coverage"]["imports"]["statements"], {"unresolved": 1, "not_in_repo": 1})
        self.assertEqual(graph.body["coverage"]["imports"]["bindings"], {"unresolved": 3, "not_in_repo": 2})

    def test_status_ok_despite_docs_externals_and_dynamic_calls(self):
        self.write("README.md", "# demo\n")
        self.write("config.json", "{}\n")
        self.write("a.py", "import requests\ndef f(obj):\n    return obj.run()\n")
        value = self.cli("map", "--kind", "file")
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["coverage"]["unparsed_files"], {"json": 1, "markdown": 1})
        self.assertNotIn("imports", value["coverage"]["gaps"])
        self.assertEqual(value["coverage"]["gaps"], {"calls": 1})

    def calls(self, graph):
        name = lambda ident: (graph.nodes[ident].get("path"), graph.nodes[ident].get("qualname"))
        return {(name(e["source"]), name(e["target"])) for e in graph.body["edges"] if e["predicate"] == "CALLS_STATIC"}

    def test_typescript_calls_follow_imports_reexports_and_this(self):
        self.write("src/store.ts", "export function save(x: number) { return x; }\nexport const load = () => 1;\n")
        self.write("src/index.ts", "export { save } from './store';\n")
        self.write("src/api.ts", "import { save } from './index';\nimport * as store from './store';\n"
                                 "import React from 'react';\n"
                                 "class Api {\n  run() { return this.helper(); }\n  helper() { return save(1) + store.load(); }\n}\n"
                                 "function shadow(save: any) { return save(1); }\n"
                                 "export function main(obj: any) { obj.save(1); React.render(); return new Api().run(); }\n")
        graph = self.graph()
        self.assertEqual(self.calls(graph), {
            (("src/api.ts", "Api.run"), ("src/api.ts", "Api.helper")),
            (("src/api.ts", "Api.helper"), ("src/store.ts", "load")),
            (("src/api.ts", "Api.helper"), ("src/store.ts", "save")),
            (("src/api.ts", "main"), ("src/api.ts", "Api"))})
        # Inside shadow(), save is both the import and a parameter, so that call is not resolved.
        reasons = {g["reason"]: g for g in graph.body["gaps"] if g["area"] == "calls" and g["path"] == "src/api.ts"}
        self.assertIn("multiple_bindings", reasons)
        self.assertIn("dynamic_receiver", reasons)
        self.assertEqual(reasons["dynamic_receiver"]["calls"], 3)  # obj.save, React.render, new Api().run

    def test_typescript_unique_import_binding_resolves_through_barrel(self):
        self.write("store.ts", "export function save(x: number) { return x; }\n")
        self.write("index.ts", "export { save } from './store';\n")
        self.write("app.ts", "import { save } from './index';\nexport function main() { return save(1); }\n")
        graph = self.graph()
        self.assertEqual(self.calls(graph), {(("app.ts", "main"), ("store.ts", "save"))})

    def test_rust_calls_through_use_module_paths_and_self(self):
        self.write("Cargo.toml", '[package]\nname = "demo"\n')
        self.write("src/lib.rs", "pub mod store;\nuse crate::store::save;\npub struct World;\n"
                                 "impl World {\n    pub fn new() -> Self { Self::make() }\n    fn make() -> Self { World }\n"
                                 "    pub fn tick(&self) { self.step(); save(); store::load(); crate::store::load(); }\n"
                                 "    fn step(&self) {}\n}\n"
                                 "pub fn run() { let w = World::new(); w.tick(); helper(); String::from(\"x\"); }\n"
                                 "fn helper() {}\n")
        self.write("src/store.rs", "pub fn save() {}\npub fn load() {}\n")
        graph = self.graph()
        self.assertEqual(self.calls(graph), {
            (("src/lib.rs", "World::new"), ("src/lib.rs", "World::make")),
            (("src/lib.rs", "World::tick"), ("src/lib.rs", "World::step")),
            (("src/lib.rs", "World::tick"), ("src/store.rs", "save")),
            (("src/lib.rs", "World::tick"), ("src/store.rs", "load")),
            (("src/lib.rs", "run"), ("src/lib.rs", "World::new")),
            (("src/lib.rs", "run"), ("src/lib.rs", "World::tick")),
            (("src/lib.rs", "run"), ("src/lib.rs", "helper"))})
        reasons = {g["reason"] for g in graph.body["gaps"] if g["area"] == "calls"}
        self.assertEqual(reasons, {"external_or_unbound"})

    def test_rust_calls_follow_pub_use_and_typed_receivers(self):
        self.write("Cargo.toml", '[package]\nname = "demo"\n')
        self.write("src/lib.rs", "mod numeric;\npub use numeric::parse;\npub struct World;\n"
                                 "impl World {\n    pub fn create() -> Result<Self, ()> { Ok(World) }\n"
                                 "    pub fn label() -> String { String::new() }\n    pub fn pour(&mut self) {}\n}\n")
        self.write("src/numeric.rs", "pub fn parse() {}\n")
        self.write("src/main.rs", "use demo::{parse, World};\n"
                                  "fn main() {\n    let mut world = World::create().map_err(|e| e).unwrap();\n"
                                  "    parse();\n    command(&mut world);\n    let name = World::label();\n    name.pour();\n}\n"
                                  "fn command(world: &mut World) { world.pour(); }\n")
        graph = self.graph()
        calls = self.calls(graph)
        self.assertLessEqual({(("src/main.rs", "main"), ("src/numeric.rs", "parse")),
                              (("src/main.rs", "main"), ("src/lib.rs", "World::create")),
                              (("src/main.rs", "command"), ("src/lib.rs", "World::pour"))}, calls)
        # label() returns String, not World, so `name.pour()` stays unresolved.
        self.assertNotIn((("src/main.rs", "main"), ("src/lib.rs", "World::pour")), calls)
        rules = {e["rule"] for e in graph.body["edges"] if e["predicate"] == "CALLS_STATIC"}
        self.assertIn("rust_receiver_type", rules)

    def test_typescript_typed_receivers_and_constructors(self):
        self.write("src/store.ts", "export class Store {\n  save() { return 1; }\n}\n")
        self.write("src/index.ts", "export { Store } from './store';\n")
        self.write("src/app.ts", "import { Store } from './index';\n"
                                 "export function make() { const s = new Store(); return s.save(); }\n"
                                 "export function use(store: Store) { return store.save(); }\n"
                                 "export function other(s: any) { return s.save(); }\n")
        graph = self.graph()
        self.assertEqual(self.calls(graph), {
            (("src/app.ts", "make"), ("src/store.ts", "Store")),
            (("src/app.ts", "make"), ("src/store.ts", "Store.save")),
            (("src/app.ts", "use"), ("src/store.ts", "Store.save"))})

    def test_python_class_call_is_construction(self):
        self.write("model.py", "class Model:\n    pass\n")
        self.write("app.py", "from model import Model\ndef build():\n    return Model()\n")
        self.assertEqual(self.calls(self.graph()), {(("app.py", "build"), ("model.py", "Model"))})

    def test_missing_optional_parser_stays_visible(self):
        from helper import code_symbols
        self.write("app.ts", "import { save } from './store';\n")
        with patch.object(code_symbols, "symbols", side_effect=code_symbols.ParserUnavailable("test")):
            graph = self.graph()
        self.assertIn("parser_unavailable", {g["reason"] for g in graph.body["gaps"]})

    def test_capture_detects_file_replaced_after_read(self):
        self.write("a.py", "before = 1\n")
        real_read = read_file
        def changing(*args):
            result = real_read(*args)
            self.write("a.py", "after = 2\n")
            return result
        with patch("helper.graph_store.read_file", side_effect=changing):
            result = capture(self.repo)
        self.assertFalse(result["inventory"]["stable"])
        self.assertEqual(result["files"][0]["state"], "changed_during_capture")
        self.assertIsNone(result["files"][0]["text"])

    def test_workspace_caches_are_isolated(self):
        self.write("a.py", "x=1\n")
        graph = self.graph()
        other = self.home/"other"
        other.mkdir()
        (other/"a.py").write_text("x=1\n")
        store = Store(other, self.cache)
        self.assertNotEqual(store.path, self.store.path)
        with self.assertRaises(ValueError):
            store.load(graph.snapshot)

    def test_python_package_root_does_not_loop(self):
        self.write("__init__.py", "")
        self.write("a.py", "from b import run\nrun()\n")
        self.write("b.py", "def run():\n    return 1\n")
        out = impact(self.graph(), ["b.py"])
        self.assertIn("a.py", {r.get("path") for r in out["results"] if r.get("role") == "possible_consumer"})

    def test_ambiguous_names_not_silently_selected(self):
        self.write("a.py", "def save():\n    pass\n")
        self.write("b.py", "def save():\n    pass\n")
        out = self.cli("inspect", "save", "--no-sessions")
        self.assertEqual((out["status"], out["coverage"]["target"]["reason"]), ("partial", "ambiguous"))
        self.assertEqual(self.cli("inspect", "a.py::save", "--no-sessions")["target"]["path"], "a.py")

    def test_ambiguous_python_roots_are_visible(self):
        self.write("mod.py", "")
        self.write("src/mod.py", "")
        self.write("app.py", "import mod\n")
        graph = self.graph()
        self.assertIn("ambiguous", {g["reason"] for g in graph.body["gaps"]})

    def test_tests_do_not_bridge_implementations(self):
        self.fixture()
        graph = self.graph()
        doc = impact(graph, ["core.py"], depth=4)
        consumers = {r.get("path") for r in doc["results"] if r.get("role") == "possible_consumer"}
        self.assertNotIn("unrelated.py", consumers)
        self.assertNotIn("tests/test_service.py", consumers)
        tests = [r for r in doc["results"] if r["kind"] == "verification"]
        self.assertEqual([r["test"]["path"] for r in tests], ["tests/test_service.py"])
        self.assertTrue(all(r["execution"] == "not_run" for r in tests))

    def test_consumers_and_dependencies_are_separate(self):
        self.fixture()
        rows = impact(self.graph(), ["core.py"], depth=3)["results"]
        self.assertIn("api.py", {r["path"] for r in rows if r.get("role") == "possible_consumer"})
        self.assertFalse([r for r in rows if r.get("role") == "dependency_context"])

    def test_depth_and_node_limits_report_frontier(self):
        self.fixture()
        graph = self.graph()
        rows, limits = graph.walk(graph.seeds("core.py", "file"), depth=1, impact=True)
        self.assertEqual({r["path"] for r in rows}, {"service.py"})
        self.assertIn("depth", limits["bounds"])
        self.assertIn("api.py", {r["path"] for r in limits["frontier"]})
        _, capped = graph.walk(graph.seeds("core.py", "file"), max_nodes=1)
        self.assertIn("nodes", capped["bounds"])

    def test_leaf_at_bound_is_complete(self):
        self.write("a.py", "import b\n")
        self.write("b.py", "")
        graph = self.graph()
        _, limits = graph.walk(graph.seeds("b.py", "file"), depth=1)
        self.assertTrue(limits["complete_within_predicates"])

    def test_cycle_terminates_and_reaches_consumer(self):
        self.write("a.py", "import b\n")
        self.write("b.py", "import a\n")
        self.write("c.py", "import b\n")
        graph = self.graph()
        rows, limits = graph.walk(graph.seeds("a.py", "file"), depth=5)
        self.assertEqual({r["path"] for r in rows}, {"b.py", "c.py"})
        self.assertTrue(limits["complete_within_predicates"])

    def test_snapshot_is_repeatable_and_superseded(self):
        self.fixture()
        before = self.graph()
        self.assertEqual(before.snapshot, self.graph().snapshot)
        original = (self.repo/"core.py").read_text()
        shown = self.cli("show", reference(before.snapshot, "source", "core.py"))
        self.assertEqual("\n".join(r["text"] for r in shown["results"]) + "\n", original)
        self.write("core.py", "# replaced\n")
        after = self.graph()
        self.assertNotEqual(before.snapshot, after.snapshot)
        # Only the newest capture is kept; an older ref fails instead of serving old code.
        self.assertEqual(sorted(p.name.split("-")[0] for p in self.store.path.glob("*.json")), ["snapshot", "source"])
        error = self.cli("show", reference(before.snapshot, "source", "core.py"), success=False)["error"]
        self.assertIn("superseded", error)
        status = self.store.freshness(before.body)
        self.assertEqual(status["state"], "changed")
        self.assertIn("core.py", status["changed_paths"])

    def test_dirty_untracked_and_missing_git_files(self):
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.write("tracked.py", "x=1\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        self.write("untracked.py", "x=2\n")
        (self.repo/"tracked.py").unlink()
        graph = self.graph()
        states = {r["path"]: r["state"] for r in graph.body["source"]["files"]}
        self.assertEqual(states, {"tracked.py": "missing", "untracked.py": "read"})
        self.assertTrue(graph.body["source"]["git"]["available"])
        self.assertIsNone(graph.body["source"]["git"]["head"])

    def test_exclusions_and_symlinks(self):
        self.write("node_modules/a.py", "raise RuntimeError()\n")
        self.write("hidden/a.py", "x=1\n")
        self.write(".rememberignore", "hidden/\n")
        try:
            (self.repo/"link.py").symlink_to(self.home/"outside.py")
        except OSError:
            self.skipTest("symlinks need Windows Developer Mode or admin")
        (self.home/"outside.py").write_text("secret = 1\n")
        graph = self.graph()
        self.assertNotIn("node_modules/a.py", graph.files)
        self.assertNotIn("hidden/a.py", graph.files)
        self.assertEqual(graph.files["link.py"]["state"], "symlink")
        self.assertNotIn("secret", json.dumps(graph.body))

    def test_file_and_total_byte_limits_remain_visible(self):
        self.write("a.py", "x=12345\n")
        self.write("b.py", "y=12345\n")
        graph = self.graph(max_file_bytes=4)
        self.assertEqual({r["state"] for r in graph.body["source"]["files"]}, {"file_limit"})
        graph = self.graph(max_total_bytes=10)
        self.assertIn("byte_limit", {r["state"] for r in graph.body["source"]["files"]})
        self.assertEqual(self.store.freshness(graph.body)["state"], "unknown")

    def test_configuration_changes_invalidate_snapshot(self):
        self.write("a.py", "x=1\n")
        self.write("pyproject.toml", '[project]\nname="before"\n')
        before = self.graph()
        self.write("pyproject.toml", '[project]\nname="after"\n')
        self.assertNotEqual(before.snapshot, self.graph().snapshot)

    def test_no_project_code_execution(self):
        self.write("danger.py", "from pathlib import Path\nPath('executed').touch()\nraise RuntimeError()\n")
        self.graph()
        self.assertFalse((self.repo/"executed").exists())

    def test_sql_works_without_optional_dependencies_and_counts_before_paging(self):
        self.fixture()
        self.assertEqual(self.cli("query", "SELECT count(*) AS n FROM files")["results"], [{"n": 5}])
        self.assertEqual(self.cli("query", "SELECT path FROM files WHERE path = :p", "--param", "p=core.py")["results"],
                         [{"path": "core.py"}])

    def test_sql_rejects_mutations_external_reads_and_multiple_statements(self):
        for sql in ("DELETE FROM nodes", "DROP TABLE edges", "ATTACH DATABASE '/tmp/remember-test.db' AS other",
                    "SELECT load_extension('x')", "PRAGMA database_list", "SELECT 1; SELECT 2", "CREATE TABLE x(a)"):
            with self.subTest(sql=sql), self.assertRaises(ValueError), SQLiteQuery(TABLES, 5) as engine:
                engine.plan(sql, {})
                engine.evaluate(10)

    def test_sql_timeout(self):
        with self.assertRaises(ValueError), SQLiteQuery(TABLES, 0.01) as engine:
            engine.plan("WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT sum(n) FROM x", {})
            engine.evaluate(10)

    def test_sql_blob_and_nonfinite_values_are_json_safe(self):
        out = self.cli("query", "SELECT x'00ff' AS blob, 1e999 AS value")
        self.assertEqual(out["results"], [{"blob": {"encoding": "hex", "value": "00ff"}, "value": "inf"}])

    def test_envelope_coverage_reports_omitted_rows(self):
        from helper.envelope import load_rows
        self.write("a.py", "")
        out = self.cli("map", "--whole-repo-lines", "0", "--limit", "1", "--no-primer")
        _, coverage = load_rows(json.dumps(out))
        self.assertFalse(coverage["complete"])
        self.assertEqual(coverage["rows_omitted"], 1)

    def test_last_page_is_complete(self):
        self.fixture()
        first = self.cli("map", "--kind", "file", "--limit", "3")
        last = self.cli("show", first["next"][0]["ref"], "--limit", "100")
        self.assertFalse(first["delivery"]["complete"])
        self.assertTrue(last["delivery"]["complete"])
        self.assertEqual((last["status"], last["delivery"]["offset"]), ("ok", 3))

    def test_map_path_limits_rows(self):
        self.fixture()
        symbols = self.cli("map", "service.py", "--kind", "symbol")
        self.assertEqual({(r["path"], r["name"]) for r in symbols["results"]}, {("service.py", "work")})
        files = self.cli("map", "tests", "--kind", "file")
        self.assertEqual([r["path"] for r in files["results"]], ["tests/test_service.py"])

    def test_find_word_case_and_regex(self):
        self.write("a.rs", "fn main() {\n    x.unwrap();\n    y.unwrap_or(0);\n    Unwrap();\n}\n")
        lines = lambda *a: [r["line"] for r in self.cli("find", *a, "--kind", "occurrence")["results"]]
        self.assertEqual(lines("unwrap"), [2, 3, 4])
        self.assertEqual(lines("unwrap", "--word"), [2, 4])
        self.assertEqual(lines("unwrap", "--word", "--case-sensitive"), [2])
        self.assertEqual(lines("--regex", r"unwrap_\w+"), [3])
        self.assertIn("Invalid --regex", self.cli("find", "--regex", "(", success=False)["error"])

    def test_gap_calls_column(self):
        self.write("a.py", "def f(obj):\n    return obj.run()\n")
        out = self.cli("query", "SELECT sum(calls) AS calls FROM gaps WHERE area = 'calls'")
        self.assertEqual(out["results"], [{"calls": 1}])

    def test_bare_map_prints_small_repository_whole(self):
        self.fixture()
        self.write("package-lock.json", "{}\n")
        (self.repo / ".tools").mkdir()
        self.write("run.sh", "python3 .tools/check.py\n")
        empty = self.home / "empty-recordings"
        empty.mkdir()
        settings = self.home / "settings.json"
        settings.write_text(json.dumps({"recordings": {name: [str(empty)]
                                                       for name in ("pi", "codex", "claude-code")}}))
        proc = subprocess.run([sys.executable, str(ROOT/"scripts/remember.py"),
                               "--repo", str(self.repo), "--cache", str(self.cache)],
                              capture_output=True, text=True, timeout=20,
                              env={**os.environ, "TRACE_SESSIONS_SETTINGS": str(settings)})
        text = proc.stdout
        self.assertTrue(text.startswith("WHOLE REPOSITORY: all 6 readable captured files"), text[:200])
        self.assertIn("==> core.py <==\ndef save(value):\n    return value\n", text)
        self.assertIn("not shown: package-lock.json (lockfile)", text)
        self.assertIn("not captured but used: .tools/ (referenced at run.sh:1)", text)
        self.assertEqual(sorted(self.cache.rglob("*.md")), [])
        listing = self.cli("map", "--whole-repo-lines", "0", "--no-primer")
        self.assertIn("core.py", {r["path"] for r in listing["results"]})

    def test_paging_replays_pinned_capture_and_fails_once_superseded(self):
        self.fixture()
        page = self.cli("map", "--whole-repo-lines", "0", "--limit", "1", "--no-primer")
        following = self.cli("show", page["next"][0]["ref"], "--limit", "100")
        self.assertEqual(following["delivery"]["total"], page["delivery"]["total"])
        self.write("later.py", "")
        self.cli("map", "--whole-repo-lines", "0", "--limit", "1", "--no-primer")
        self.assertIn("superseded", self.cli("show", page["next"][0]["ref"], success=False)["error"])

    def test_invalid_expired_and_corrupt_references(self):
        self.write("a.py", "x=1\n")
        graph = self.graph()
        for ref in ("../anything", "snapshot:" + "f"*64):
            with self.assertRaises(ValueError):
                self.store.load(ref)
        path = self.store.path/(graph.snapshot.replace(":", "-") + ".json")
        path.write_text('{"tampered": true}')
        with self.assertRaises(ValueError):
            self.store.load(graph.snapshot)

    def test_cache_inside_repo_rejected(self):
        with self.assertRaises(ValueError):
            Store(self.repo, self.repo/"cache")

    def test_common_flags_before_action(self):
        self.fixture()
        proc = subprocess.run([sys.executable, str(ROOT/"scripts/remember.py"), "--repo", str(self.repo),
                               "--cache", str(self.cache), "--limit", "1", "map", "--whole-repo-lines", "0",
                               "--no-primer"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(json.loads(proc.stdout)["results"]), 1)

    def test_empty_directory_and_empty_query_tables(self):
        self.assertEqual(self.cli("query", "SELECT count(*) AS n FROM edges")["results"], [{"n": 0}])
        self.cli("map", "--format", "json")

    def test_describe_is_bounded_without_source_capture(self):
        out = self.cli("describe", "--max-bytes", "2048")
        self.assertEqual(out["snapshots"], [])
        self.assertLessEqual(len(json.dumps(out, sort_keys=True, separators=(",", ":")).encode()) + 1, 2048)
        self.assertFalse(list(self.store.path.glob("snapshot-*")))


if __name__ == "__main__":
    unittest.main()
