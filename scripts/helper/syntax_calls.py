"""CALLS_STATIC candidates for TypeScript/JavaScript and Rust from tree-sitter call sites.

A callee resolves only when its name has exactly one binding visible at the call
(declarations, parameters, patterns and imports all count; parameters and locals are
visible only inside their function), and that binding is a function declared in the
file or imported from a resolved worktree file, following re-exports (`export { f }
from`, Rust `pub use`). Receivers resolve as `this.m()` / `self.m()` inside a class or
inherent impl, a namespace import (`ns.f()`), a Rust module path (`a::f()`,
`crate::a::f()`), a type path (`Type::f()`, `Self::f()`), or a local whose type is
written at its binding: `x: Type`, TS `new Type()`, Rust `Type::f(..)` returning
`Self`/`Type` (through `?`, `unwrap`, `expect`, `map_err`). Such receiver edges carry
their own rule. TS/JS `new Type()` is an edge to the class; unresolved `new` sites are
not counted. No other type inference, trait dispatch, closures or macros.
Unresolved call sites are counted per file and reason, not listed one by one.
"""
import collections
import posixpath
import re

from . import code_symbols

FUNCTIONS = ("function", "async_function")
CALLABLES = FUNCTIONS + ("method",)
RULES = {"typescript": "es_lexical_binding", "javascript": "es_lexical_binding", "rust": "rust_lexical_binding"}
ES_PATTERNS = ("formal_parameters", "catch_clause", "for_in_statement")
RUST_PATTERNS = {"let_declaration": "pattern", "parameter": "pattern", "closure_parameters": None,
                 "for_expression": "pattern", "match_arm": "pattern", "let_condition": "pattern"}
MAX_REEXPORT_HOPS = 3
# Nodes whose parameters and locals are invisible outside them.
RUST_SCOPES = ("function_item", "closure_expression")
ES_SCOPES = ("function_declaration", "generator_function_declaration", "function_expression", "function",
             "generator_function", "arrow_function", "method_definition")
# Rust calls that keep the receiver's type for `let x = Type::f(..)?.unwrap()`.
RUST_PASS_THROUGH = {"unwrap", "expect", "map_err", "unwrap_or_else", "context", "with_context"}


def _text(node):
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _identifiers(node, kinds=("identifier", "shorthand_property_identifier_pattern")):
    """Every identifier in a subtree: over-collecting only makes a name ambiguous."""
    stack, found = [node], []
    while stack:
        current = stack.pop()
        if current.type in kinds:
            found.append(_text(current))
        stack.extend(current.named_children)
    return found


def _scoped(binding, node, kinds, own=False):
    """A binding visible only inside the innermost enclosing function, if any."""
    current = node if own else node.parent
    while current is not None and current.type not in kinds:
        current = current.parent
    if current is None:
        return binding
    return ("scoped", (current.start_point[0] + 1, current.end_point[0] + 1), binding)


