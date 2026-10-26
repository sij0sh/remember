import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from helper import import_targets  # noqa: E402


def imp(path, language, module, qualname=None, parent=None, node_kind=None, name=None):
    return {"path": path, "language": language, "kind": "import", "module": module,
            "qualname": qualname or module, "parent": parent, "node_kind": node_kind, "name": name}


def resolve(paths, rows, files=None):
    files = files or {}
    counts = import_targets.annotate(rows, paths, files.get, "/work")
    return [(r["target_path"], r["target_state"], r["target_rule"]) for r in rows], counts


class ImportTargetTests(unittest.TestCase):
    def test_python_relative_absolute_and_stdlib(self):
        paths = ["pkg/__init__.py", "pkg/a.py", "pkg/sub/__init__.py", "pkg/sub/b.py", "scripts/run.py",
                 "scripts/helper/__init__.py", "scripts/helper/x.py", "src/lib/__init__.py",
                 "lib/__init__.py"]
        got, counts = resolve(paths, [
            imp("pkg/sub/b.py", "python", "..", "..a"),               # from .. import a: submodule a
            imp("pkg/sub/b.py", "python", "..a", "..a.f"),            # from ..a import f: module a
            imp("pkg/a.py", "python", ".missing", ".missing.x"),
            imp("pkg/a.py", "python", "...", "...x"),                 # above the repository root
            imp("scripts/run.py", "python", "helper.x", "helper.x.y"),  # the script's own directory
            imp("pkg/a.py", "python", "lib"),                         # repo root and src/ both hold lib
            imp("pkg/a.py", "python", "json"),
            imp("pkg/a.py", "python", "requests"),
        ])
        self.assertEqual(got, [
            ("pkg/a.py", "resolved", "python_relative"),
            ("pkg/a.py", "resolved", "python_relative"),
            (None, "unresolved", "python_relative"),
            (None, "unresolved", "python_relative"),
            ("scripts/helper/x.py", "resolved", "python_module"),
            (None, "ambiguous", "python_module"),
            (None, "standard_library", "python_module"),
            (None, "not_in_repo", "python_module")])
        self.assertEqual(counts, {"resolved": 3, "unresolved": 2, "ambiguous": 1, "standard_library": 1,
                                  "not_in_repo": 1})

    def test_es_relative_extension_order_and_bare(self):
        paths = ["web/a.ts", "web/b.ts", "web/b.js", "web/c/index.tsx", "web/d.ts", "web/e.mjs"]
        got, _ = resolve(paths, [
            imp("web/a.ts", "typescript", "./b"),        # .ts precedes .js
            imp("web/a.ts", "typescript", "./c"),
            imp("web/a.ts", "typescript", "./d.js"),     # ESM .js specifier naming a .ts file
            imp("web/a.ts", "javascript", "./e.mjs"),
            imp("web/a.ts", "typescript", "../../out"),
            imp("web/a.ts", "typescript", "./none"),
            imp("web/a.ts", "typescript", "react"),
            imp("web/a.ts", "typescript", "@/alias"),
            imp("web/a.ts", "typescript", "node:fs"),
        ])
        self.assertEqual(got, [
            ("web/b.ts", "resolved", "es_relative"), ("web/c/index.tsx", "resolved", "es_relative"),
            ("web/d.ts", "resolved", "es_relative"), ("web/e.mjs", "resolved", "es_relative"),
            (None, "unresolved", "es_relative"), (None, "unresolved", "es_relative"),
            (None, "not_in_repo", "es_bare"), (None, "unresolved", "es_alias"),
            (None, "standard_library", "es_bare")])

    def test_es_workspace_packages_and_tsconfig_paths(self):
        paths = ["tsconfig.json", "tsconfig.base.json", "apps/server/package.json", "apps/server/tsconfig.json",
                 "apps/server/src/index.ts", "apps/server/src/adapter-sdk.ts", "packages/contracts/package.json",
                 "packages/contracts/src/index.ts", "packages/plain/package.json", "packages/plain/lib/util.js",
                 "packages/plain/index.js", "web/tsconfig.json", "web/src/app.ts", "web/src/lib/x.ts",
                 "apps/server/src/main.ts"]
        files = {
            "apps/server/package.json": '{"name": "@acme/server", "exports": {".": "./dist/index.js", '
                                        '"./adapter-sdk": {"types": "./dist/adapter-sdk.d.ts", "default": "./dist/adapter-sdk.js"}}}',
            "apps/server/tsconfig.json": '{"extends": "../../tsconfig.base.json", // shared\n'
                                         ' "compilerOptions": {"rootDir": "src", "outDir": "dist",},}',
            "packages/contracts/package.json": '{"name": "@acme/contracts", "exports": {".": {"types": "./dist/index.d.ts"}}}',
            "packages/plain/package.json": '{"name": "plain", "main": "index.js"}',
            "web/tsconfig.json": '{"compilerOptions": {"baseUrl": "src", /* aliases */ "paths": {"@/*": ["./*"]}}}',
        }
        got, _ = resolve(paths, [
            imp("web/src/app.ts", "typescript", "@acme/server"),
            imp("web/src/app.ts", "typescript", "@acme/server/adapter-sdk"),
            imp("web/src/app.ts", "typescript", "@acme/contracts"),
            imp("web/src/app.ts", "typescript", "plain"),
            imp("web/src/app.ts", "typescript", "plain/lib/util"),
            imp("web/src/app.ts", "typescript", "@acme/server/missing"),
            imp("web/src/app.ts", "typescript", "@/lib/x"),
            imp("web/src/app.ts", "typescript", "@/lib/none"),
            imp("apps/server/src/main.ts", "typescript", "@/lib/x"),   # outside web/: no paths apply
            imp("web/src/app.ts", "typescript", "fs"),
            imp("web/src/app.ts", "typescript", "react"),
        ], files)
        self.assertEqual(got, [
            ("apps/server/src/index.ts", "resolved", "es_workspace"),
            ("apps/server/src/adapter-sdk.ts", "resolved", "es_workspace"),
            ("packages/contracts/src/index.ts", "resolved", "es_workspace"),
            ("packages/plain/index.js", "resolved", "es_workspace"),
            ("packages/plain/lib/util.js", "resolved", "es_workspace"),
            (None, "unresolved", "es_workspace"),
            ("web/src/lib/x.ts", "resolved", "es_tsconfig_paths"),
            (None, "unresolved", "es_tsconfig_paths"),
            (None, "unresolved", "es_alias"),
            (None, "standard_library", "es_bare"),
            (None, "not_in_repo", "es_bare")])

    def test_rust_paths_crates_and_targets(self):
        paths = ["Cargo.toml", "src/lib.rs", "src/main.rs", "src/store.rs", "src/ingest/mod.rs",
                 "src/ingest/backfill.rs", "src/both.rs", "src/both/mod.rs", "tests/it.rs",
                 "tests/common/mod.rs", "tests/cli.rs", "tests/cli/ensure.rs",
                 "util/Cargo.toml", "util/src/lib.rs", "util/src/fmt.rs"]
        files = {"Cargo.toml": '[package]\nname = "app"\n[dependencies]\nname = "decoy"\n',
                 "util/Cargo.toml": '[package]\nname = "my-util"\n'}
        modules = [{"path": "src/ingest/backfill.rs", "language": "rust", "kind": "module", "qualname": "tests"}]
        rows = [
            imp("src/ingest/backfill.rs", "rust", "crate::store", "crate::store::Store"),
            imp("src/ingest/backfill.rs", "rust", "super", "super::Embedder"),
            imp("src/ingest/backfill.rs", "rust", "super", "super::*", parent="tests"),  # inline mod tests
            imp("src/ingest/backfill.rs", "rust", "super::super::super", "super::super::super::store::S", parent="tests"),
            imp("src/ingest/mod.rs", "rust", "self::backfill", "self::backfill::run"),
            imp("src/ingest/mod.rs", "rust", "backfill", "backfill::run"),             # 2018 child path
            imp("src/lib.rs", "rust", "crate::both", "crate::both::X"),
            imp("src/store.rs", "rust", "my_util::fmt", "my_util::fmt::pad"),
            imp("tests/it.rs", "rust", "common", "common::fixture"),
            imp("tests/cli/ensure.rs", "rust", "super", "super::*"),
            imp("src/lib.rs", "rust", "std::io", "std::io::Read"),
            imp("src/lib.rs", "rust", "serde", "serde::Serialize"),
            imp("src/lib.rs", "rust", "decoy", "decoy::X"),
            imp("src/lib.rs", "rust", "super", "super::x"),                            # above the crate root
        ]
        got, _ = resolve(paths, modules + rows, files)
        self.assertEqual(got[1:], [
            ("src/store.rs", "resolved", "rust_use"),
            ("src/ingest/mod.rs", "resolved", "rust_use"),
            ("src/ingest/backfill.rs", "resolved", "rust_use"),
            ("src/store.rs", "resolved", "rust_use"),
            ("src/ingest/backfill.rs", "resolved", "rust_use"),
            ("src/ingest/backfill.rs", "resolved", "rust_use"),
            (None, "ambiguous", "rust_use"),
            ("util/src/fmt.rs", "resolved", "rust_use"),
            ("tests/common/mod.rs", "resolved", "rust_use"),
            ("tests/cli.rs", "resolved", "rust_use"),
            (None, "standard_library", "rust_use"),
            (None, "not_in_repo", "rust_use"),
            (None, "not_in_repo", "rust_use"),
            (None, "unresolved", "rust_use")])
        self.assertEqual(got[0], (None, None, None))  # definitions carry no target

    def test_go_module_paths(self):
        paths = ["go.mod", "cmd/main.go", "internal/store/db.go", "tools/go.mod", "tools/gen/gen.go"]
        files = {"go.mod": "module example.com/app\n\ngo 1.22\n", "tools/go.mod": "module example.com/app/tools\n"}
        got, _ = resolve(paths, [
            imp("cmd/main.go", "go", "example.com/app/internal/store"),
            imp("cmd/main.go", "go", "example.com/app/tools/gen"),   # the longest module prefix wins
            imp("cmd/main.go", "go", "example.com/app/missing"),
            imp("cmd/main.go", "go", "net/http"),
            imp("cmd/main.go", "go", "github.com/x/y"),
        ], files)
        self.assertEqual(got, [("internal/store", "resolved", "go_module"), ("tools/gen", "resolved", "go_module"),
                               (None, "unresolved", "go_module"), (None, "standard_library", "go_module"),
                               (None, "not_in_repo", "go_module")])

    def test_shell_sources_are_runtime_dependent(self):
        got, _ = resolve(["lib.sh", "run.sh"], [
            imp("run.sh", "shell", "./lib.sh"), imp("run.sh", "shell", "/work/lib.sh"),
            imp("run.sh", "shell", "/etc/profile")])
        self.assertEqual(got, [(None, "unresolved", "shell_runtime_directory"),
                               ("lib.sh", "resolved", "shell_absolute"), (None, "not_in_repo", "shell_absolute")])


if __name__ == "__main__":
    unittest.main()
