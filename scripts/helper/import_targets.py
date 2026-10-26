"""Import targets mapped to worktree files by stated per-language rules, beside ``module``.

``module`` stays the literal source text. Each import row gains ``target_path``
(a worktree-relative file, or a package directory for Go), ``target_state`` and
``target_rule``. Nothing is executed, installed packages and toolchains are not
consulted, and build configuration beyond ``Cargo.toml`` package names,
``go.mod`` module lines, ``package.json`` names/exports/main and tsconfig
``paths``/``baseUrl``/``outDir``/``rootDir`` is not read (no ``#[path]``,
``PYTHONPATH``, ``replace`` directives or bundler aliases). States:

- ``resolved``: exactly one worktree target under the rule.
- ``ambiguous``: several targets match and the rule cannot choose.
- ``standard_library``: not in the worktree and named by the language's standard
  library (Python ``sys.stdlib_module_names``; Rust std/core/alloc/proc_macro/test;
  Go paths whose first element has no dot).
- ``not_in_repo``: an absolute module, crate or package found nowhere in the
  worktree; whether it is installed is not checked.
- ``unresolved``: the target should be local (relative, ``crate::``, inside a
  ``go.mod`` module, a workspace package or tsconfig path) but no file matched,
  or the rule cannot apply (shell paths resolved against the runtime directory).
"""
import posixpath
import re
import sys

STATES = ("resolved", "ambiguous", "standard_library", "not_in_repo", "unresolved")
PY_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) | frozenset(sys.builtin_module_names)
RUST_STD = frozenset(("std", "core", "alloc", "proc_macro", "test"))
# TypeScript's extension order for relative specifiers; the first existing file wins.
ES_EXTENSIONS = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")
ES_SWAPS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
MAX_MANIFEST = 256 * 1024
# Built-in Node modules importable without the node: prefix.
NODE_BUILTINS = frozenset((
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants", "crypto", "dgram",
    "diagnostics_channel", "dns", "domain", "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring", "readline", "repl", "stream",
    "string_decoder", "sys", "timers", "tls", "trace_events", "tty", "url", "util", "v8", "vm", "wasi",
    "worker_threads", "zlib"))
# Export conditions tried in order; types first because it usually names the declaration beside source.
ES_CONDITIONS = ("types", "import", "module", "default", "require", "node")
JSONC_COMMENT = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.DOTALL)
JSONC_COMMA = re.compile(r'("(?:\\.|[^"\\])*")|,(?=\s*[}\]])')


def _dir(path):
    return posixpath.dirname(path)


def _join(*parts):
    joined = posixpath.normpath(posixpath.join(*[p for p in parts if p]))
    return "" if joined == "." else joined


def _jsonc(text):
    """Parse JSON with comments and trailing commas (tsconfig); None when unreadable."""
    import json
    try:
        keep = lambda m: m.group(1) or ""
        value = json.loads(JSONC_COMMA.sub(keep, JSONC_COMMENT.sub(keep, text or "")))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _choose(found, rule):
    found = list(dict.fromkeys(found))
    if len(found) == 1:
        return found[0], "resolved", rule
    return None, ("ambiguous" if found else None), rule