class SyntaxCalls:
    def __init__(self, builder, resolver):
        self.b, self.resolver = builder, resolver
        self.by_path = collections.defaultdict(list)
        for (path, _), sids in builder.symbols.items():
            self.by_path[path].extend(sids)
        self.imports = collections.defaultdict(list)
        for row in builder.items:
            if row["kind"] == "import":
                self.imports[row["path"]].append(row)

    # --- symbol lookup --------------------------------------------------------

    def declared(self, path, qualname, kinds=CALLABLES):
        found = [s for s in self.b.symbols.get((path, qualname), []) if self.b.nodes[s].get("symbol_kind") in kinds]
        return found[0] if len(found) == 1 else None

    def at_line(self, path, name, line):
        return next((s for s in self.by_path[path] if self.b.nodes[s]["name"] == name
                     and self.b.nodes[s]["line"] == line), None)

    def owner(self, path, line):
        """The innermost declared function or method whose lines hold the call, else the file."""
        best = None
        for sid in self.by_path[path]:
            n = self.b.nodes[sid]
            if n.get("symbol_kind") in CALLABLES and n["line"] <= line <= n["end_line"]:
                if best is None or n["line"] >= self.b.nodes[best]["line"]:
                    best = sid
        return best or self.b.file_id(path)

    def es_export(self, path, name, kinds=FUNCTIONS, hops=0):
        """A function (or class) a module exports under `name`, following `export { x } from` re-exports."""
        found = self.declared(path, name, kinds)  # qualname == name: top level only
        if found:
            return found
        if hops >= MAX_REEXPORT_HOPS:
            return None
        for row in self.imports[path]:
            if row["node_kind"] == "export_statement" and row["name"] == name and row["target_state"] == "resolved":
                return self.es_export(row["target_path"], row["qualname"].rpartition("#")[2], kinds, hops + 1)
        return None

    def rust_export(self, path, name, hops=0):
        """The binding a Rust module offers as `name`: a function, type or module, following `pub use`."""
        found = self.declared(path, name, FUNCTIONS)
        if found:
            return ("symbol", found)
        if self.b.symbols.get((path, name)):
            return ("type", path, name)
        if hops >= MAX_REEXPORT_HOPS:
            return None
        for row in self.imports[path]:
            if (row["node_kind"] == "use_declaration" and row["name"] == name
                    and row["target_state"] == "resolved" and row["target_path"] in self.b.files):
                last = (row["qualname"] or "").rsplit("::", 1)[-1]
                return self.rust_export(row["target_path"], last, hops + 1)
        return None

    def returns_self(self, sid, type_name):
        """Whether a declared function's signature returns `Self`/`type_name`, possibly wrapped."""
        node = self.b.nodes[sid]
        lines = self.b.files[node["path"]]["text"].splitlines()[node["line"]-1:node["end_line"]]
        result = "\n".join(lines).split("{", 1)[0].partition("->")[2]
        wrapped = r"(?:(?:\w+::)*(?:Result|Option|Box|Rc|Arc)\s*<\s*)?"
        return re.match(rf"\s*{wrapped}(?:Self|{re.escape(type_name)})\b", result) is not None

    # --- bindings -------------------------------------------------------------

    def es_bindings(self, path, tree):
        bindings = collections.defaultdict(list)
        for row in self.imports[path]:
            if row["node_kind"] != "import_statement" or not row["name"]:
                continue
            imported = row["qualname"].rpartition("#")[2] if "#" in row["qualname"] else None
            if row["target_state"] in ("standard_library", "not_in_repo"):
                binding = ("unknown", "external_import")
            elif row["target_state"] != "resolved" or row["target_path"] not in self.b.files:
                binding = ("unknown", "unresolved_import")
            elif imported == "*":
                binding = ("module", row["target_path"])
            elif imported in (None, "default"):
                binding = ("unknown", "default_or_require_import")
            else:
                found = self.es_export(row["target_path"], imported)
                cls = None if found else self.es_export(row["target_path"], imported, ("class",))
                binding = ("symbol", found) if found else ("class", cls) if cls else ("unknown", "import_not_function")
            bindings[row["name"]].append(binding)
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            kind = node.type
            if kind in ("function_declaration", "generator_function_declaration", "class_declaration",
                        "abstract_class_declaration"):
                name = node.child_by_field_name("name")
                sid = self.at_line(path, _text(name), node.start_point[0] + 1)
                bindings[_text(name)].append(("unknown", "class_or_declaration") if not sid
                                             else ("symbol", sid) if kind.startswith(("function", "generator"))
                                             else ("class", sid))
            elif kind == "variable_declarator":
                target, value = node.child_by_field_name("name"), node.child_by_field_name("value")
                sid = (self.at_line(path, _text(target), node.start_point[0] + 1)
                       if target is not None and target.type == "identifier" and value is not None
                       and value.type in code_symbols.ES_CALLABLES else None)
                typed = self.es_receiver(node) if target is not None and target.type == "identifier" else None
                for name in ([_text(target)] if sid else _identifiers(target) if target is not None else []):
                    bindings[name].append(("symbol", sid) if sid else
                                          _scoped(typed or ("unknown", "assignment"), node, ES_SCOPES))
            elif kind == "formal_parameters":
                for param in node.named_children:
                    pattern = param.child_by_field_name("pattern")
                    typed = self.es_receiver(param) if pattern is not None and pattern.type == "identifier" else None
                    for name in _identifiers(param):
                        bindings[name].append(_scoped(typed or ("unknown", "parameter"), node, ES_SCOPES))
            elif kind in ES_PATTERNS or (kind == "arrow_function" and node.child_by_field_name("parameter")):
                part = node.child_by_field_name("parameter") or node.child_by_field_name("left") or node
                for name in _identifiers(part):
                    bindings[name].append(_scoped(("unknown", "parameter"), node, ES_SCOPES, own=kind == "arrow_function"))
            stack.extend(node.named_children)
        return bindings

    def rust_bindings(self, path, tree):
        bindings = collections.defaultdict(list)
        for row in self.imports[path]:
            if row["node_kind"] != "use_declaration" or not row["name"]:
                continue
            if row["name"] == "*":
                bindings["*"].append(("unknown", "glob_import"))
                continue
            target, last = row["target_path"], (row["qualname"] or "").rsplit("::", 1)[-1]
            if row["target_state"] in ("standard_library", "not_in_repo"):
                binding = ("unknown", "external_import")
            elif row["target_state"] != "resolved" or target not in self.b.files:
                binding = ("unknown", "unresolved_import")
            elif target.endswith((f"/{last}.rs", f"/{last}/mod.rs")) or target in (f"{last}.rs", f"{last}/mod.rs"):
                binding = self.rust_export(target, last) or ("module", target)
            else:
                binding = self.rust_export(target, last) or ("unknown", "import_not_function")
            bindings[row["name"]].append(binding)
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "function_item":
                name = node.child_by_field_name("name")
                sid = self.at_line(path, _text(name), node.start_point[0] + 1)
                symbol = self.b.nodes.get(sid, {})
                # Methods are reached through a receiver or type path, never by a bare name.
                if symbol.get("symbol_kind") != "method":
                    bindings[_text(name)].append(("symbol", sid) if sid else ("unknown", "function_item"))
            elif node.type in ("struct_item", "enum_item", "union_item", "trait_item", "type_item", "mod_item"):
                name = _text(node.child_by_field_name("name"))
                # A module name is a path head; the resolver walks it like `use` does.
                bindings[name].append(("path",) if node.type == "mod_item" else ("type", path, name))
            elif node.type in RUST_PATTERNS:
                field = RUST_PATTERNS[node.type]
                part = node.child_by_field_name(field) if field else node
                typed = self.rust_receiver(node) if part is not None and part.type == "identifier" else None
                own = node.type == "closure_parameters"
                for name in _identifiers(part) if part is not None else []:
                    bindings[name].append(_scoped(typed or ("unknown", "parameter_or_pattern"), node,
                                                  RUST_SCOPES, own))
            stack.extend(node.named_children)
        return bindings

    def rust_receiver(self, node):
        """('typed', T) for `x: T`, ('ctor', T, f) for `let x = T::f(..)?`; None otherwise."""
        written = node.child_by_field_name("type")
        if written is not None:
            name = code_symbols._rust_type_path(written)
            return ("typed", name) if name and name.isidentifier() else None
        value = node.child_by_field_name("value") if node.type == "let_declaration" else None
        while value is not None:
            if value.type == "try_expression" and value.named_children:
                value = value.named_children[0]
                continue
            if value.type != "call_expression":
                return None
            function = value.child_by_field_name("function")
            if function.type == "field_expression" and _text(function.child_by_field_name("field")) in RUST_PASS_THROUGH:
                value = function.child_by_field_name("value")
                continue
            segments = "".join(_text(function).split()).split("::")
            if function.type == "scoped_identifier" and len(segments) == 2 and segments[0].isidentifier():
                return ("ctor", *segments)
            return None
        return None

    def es_receiver(self, node):
        """('typed', T) for `x: T` and `x = new T()`; None otherwise."""
        written = node.child_by_field_name("type")
        if written is not None and written.type == "type_annotation":
            inner = written.named_children[0] if written.named_children else None
            return ("typed", _text(inner)) if inner is not None and inner.type == "type_identifier" else None
        value = node.child_by_field_name("value")
        if value is not None and value.type == "new_expression":
            built = value.child_by_field_name("constructor")
            return ("typed", _text(built)) if built is not None and built.type == "identifier" else None
        return None

    def rust_type(self, path, bindings, binding, line, owner):
        """(file, Type) a typed Rust binding names, or None."""
        kind, *value = binding
        if kind == "typed" and value[0] == "Self":
            parent = self.enclosing_type(owner, "::")
            return (path, parent) if parent else None
        if kind == "typed":
            kind, *value = self.lookup(bindings, value[0], line)
            return (value[0], value[1]) if kind == "type" else None
        if kind == "ctor":
            found = self.rust_type(path, bindings, ("typed", value[0]), line, owner)
            sid = found and self.declared(found[0], f"{found[1]}::{value[1]}")
            return found if sid and self.returns_self(sid, found[1]) else None
        return None

    # --- call targets ---------------------------------------------------------

    def lookup(self, bindings, name, line=None):
        """The one binding of `name` visible at `line`; function-scoped bindings need a line."""
        values = []
        for value in bindings.get(name, []):
            if value[0] == "scoped":
                (first, last), value = value[1], value[2]
                if line is None or not first <= line <= last:
                    continue
            if value not in values:
                values.append(value)
        if len(values) > 1:
            return ("unknown", "multiple_bindings")
        if values:
            return values[0]
        return ("unknown", "glob_import" if "*" in bindings else "external_or_unbound")

    def enclosing_type(self, owner, separator):
        node = self.b.nodes.get(owner, {})
        if node.get("symbol_kind") != "method":
            return None
        parent = node["qualname"].rpartition(separator)[0]
        return None if not parent or parent.endswith(">") else parent  # trait impls dispatch dynamically

    def es_target(self, path, function, bindings, owner, line):
        if function.type == "identifier":
            kind, *value = self.lookup(bindings, _text(function), line)
        elif function.type == "member_expression":
            receiver, prop = function.child_by_field_name("object"), function.child_by_field_name("property")
            if receiver is None or prop is None or prop.type not in ("property_identifier", "private_property_identifier"):
                return None, "dynamic_call"
            if receiver.type == "this":
                parent = self.enclosing_type(owner, ".")
                found = self.declared(path, f"{parent}.{_text(prop)}", ("method",)) if parent else None
                return (found, None) if found else (None, "dynamic_receiver")
            if receiver.type != "identifier":
                return None, "dynamic_receiver"
            kind, *value = self.lookup(bindings, _text(receiver), line)
            if kind == "typed":
                cls = self.lookup(bindings, value[0], line)
                if cls[0] == "class":
                    node = self.b.nodes[cls[1]]
                    found = self.declared(node["path"], f"{node['qualname']}.{_text(prop)}", ("method",))
                    return (found, "es_receiver_type") if found else (None, "unresolved_method")
            if kind != "module":
                return None, "dynamic_receiver"
            found = self.es_export(value[0], _text(prop))
            return (found, None) if found else (None, "unresolved_attribute")
        else:
            return None, "dynamic_call"
        if kind == "symbol":
            return value[0], None
        return None, value[0] if kind == "unknown" else "non_function_binding"

    def es_constructor(self, constructor, bindings, line):
        """The class `new Name()` builds, when Name binds to one."""
        if constructor is None or constructor.type != "identifier":
            return None
        kind, *value = self.lookup(bindings, _text(constructor), line)
        return value[0] if kind == "class" else None

    def rust_target(self, path, function, bindings, owner, line):
        while function.type == "generic_function":
            function = function.child_by_field_name("function")
        if function.type == "identifier":
            kind, *value = self.lookup(bindings, _text(function), line)
            if kind == "symbol":
                return value[0], None
            return None, value[0] if kind == "unknown" else "non_function_binding"
        if function.type == "field_expression":
            receiver, field = function.child_by_field_name("value"), function.child_by_field_name("field")
            if receiver is not None and receiver.type == "self" and field is not None:
                parent = self.enclosing_type(owner, "::")
                found = self.declared(path, f"{parent}::{_text(field)}", ("method",)) if parent else None
                return (found, None) if found else (None, "dynamic_receiver")
            if receiver is not None and receiver.type == "identifier" and field is not None:
                typed = self.rust_type(path, bindings, self.lookup(bindings, _text(receiver), line), line, owner)
                if typed:
                    found = self.declared(typed[0], f"{typed[1]}::{_text(field)}", ("method",))
                    return (found, "rust_receiver_type") if found else (None, "unresolved_method")
            return None, "dynamic_receiver"
        if function.type != "scoped_identifier":
            return None, "dynamic_call"
        segments = [s for s in "".join(_text(function).split()).split("::") if s]
        if any("<" in s for s in segments):
            return None, "generic_path"
        *prefix, name = segments
        if prefix == ["Self"]:
            parent = self.enclosing_type(owner, "::")
            found = self.declared(path, f"{parent}::{name}") if parent else None
            return (found, None) if found else (None, "unresolved_type_path")
        if prefix[0] not in ("crate", "self", "super"):
            kind, *value = self.lookup(bindings, prefix[0], line)
            if len(prefix) == 1 and kind in ("module", "type"):
                found = (self.declared(value[0], name, FUNCTIONS) if kind == "module"
                         else self.declared(value[0], f"{value[1]}::{name}"))
                return (found, None) if found else (None, "unresolved_type_path")
            if kind not in ("path", "unknown") or (kind == "unknown" and value[0] != "external_or_unbound"):
                return None, "unresolved_type_path" if kind != "unknown" else value[0]
        row = {"path": path, "language": "rust", "kind": "import", "qualname": "::".join(segments),
               "node_kind": "use_declaration"}
        target, state, _ = self.resolver.rust(row)
        if state in ("standard_library", "not_in_repo"):
            return None, "external_or_unbound"
        if state != "resolved" or target not in self.b.files:
            return None, "unresolved_module_path"
        found = self.declared(target, name, FUNCTIONS) or self.declared(target, f"{prefix[-1]}::{name}")
        exported = None if found else self.rust_export(target, name)
        found = found or (exported[1] if exported and exported[0] == "symbol" else None)
        return (found, None) if found else (None, "unresolved_module_path")

    # --- driver ---------------------------------------------------------------

    def run(self, path, language, ext):
        tree = code_symbols._parser(ext).parse(self.b.files[path]["text"].encode())
        bindings = (self.rust_bindings if language == "rust" else self.es_bindings)(path, tree)
        target_of = self.rust_target if language == "rust" else self.es_target
        unresolved = collections.defaultdict(list)
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.named_children))
            line = node.start_point[0] + 1
            if node.type == "new_expression":
                built = self.es_constructor(node.child_by_field_name("constructor"), bindings, line)
                if built:
                    self.b.edge(self.owner(path, line), "CALLS_STATIC", built,
                                self.b.evidence(path, line, node.end_point[0] + 1), "es_constructor")
                continue
            if node.type != "call_expression":
                continue
            function = node.child_by_field_name("function")
            if function is None or function.type == "import" or _text(function) == "require":
                continue
            owner = self.owner(path, line)
            target, reason = target_of(path, function, bindings, owner, line)
            if target:
                # A resolved target's reason, when present, names the narrower rule that resolved it.
                self.b.edge(owner, "CALLS_STATIC", target, self.b.evidence(path, line, node.end_point[0] + 1),
                            reason or RULES[language])
            else:
                unresolved[reason].append(node)
        for reason, nodes in sorted(unresolved.items()):
            first = nodes[0]
            self.b.gap("calls", reason, path, first.start_point[0] + 1, predicate="CALLS_STATIC",
                       text=_text(first).splitlines()[0][:240], calls=len(nodes), candidates=[])


def produce(builder, resolver):
    """Add TS/JS and Rust CALLS_STATIC edges and per-file call gaps to the builder."""
    calls = SyntaxCalls(builder, resolver)
    for path, row in builder.files.items():
        node = builder.nodes[builder.file_id(path)]
        language = node.get("language")
        if row["state"] != "read" or language not in RULES or node.get("parse_state") not in ("parsed", "parsed_with_errors"):
            continue
        ext = posixpath.splitext(path)[1]
        if not code_symbols.supported(ext):
            continue
        try:
            calls.run(path, language, ext)
        except code_symbols.ParserUnavailable:
            continue
