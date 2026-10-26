"""Evidence graph producers: syntax, import resolution and conservative Python calls."""
import ast
import collections
import re
from pathlib import PurePosixPath

from . import import_targets, manifest_refs, repo_files, syntax_calls
from .graph_store import digest

PREDICATES = {
    "CONTAINS": "Directory/file containment; never an impact propagation edge.",
    "DECLARES": "Source file declares a symbol.",
    "IMPORTS": "Literal import mapped by the documented resolver rule; not a call.",
    "CALLS_STATIC": "Python, TS/JS or Rust call (or construction) with one supported binding; not observed execution.",
    "TESTS": "Test file imports an implementation file; association, not assertion coverage.",
    "REFERENCES": "Manifest or CI file names this path (package.json, tsconfig, Cargo.toml, workflow); not an import.",
}
DEPENDENCIES = ("IMPORTS", "CALLS_STATIC")


def is_test(path):
    p = PurePosixPath(path)
    return bool(set(p.parts) & {"test", "tests", "__tests__"} or p.name.startswith("test_")
                or p.name.endswith(("_test.py", "_test.go", ".test.ts", ".test.js", ".spec.ts", ".spec.js")))


# Folders and names that hold built, bundled or third-party code, not project source.
GENERATED_DIRS = {"dist", "vendor", "vendored", "third_party", "third-party", "node_modules", "generated",
                  "__generated__", ".next", ".nuxt", "bower_components"}
GENERATED_NAMES = re.compile(r"(?:\.min\.[cm]?js|\.min\.css|\.bundle\.[cm]?js|_pb2(?:_grpc)?\.pyi?|\.pb\.go"
                             r"|\.generated\.\w+|\.g\.dart)$")
GENERATED_HEADER = re.compile(r"@generated|do not edit|code generated|auto-?generated|regenerate (?:it )?(?:with|by|using)",
                              re.I)
# Declaration files this long are host or library typings, not hand-written project types.
LARGE_DECLARATION = 1000
RUST_TEST_ATTR = re.compile(r"^\s*#\[(?:cfg\(test\)|(?:[\w:]+::)?test)\]")


def generated(path, text):
    """Why a file is generated or vendored, or None; such files are left out of rankings."""
    p = PurePosixPath(path)
    folder = next((part for part in p.parts[:-1] if part in GENERATED_DIRS), None)
    if folder:
        return f"folder:{folder}"
    if GENERATED_NAMES.search(p.name):
        return "name"
    head = "\n".join((text or "").splitlines()[:10])
    if GENERATED_HEADER.search(head):
        return "header"
    if p.name.endswith((".d.ts", ".d.mts", ".d.cts")) and (text or "").count("\n") >= LARGE_DECLARATION:
        return "large_declaration_file"
    return None


def rust_test_ranges(text, rows):
    """(line, end_line) of items under `#[cfg(test)]` or `#[test]`; attributes sit just above the item."""
    lines = text.splitlines()
    ranges = []
    for item in rows:
        if item["kind"] == "import":
            continue
        number = item["line"] - 2
        while number >= 0 and lines[number].lstrip().startswith(("#[", "//", "///")):
            if RUST_TEST_ATTR.match(lines[number]):
                ranges.append((item["line"], item["end_line"]))
                break
            number -= 1
    return ranges