class Resolver:
    def __init__(self, paths, read):
        self.paths = set(paths)
        self.dirs = {}
        for path in self.paths:
            self.dirs.setdefault(_dir(path), []).append(path)
        self.read = read
        self._crates = self._go = self._workspace = None
        self._tsconfigs = {}
        self.packages = {_dir(p) for p in self.paths if posixpath.basename(p) in ("__init__.py", "__init__.pyi")}

    # --- Python ---------------------------------------------------------------

    def _py_module(self, base, dotted):
        stem = _join(base, *dotted.split(".")) if dotted else base
        names = ([stem + ".py", stem + ".pyi"] if dotted else []) + [_join(stem, "__init__.py"),
                                                                    _join(stem, "__init__.pyi")]
        hits = [n for n in names if n in self.paths]
        return [h for h in hits if not h.endswith(".pyi")][:1] or hits[:1]  # a stub stands in only alone

    def _py_roots(self, path):
        top = _dir(path)
        while top and top in self.packages:
            top = _dir(top)
        return list(dict.fromkeys(["", "src", top, _dir(path)]))

    def python(self, row):
        module, qualname = row.get("module") or "", row.get("qualname") or ""
        if module.startswith("."):
            level = len(module) - len(module.lstrip("."))
            base = _dir(row["path"])
            for _ in range(level - 1):
                if not base:
                    return None, "unresolved", "python_relative"
                base = _dir(base)
            for target in (qualname, module):  # `from . import x` may name submodule x
                found = self._py_module(base, target.lstrip("."))
                if found:
                    return found[0], "resolved", "python_relative"
            return None, "unresolved", "python_relative"
        targets = [qualname, module] if qualname != module else [module]
        for target in targets:
            found = [hit for root in self._py_roots(row["path"]) for hit in self._py_module(root, target)]
            path, state, rule = _choose(found, "python_module")
            if state:
                return path, state, rule
        top = module.split(".")[0]
        return None, ("standard_library" if top in PY_STDLIB else "not_in_repo"), "python_module"

    # --- TypeScript / JavaScript ----------------------------------------------

    def _es_file(self, base):
        """The file a TypeScript-resolved specifier base names, or None."""
        if base == ".." or base.startswith("../"):
            return None
        base = "" if base == "." else base
        stem, ext = posixpath.splitext(base)
        candidates = [base] if base else []
        candidates += [stem + swap for swap in ES_SWAPS.get(ext, ())]
        candidates += [base + e for e in ES_EXTENSIONS] + [_join(base, "index" + e) for e in ES_EXTENSIONS]
        return next((c for c in candidates if c in self.paths), None)

    def _tsconfig(self, path):
        """Merged compilerOptions of a tsconfig and its relative extends chain, with each option's directory."""
        if path in self._tsconfigs:
            return self._tsconfigs[path]
        self._tsconfigs[path] = {}  # an extends cycle ends here
        body = _jsonc(self.read(path)) if path in self.paths else None
        options = {}
        if body:
            parent = body.get("extends")
            if isinstance(parent, str) and parent.startswith("."):
                target = _join(_dir(path), parent)
                options.update(self._tsconfig(target if target.endswith(".json") else target + ".json"))
            own = body.get("compilerOptions")
            if isinstance(own, dict):
                options.update({k: (v, _dir(path)) for k, v in own.items()})
        self._tsconfigs[path] = options
        return options

    def _nearest_tsconfig(self, folder):
        while True:
            if _join(folder, "tsconfig.json") in self.paths:
                return self._tsconfig(_join(folder, "tsconfig.json"))
            if not folder:
                return {}
            folder = _dir(folder)

    def _ts_paths(self, row, spec):
        """tsconfig compilerOptions.paths from the nearest tsconfig.json: (path, state) or None."""
        options = self._nearest_tsconfig(_dir(row["path"]))
        mapping, where = options.get("paths", (None, ""))
        if not isinstance(mapping, dict):
            return None
        base_url, base_dir = options.get("baseUrl", (".", where))
        base = _join(base_dir, base_url) if isinstance(base_url, str) else where
        best = None
        for pattern, targets in mapping.items():
            prefix, star, suffix = pattern.partition("*")
            if star and spec.startswith(prefix) and spec.endswith(suffix) and len(spec) >= len(prefix) + len(suffix):
                rank, fill = len(prefix), spec[len(prefix):len(spec) - len(suffix)]
            elif not star and spec == pattern:
                rank, fill = len(pattern) + 1, ""
            else:
                continue
            if isinstance(targets, list) and (best is None or rank > best[0]):
                best = (rank, [str(t).replace("*", fill) for t in targets])
        if best is None:
            return None
        found = [hit for hit in (self._es_file(_join(base, t)) for t in best[1]) if hit]
        # Fallback targets are tried in order; the first that exists is the compiler's choice.
        return (found[0], "resolved") if found else (None, "unresolved")

    def workspace(self):
        """{package name: [package dir]}, from package.json files with a name."""
        if self._workspace is None:
            self._workspace = {}
            for manifest in sorted(p for p in self.paths if posixpath.basename(p) == "package.json"):
                body = _jsonc(self.read(manifest))
                if body and isinstance(body.get("name"), str):
                    self._workspace.setdefault(body["name"], []).append(_dir(manifest))
        return self._workspace

    def _exports(self, value, subpath):
        """Export targets for a subpath, in condition order."""
        if isinstance(value, str):
            return [value] if subpath == "." else []
        if isinstance(value, list):
            return [t for item in value for t in self._exports(item, subpath)]
        if not isinstance(value, dict):
            return []
        if not any(k.startswith(".") for k in value):  # a condition map applies to "."
            return [t for k in ES_CONDITIONS if k in value for t in self._conditions(value[k])] if subpath == "." else []
        if subpath in value:
            return self._conditions(value[subpath])
        for key in sorted((k for k in value if "*" in k), key=len, reverse=True):
            prefix, _, suffix = key.partition("*")
            if subpath.startswith(prefix) and subpath.endswith(suffix) and len(subpath) >= len(prefix) + len(suffix):
                fill = subpath[len(prefix):len(subpath) - len(suffix)]
                return [t.replace("*", fill) for t in self._conditions(value[key])]
        return []

    def _conditions(self, value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [t for item in value for t in self._conditions(item)]
        if isinstance(value, dict):
            return [t for k in ES_CONDITIONS if k in value for t in self._conditions(value[k])]
        return []

    def _package_source(self, folder, target):
        """A package-relative build target mapped back to its source file."""
        rel = posixpath.normpath(target)
        options = self._tsconfig(_join(folder, "tsconfig.json")) if _join(folder, "tsconfig.json") in self.paths else {}
        out_dir = posixpath.normpath(str(options.get("outDir", ("dist",))[0]))
        root_dir = posixpath.normpath(str(options.get("rootDir", ("src",))[0]))
        candidates = []
        for out in dict.fromkeys((out_dir, "dist", "lib", "build")):
            if rel == out or rel.startswith(out + "/"):
                inner = rel[len(out):].lstrip("/")
                for suffix in (".d.ts", ".d.mts", ".d.cts"):
                    if inner.endswith(suffix):
                        inner = inner[:-len(suffix)] + ".js"
                candidates.append(_join(folder, root_dir, inner))
        candidates.append(_join(folder, rel))
        return next((hit for hit in map(self._es_file, candidates) if hit), None)

    def _workspace_package(self, spec):
        parts = spec.split("/")
        width = 2 if spec.startswith("@") else 1
        name, subpath = "/".join(parts[:width]), "/".join(parts[width:])
        folders = self.workspace().get(name)
        if not folders:
            return None
        if len(folders) > 1:
            return None, "ambiguous"
        folder, subpath = folders[0], "./" + subpath if subpath else "."
        body = _jsonc(self.read(_join(folder, "package.json"))) or {}
        if "exports" in body:
            targets = self._exports(body["exports"], subpath)
        elif subpath == ".":
            targets = [body[k] for k in ("types", "module", "main") if isinstance(body.get(k), str)] + ["index"]
        else:
            targets = [subpath]
        found = next((hit for hit in (self._package_source(folder, t) for t in targets) if hit), None)
        return (found, "resolved") if found else (None, "unresolved")

    def es(self, row):
        spec = row.get("module") or ""
        if spec.startswith("/"):
            return None, "unresolved", "es_absolute"
        if spec in (".", "..") or spec.startswith(("./", "../")):
            hit = self._es_file(posixpath.normpath(posixpath.join(_dir(row["path"]), spec)))
            return (hit, "resolved", "es_relative") if hit else (None, "unresolved", "es_relative")
        if spec.startswith("node:") or spec.split("/")[0] in NODE_BUILTINS:
            return None, "standard_library", "es_bare"
        found = self._ts_paths(row, spec)
        if found:
            return (*found, "es_tsconfig_paths")
        found = self._workspace_package(spec)
        if found:
            return (*found, "es_workspace")
        if spec.startswith(("@/", "~/", "#")):
            return None, "unresolved", "es_alias"  # a bundler alias or package import map, not a package
        return None, "not_in_repo", "es_bare"

    # --- Rust -----------------------------------------------------------------

    def crates(self):
        """{crate dir: (package name with '_', root file or None)}, from Cargo.toml files."""
        if self._crates is None:
            self._crates = {}
            for manifest in sorted(p for p in self.paths if posixpath.basename(p) == "Cargo.toml"):
                text = self.read(manifest) or ""
                section = re.search(r"(?ms)^\[package\]\s*$(.*?)(?=^\[|\Z)", text)
                name = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"', section.group(1)) if section else None
                folder = _dir(manifest)
                root = next((r for r in (_join(folder, "src/lib.rs"), _join(folder, "src/main.rs"))
                             if r in self.paths), None)
                self._crates[folder] = (name.group(1).replace("-", "_") if name else None, root)
        return self._crates

    def _crate_of(self, path):
        folder = _dir(path)
        while True:
            if folder in self.crates():
                return folder
            if not folder:
                return None
            folder = _dir(folder)

    def _rust_root(self, crate, path):
        """The crate root file whose module tree holds this file, or None when unknown.

        Binaries, integration tests, examples and benches are separate crates: a file
        directly in their directory is its own root; a subdirectory's root is its
        main.rs, or the sibling ``<dir>.rs`` that declares it as a module.
        """
        rel = posixpath.relpath(path, crate) if crate else path
        parts = rel.split("/")
        for target in (("src", "bin"), ("tests",), ("examples",), ("benches",)):
            if tuple(parts[:len(target)]) == target and len(parts) > len(target):
                inner = parts[len(target):]
                if len(inner) == 1:
                    return path
                # tests/x/main.rs is its own crate; tests/x/y.rs is a module of crate tests/x.rs.
                found = [r for r in (_join(crate, *target, inner[0], "main.rs"), _join(crate, *target, inner[0] + ".rs"))
                         if r in self.paths]
                return found[0] if len(found) == 1 else None
        if path == _join(crate, "src/main.rs"):
            return path
        return self.crates()[crate][1]

    def _module_dir(self, file, roots):
        """Directory holding a module's child module files."""
        if file in roots or posixpath.basename(file) == "mod.rs":
            return _dir(file)
        return file[:-3]

    def _child(self, folder, name):
        found = [c for c in (_join(folder, name + ".rs"), _join(folder, name, "mod.rs")) if c in self.paths]
        return found

    def _parent_module(self, file, roots):
        folder = self._module_dir(file, roots)
        if file in roots:
            return None
        up = _dir(folder)
        root = next((r for r in roots if _dir(r) == up), None)
        if root:
            return root
        found = self._child(_dir(up), posixpath.basename(up))
        return found[0] if len(found) == 1 else None

    def _walk(self, file, segments, roots):
        """(file of the longest module prefix, ambiguous?) walking child modules."""
        for segment in segments:
            found = self._child(self._module_dir(file, roots), segment)
            if len(found) > 1:
                return None, True
            if not found:
                break
            file = found[0]
        return file, False

    def rust(self, row):
        path = row["path"]
        if row.get("node_kind") == "extern_crate_declaration":
            segments, rule = [row.get("module") or ""], "rust_extern_crate"
        else:
            segments, rule = (row.get("qualname") or "").split("::"), "rust_use"
        segments = [s for s in segments if s and s != "*"]
        absolute = (row.get("qualname") or "").startswith("::")
        crate = self._crate_of(path)
        root = self._rust_root(crate, path) if crate is not None else None
        roots = {r for _, r in self.crates().values() if r} | ({root} if root else set())
        if not segments:
            return None, "unresolved", rule
        head, rest = segments[0], segments[1:]
        if head in ("crate", "self", "super") and rule == "rust_use":
            if root is None:
                return None, "unresolved", rule
            current = root if head == "crate" else path
            ups = 1 if head == "super" else 0
            while rest and rest[0] == "super":
                ups, rest = ups + 1, rest[1:]
            if head != "crate":
                inline = row.get("inline_depth", 0)  # `mod x { ... }` blocks enclosing the use
                if ups < inline:
                    return path, "resolved", rule  # the path stays inside this file's inline modules
                ups -= inline
            for _ in range(ups):
                current = self._parent_module(current, roots)
                if current is None:
                    return None, "unresolved", rule
            found, ambiguous = self._walk(current, rest, roots)
            return (None, "ambiguous", rule) if ambiguous else (found, "resolved", rule)
        if not absolute and rule == "rust_use" and root is not None and self._child(self._module_dir(path, roots), head):
            found, ambiguous = self._walk(path, segments, roots)  # 2018 paths may start at a child module
            return (None, "ambiguous", rule) if ambiguous else (found, "resolved", rule)
        named = [(folder, r) for folder, (name, r) in self.crates().items() if name == head and r]
        if len(named) > 1:
            return None, "ambiguous", rule
        if named:
            found, ambiguous = self._walk(named[0][1], rest, roots)
            return (None, "ambiguous", rule) if ambiguous else (found, "resolved", rule)
        return None, ("standard_library" if head in RUST_STD else "not_in_repo"), rule

    # --- Go -------------------------------------------------------------------

    def go_modules(self):
        if self._go is None:
            self._go = []
            for manifest in sorted(p for p in self.paths if posixpath.basename(p) == "go.mod"):
                found = re.search(r'(?m)^\s*module\s+"?([^\s"]+)"?', self.read(manifest) or "")
                if found:
                    self._go.append((found.group(1), _dir(manifest)))
        return self._go

    def go(self, row):
        target = row.get("module") or ""
        matches = [(m, d) for m, d in self.go_modules() if target == m or target.startswith(m + "/")]
        if matches:
            longest = max(len(m) for m, _ in matches)
            dirs = {_join(d, target[len(m) + 1:]) for m, d in matches if len(m) == longest}
            hits = [d for d in dirs if any(p.endswith(".go") for p in self.dirs.get(d, ()))]
            path, state, _ = _choose(hits, "go_module")
            return path, state or "unresolved", "go_module"
        first = target.split("/")[0]
        return None, ("not_in_repo" if "." in first else "standard_library"), "go_module"

    # --- Shell ----------------------------------------------------------------

    def shell(self, row, worktree):
        target = row.get("module") or ""
        if worktree and target.startswith(worktree.rstrip("/") + "/"):
            rel = target[len(worktree.rstrip("/")) + 1:]
            return (rel, "resolved", "shell_absolute") if rel in self.paths else (None, "unresolved", "shell_absolute")
        if target.startswith("/"):
            return None, "not_in_repo", "shell_absolute"
        return None, "unresolved", "shell_runtime_directory"  # bash resolves against $PWD, then $PATH

    def resolve(self, row, worktree=None):
        language = row.get("language")
        if language == "python":
            return self.python(row)
        if language in ("typescript", "javascript"):
            return self.es(row)
        if language == "rust":
            return self.rust(row)
        if language == "go":
            return self.go(row)
        if language == "shell":
            return self.shell(row, worktree)
        return None, "unresolved", None


def annotate(items, paths, read, worktree=None):
    """Add target columns to import rows in place; {state: count} for coverage."""
    resolver, counts = Resolver(paths, read), {}
    modules = {(r["path"], r["qualname"]) for r in items if r.get("language") == "rust" and r.get("kind") == "module"}
    for row in items:
        if row.get("kind") != "import":
            row.update(target_path=None, target_state=None, target_rule=None)
            continue
        view = row
        if row.get("language") == "rust" and row.get("parent"):
            # A use can only sit inside an inline module, never a `mod x;` declaration.
            parts = row["parent"].split("::")
            depth = sum(1 for i in range(1, len(parts) + 1) if (row["path"], "::".join(parts[:i])) in modules)
            view = {**row, "inline_depth": depth}
        path, state, rule = resolver.resolve(view, worktree)
        row.update(target_path=path, target_state=state, target_rule=rule)
        counts[state] = counts.get(state, 0) + 1
    return counts