class Builder:
    def __init__(self, source):
        self.source = source
        self.nodes, self.edges, self.gaps, self.items = {}, {}, [], []
        self.files = {r["path"]: r for r in source["files"]}
        self.symbols = collections.defaultdict(list)
        self.unparsed = collections.Counter()  # non-code or unsupported languages: coverage, not gaps
        self.inline = {}  # path -> [(line, end_line)] of inline Rust test items

    def node(self, kind, key, **fields):
        ident = "node:" + digest([kind, key])[:24]
        self.nodes[ident] = {"id": ident, "kind": kind, "key": key, **fields}
        return ident

    def file_id(self, path):
        return "node:" + digest(["file", path])[:24]

    def evidence(self, path, line=1, end_line=None):
        return {"path": path, "line": line, "end_line": end_line or line,
                "digest": self.files.get(path, {}).get("digest")}

    def edge(self, src, predicate, dst, evidence, rule, resolution="resolved", **extra):
        body = {"source": src, "predicate": predicate, "target": dst, "evidence": evidence,
                "rule": rule, "resolution": resolution, "basis": "derived", **extra}
        key = "edge:" + digest(body)[:24]
        self.edges[key] = {"id": key, **body}

    def gap(self, area, reason, path=None, line=None, **extra):
        self.gaps.append({"area": area, "reason": reason, "path": path, "line": line, **extra})

    def structures(self):
        root = self.node("directory", ".", path=".", name=".")
        dirs = {".": root}
        for path, row in self.files.items():
            parent = root
            parts = PurePosixPath(path).parts[:-1]
            for i in range(len(parts)):
                folder = "/".join(parts[:i+1])
                if folder not in dirs:
                    dirs[folder] = self.node("directory", folder, path=folder, name=parts[i])
                    self.edge(parent, "CONTAINS", dirs[folder], None, "inventory")
                parent = dirs[folder]
            node = self.node("file", path, path=path, name=PurePosixPath(path).name,
                             is_test=is_test(path), state=row["state"], digest=row["digest"])
            reason = generated(path, row["text"])
            if reason:
                self.nodes[node]["generated"] = reason
            self.edge(parent, "CONTAINS", node, None, "inventory")
            if row["state"] != "read":
                self.gap("source", row["state"], path)
                continue
            data = row["text"].encode()
            language, ext = repo_files.language_of(path, data)
            self.nodes[node]["language"] = language
            rows, state, error = repo_files.parse_symbols(data, row["text"], self.source["root"], path, language, ext)
            self.nodes[node]["parse_state"] = state
            if state == "unsupported":
                self.unparsed[language or "unknown"] += 1
            elif state != "parsed":
                self.gap("parser", state, path, error=error, language=language)
            self.items.extend(rows)
            tests = rust_test_ranges(row["text"], rows) if language == "rust" else []
            if tests:
                self.nodes[node]["inline_tests"] = len(tests)
                self.inline[path] = tests
            for item in rows:
                if item["kind"] == "import":
                    continue
                key = f"{path}::{item['qualname']}:{item['kind']}:{item['line']}"
                inline = any(a <= item["line"] <= b for a, b in tests)
                sid = self.node("symbol", key, path=path, name=item["name"], qualname=item["qualname"],
                                symbol_kind=item["kind"], line=item["line"], end_line=item["end_line"],
                                is_test=is_test(path) or inline, language=language,
                                public_candidate=not item["parent"] and not item["name"].startswith("_"))
                self.symbols[(path, item["qualname"])].append(sid)
                self.edge(node, "DECLARES", sid, self.evidence(path, item["line"], item["end_line"]), "syntax")
        return dirs

    def imports(self, dirs):
        readable = {p: r["text"] for p, r in self.files.items() if r["state"] == "read"}
        bindings = import_targets.annotate(self.items, list(self.files), readable.get, self.source["root"])
        # One import statement can bind many names; gaps and counts are per statement.
        statements = collections.defaultdict(list)
        for row in self.items:
            if row["kind"] != "import":
                continue
            src, path = self.file_id(row["path"]), row.get("target_path")
            state, rule = row["target_state"], row["target_rule"]
            evidence = self.evidence(row["path"], row["line"], row["end_line"])
            if state == "resolved":
                # Go targets a package directory; retain it and individual member witnesses.
                if path in self.files:
                    targets = [self.file_id(path)]
                elif path in dirs or path == "":
                    folder = dirs.get(path or ".")
                    targets = [folder] + [self.file_id(p) for p in self.files
                                         if str(PurePosixPath(p).parent) == (path or ".") and p.endswith(".go")]
                else:
                    targets = []
                # Type-only imports are erased at runtime: no load order, no runtime cycle.
                erased = {"type_only": True} if row.get("type_only") else {}
                for target in targets:
                    self.edge(src, "IMPORTS", target, evidence, rule, **erased)
                    target_path = self.nodes[target].get("path")
                    inline = any(a <= row["line"] <= b for a, b in self.inline.get(row["path"], []))
                    if (is_test(row["path"]) or inline) and target_path and not is_test(target_path) and self.nodes[target]["kind"] == "file":
                        self.edge(src, "TESTS", target, evidence, "test_file_import")
            else:
                if state in ("standard_library", "not_in_repo"):
                    target = self.node("external", f"{row['language']}:{row['module']}", name=row["module"],
                                       path=None, resolution=state)
                    self.edge(src, "IMPORTS", target, evidence, rule, state)
            statements[(row["path"], row["line"], row["module"], state, rule)].append(row)
        for (path, line, module, state, rule), rows in statements.items():
            # External packages are edges to external nodes, not gaps in the repository graph.
            if state not in ("resolved", "standard_library", "not_in_repo"):
                self.gap("imports", state, path, line, predicate="IMPORTS", text=module, rule=rule,
                         bindings=len(rows), candidates=[])
        return {"statements": dict(collections.Counter(key[3] for key in statements)), "bindings": bindings}

    def references(self, dirs):
        """REFERENCES edges from manifests and CI files; certain paths that match nothing become gaps."""
        counts = collections.Counter()
        for path, row in self.files.items():
            if row["state"] != "read":
                continue
            for ref in manifest_refs.references(path, row["text"]):
                target = ref["target"]
                node = self.file_id(target) if target in self.files else dirs.get(target)
                evidence = self.evidence(path, ref["line"])
                if node:
                    self.edge(self.file_id(path), "REFERENCES", node, evidence, "manifest_path")
                    counts["resolved"] += 1
                elif ref["strong"]:
                    self.gap("references", "not_captured", path, ref["line"], predicate="REFERENCES",
                             text=ref["text"], target=target, field=ref["field"])
                    counts["not_captured"] += 1
        return dict(counts)

    def calls(self):
        for path, row in self.files.items():
            if row["state"] != "read" or self.nodes[self.file_id(path)].get("language") != "python":
                continue
            try:
                tree = ast.parse(row["text"], filename=path)
            except (SyntaxError, ValueError, RecursionError):
                continue
            try:
                PythonCalls(self, path, tree).run()
            except RecursionError:
                self.gap("calls", "ast_recursion_limit", path)
        syntax_calls.produce(self, import_targets.Resolver(self.files, lambda p: self.files.get(p, {}).get("text")))


class Scope:
    def __init__(self, parent, owner, kind):
        self.parent, self.owner, self.kind = parent, owner, kind
        self.bindings = collections.defaultdict(list)

    def resolve(self, name):
        if name in self.bindings:
            values = self.bindings[name]
            return values[0] if len(values) == 1 else ("unknown", "multiple_bindings")
        parent = self.parent
        while parent and parent.kind == "class":
            parent = parent.parent
        return parent.resolve(name) if parent else ("unknown", "external_or_unbound")


class PythonCalls:
    """No receiver inference, re-export guessing, alias propagation or runtime execution."""
    def __init__(self, builder, path, tree):
        self.b, self.path, self.tree = builder, path, tree
        self.scopes = {}
        self.lines = builder.files[path]["text"].splitlines()
        self.imports = [r for r in builder.items if r["path"] == path and r["kind"] == "import"]
        self.resolver = import_targets.Resolver(builder.files,
                                                lambda p: builder.files.get(p, {}).get("text"))

    def symbol(self, qualname, line):
        return next((sid for sid in self.b.symbols.get((self.path, qualname), [])
                     if self.b.nodes[sid]["line"] == line), self.b.file_id(self.path))

    def bind_import(self, node, scope):
        for alias in node.names:
            name = alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
            rows = [r for r in self.imports if r["line"] == node.lineno
                    and r["name"] == (alias.asname or alias.name)]
            binding = ("unknown", "unresolved_import")
            if len(rows) == 1 and rows[0].get("target_state") == "resolved":
                target = rows[0]["target_path"]
                if isinstance(node, ast.Import):
                    # `import a.b` binds a; avoid pretending it binds b.
                    binding = ("module", target) if alias.asname or "." not in alias.name else ("unknown", "dotted_import_binding")
                else:
                    base, _, _ = self.resolver.python({**rows[0], "qualname": rows[0]["module"]})
                    hits = self.b.symbols.get((target, alias.name), [])
                    if base != target:
                        binding = ("module", target)
                    elif len(hits) == 1:
                        binding = ("symbol", hits[0])
            if alias.name == "*":
                scope.bindings["*"].append(("unknown", "star_import"))
            else:
                scope.bindings[name].append(binding)

    def collect(self, node, scope, qual=""):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = f"{qual}.{node.name}" if qual else node.name
            sid = self.symbol(name, node.lineno)
            scope.bindings[node.name].append(("symbol", sid))
            child = Scope(scope, sid, "class" if isinstance(node, ast.ClassDef) else "function")
            self.scopes[id(node)] = child
            if child.kind == "function":
                args = node.args
                for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                    if arg:
                        child.bindings[arg.arg].append(("unknown", "parameter"))
            for statement in node.body:
                self.collect(statement, child, name)
            # Definitions/defaults may themselves rebind outer names via walrus.
            outer = [*node.decorator_list]
            if child.kind == "function":
                outer += [*node.args.defaults, *[v for v in node.args.kw_defaults if v]]
            else:
                outer += list(node.bases) + [k.value for k in node.keywords]
            for item in outer:
                self.collect(item, scope, qual)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            self.bind_import(node, scope)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            scope.bindings[node.id].append(("unknown", "assignment"))
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                scope.bindings[name].append(("unknown", "global_or_nonlocal"))
        if isinstance(node, ast.ExceptHandler) and node.name:
            scope.bindings[node.name].append(("unknown", "exception_binding"))
        # Comprehensions and lambdas have separate binding rules; handled as unknown at calls.
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            return
        if isinstance(node, ast.MatchAs) and node.name:
            scope.bindings[node.name].append(("unknown", "pattern_binding"))
        if isinstance(node, ast.MatchStar) and node.name:
            scope.bindings[node.name].append(("unknown", "pattern_binding"))
        for child in ast.iter_child_nodes(node):
            self.collect(child, scope, qual)

    def target(self, call, scope):
        chain = scope
        while chain:
            if "*" in chain.bindings:
                return None, "star_import"
            chain = chain.parent
        if isinstance(call, ast.Name):
            kind, value = scope.resolve(call.id)
        elif isinstance(call, ast.Attribute) and isinstance(call.value, ast.Name):
            kind, value = scope.resolve(call.value.id)
            if kind != "module":
                return None, "dynamic_receiver"
            found = self.b.symbols.get((value, call.attr), [])
            kind, value = ("symbol", found[0]) if len(found) == 1 else ("unknown", "unresolved_attribute")
        else:
            return None, "dynamic_call"
        if kind == "symbol":
            node = self.b.nodes[value]
            if node.get("symbol_kind") == "class":
                return value, None  # Calling a class constructs it.
            if node.get("symbol_kind") not in ("function", "async_function"):
                return None, "class_or_method_dispatch"
            return value, None
        return None, value if kind == "unknown" else "non_function_binding"

    def walk(self, node, scope, opaque=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            outer = list(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                outer += list(node.bases) + [k.value for k in node.keywords]
            else:
                outer += list(node.args.defaults) + [v for v in node.args.kw_defaults if v]
            for value in outer:
                self.walk(value, scope, opaque)
            for statement in node.body:
                self.walk(statement, self.scopes[id(node)], opaque)
            return
        opaque = opaque or isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
        if isinstance(node, ast.Call):
            target, reason = (None, "comprehension_or_lambda_scope") if opaque else self.target(node.func, scope)
            if target:
                self.b.edge(scope.owner, "CALLS_STATIC", target,
                            self.b.evidence(self.path, node.lineno, node.end_lineno), "python_lexical_binding")
            else:
                self.b.gap("calls", reason, self.path, node.lineno, predicate="CALLS_STATIC",
                           text=self.lines[node.lineno-1].strip()[:240], candidates=[])
        for child in ast.iter_child_nodes(node):
            self.walk(child, scope, opaque)

    def run(self):
        root = Scope(None, self.b.file_id(self.path), "module")
        self.collect(self.tree, root)
        self.walk(self.tree, root)


def build(source):
    builder = Builder(source)
    dirs = builder.structures()
    counts = builder.imports(dirs)
    references = builder.references(dirs)
    builder.calls()
    if not source["inventory"]["complete"]:
        builder.gap("inventory", "incomplete_listing")
    if not source["inventory"]["stable"]:
        builder.gap("capture", "changed_during_capture")
    nodes = sorted(builder.nodes.values(), key=lambda n: n["id"])
    gaps = sorted(builder.gaps, key=lambda g: (g.get("path") or "", g.get("line") or 0, g["area"], g["reason"]))
    return {"nodes": nodes, "edges": sorted(builder.edges.values(), key=lambda e: e["id"]), "gaps": gaps,
            "coverage": {"inventory_complete": source["inventory"]["complete"], "capture_stable": source["inventory"]["stable"],
                         "files": len(source["files"]), "nodes": len(nodes), "edges": len(builder.edges),
                         "imports": counts, "references": references, "gaps": dict(collections.Counter(g["area"] for g in gaps)),
                         "unparsed_files": dict(sorted(builder.unparsed.items())),
                         "call_producers": ["python_lexical_binding", "es_lexical_binding", "rust_lexical_binding"],
                         "limitations": ["Static candidates, not runtime behavior or a complete call graph.",
                                         "Import resolution follows references/graph.md; unsupported configuration may change targets.",
                                         "Dynamic dispatch, reflection, framework wiring and external consumers are not enumerated.",
                                         "Tests are import associations; no tests were executed.",
                                         "Ownership, lifecycle and responsibility require source/requirements interpretation."]}}
