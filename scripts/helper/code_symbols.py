"""Definitions and imports from tree-sitter grammars for non-Python files.

Each language is a small table: which node kinds define a symbol, what the symbol
is called, and which literal target an import names. One iterative walker turns
those into code_items rows with the same columns and meanings as Python's ast
rows. Grammars are optional; a missing one makes the file ``parser_unavailable``.
Import targets are literal source text only: nothing is resolved or expanded.
"""
import re

# extension: (language, grammar module, grammar function)
GRAMMARS = {".rs": ("rust", "tree_sitter_rust", "language"),
            ".go": ("go", "tree_sitter_go", "language"),
            ".js": ("javascript", "tree_sitter_javascript", "language"),
            ".mjs": ("javascript", "tree_sitter_javascript", "language"),
            ".cjs": ("javascript", "tree_sitter_javascript", "language"),
            ".jsx": ("javascript", "tree_sitter_javascript", "language"),
            ".ts": ("typescript", "tree_sitter_typescript", "language_typescript"),
            ".mts": ("typescript", "tree_sitter_typescript", "language_typescript"),
            ".cts": ("typescript", "tree_sitter_typescript", "language_typescript"),
            ".tsx": ("typescript", "tree_sitter_typescript", "language_tsx"),
            ".sh": ("shell", "tree_sitter_bash", "language"),
            ".bash": ("shell", "tree_sitter_bash", "language")}
SEPARATORS = {"rust": "::"}
_parsers = {}


class ParserUnavailable(Exception):
    """The tree-sitter runtime or this extension's grammar is not installed."""


def supported(ext):
    return ext in GRAMMARS


def _parser(ext):
    if ext not in _parsers:
        _, module, function = GRAMMARS[ext]
        try:
            import importlib
            import tree_sitter
            grammar = getattr(importlib.import_module(module), function)()
            _parsers[ext] = tree_sitter.Parser(tree_sitter.Language(grammar))
        except (ImportError, AttributeError, ValueError, TypeError) as error:
            _parsers[ext] = error
    if isinstance(_parsers[ext], Exception):
        raise ParserUnavailable(f"{GRAMMARS[ext][1]}: {_parsers[ext]}")
    return _parsers[ext]


def _text(node):
    return node.text.decode("utf-8", "replace") if node is not None else None


def _field(node, name):
    return _text(node.child_by_field_name(name))


def _has_token(node, token):
    return any(child.type == token for child in node.children)


def _unquote(node):
    """Literal string content, or None when the string interpolates anything."""
    if node is None:
        return None
    if node.type in ("word", "identifier"):
        return _text(node)
    if node.type == "raw_string" or node.type.endswith("string_literal"):
        return _text(node)[1:-1]
    if node.type == "string":
        if any(child.type not in ("string_fragment", "string_content", "escape_sequence")
               for child in node.named_children):
            return None
        return _text(node)[1:-1]
    return None


# --- Rust ---------------------------------------------------------------------

RUST_KINDS = {"struct_item": "struct", "enum_item": "enum", "union_item": "union", "trait_item": "trait",
              "type_item": "type_alias", "mod_item": "module", "macro_definition": "macro"}


def _rust_type_path(node):
    """`S<T>` and `&S` name the path `S`; other types keep their source text."""
    while node is not None and node.type in ("generic_type", "reference_type"):
        node = node.child_by_field_name("type")
    return re.sub(r"\s+", "", _text(node) or "") or None


def _rust_definition(node, enclosing):
    if node.type in ("function_item", "function_signature_item"):
        name = _field(node, "name")
        if enclosing in ("impl", "trait"):
            return "method", name, [name]
        modifiers = next((c for c in node.named_children if c.type == "function_modifiers"), None)
        return ("async_function" if modifiers and "async" in _text(modifiers) else "function"), name, [name]
    if node.type == "impl_item":
        target = _rust_type_path(node.child_by_field_name("type"))
        trait = node.child_by_field_name("trait")
        if not target:
            return None
        component = f"<{target} as {re.sub(r'\s+', '', _text(trait))}>" if trait is not None else target
        return "impl", target, [component]
    if node.type in RUST_KINDS:
        name = _field(node, "name")
        return (RUST_KINDS[node.type], name, [name]) if name else None
    return None


def _rust_uses(node, prefix):
    """(path, local name) for each leaf of a `use` tree."""
    join = lambda *parts: "::".join(p for p in parts if p)
    kind = node.type
    if kind == "self":
        yield prefix, prefix.rsplit("::", 1)[-1]
    elif kind in ("identifier", "scoped_identifier", "crate", "super", "metavariable"):
        path = join(prefix, re.sub(r"\s+", "", _text(node)))
        yield path, path.rsplit("::", 1)[-1]
    elif kind == "use_as_clause":
        yield join(prefix, re.sub(r"\s+", "", _field(node, "path"))), _field(node, "alias")
    elif kind == "use_wildcard":
        base = node.named_children[0] if node.named_children else None
        yield join(prefix, re.sub(r"\s+", "", _text(base) or ""), "*"), "*"
    elif kind == "use_list":
        for child in node.named_children:
            yield from _rust_uses(child, prefix)
    elif kind == "scoped_use_list":
        path = node.child_by_field_name("path")
        yield from _rust_uses(node.child_by_field_name("list"),
                              join(prefix, re.sub(r"\s+", "", _text(path))) if path is not None else prefix)


def _rust_imports(node):
    """(name, module, qualname): module is the path a leaf is imported from."""
    if node.type == "extern_crate_declaration":
        crate = _field(node, "name")
        return [(_field(node, "alias") or crate, crate, crate)]
    if node.type != "use_declaration":
        return None
    return [(name, path.rsplit("::", 1)[0] if "::" in path else path, path)
            for path, name in _rust_uses(node.child_by_field_name("argument"), "")]


# --- TypeScript / JavaScript --------------------------------------------------

ES_KINDS = {"class_declaration": "class", "abstract_class_declaration": "class",
            "interface_declaration": "interface", "type_alias_declaration": "type_alias",
            "enum_declaration": "enum", "internal_module": "namespace", "module": "namespace"}
ES_CALLABLES = ("arrow_function", "function_expression", "function", "generator_function")
ES_MEMBER_PARENTS = ("class_body", "interface_body")


def _es_definition(node, enclosing):
    kind = node.type
    if kind in ("function_declaration", "generator_function_declaration", "function_signature"):
        name = _field(node, "name")
        return ("async_function" if _has_token(node, "async") else "function"), name, [name]
    if kind in ("method_definition", "method_signature", "abstract_method_signature"):
        if node.parent is None or node.parent.type not in ES_MEMBER_PARENTS:
            return None  # object-literal methods are values, not declarations
        name = _field(node, "name")
        return "method", name, [name]
    if kind == "public_field_definition":
        value = node.child_by_field_name("value")
        if value is None or value.type not in ES_CALLABLES:
            return None
        name = _field(node, "name")
        return "method", name, [name]
    if kind == "variable_declarator":
        value, target = node.child_by_field_name("value"), node.child_by_field_name("name")
        if value is None or value.type not in ES_CALLABLES or target is None or target.type != "identifier":
            return None
        name = _text(target)
        return ("async_function" if _has_token(value, "async") else "function"), name, [name]
    if kind in ES_KINDS:
        target = node.child_by_field_name("name")
        name = _unquote(target) if target is not None and target.type == "string" else _text(target)
        return (ES_KINDS[kind], name, [name]) if name else None
    return None


def _es_clause_rows(clause, module):
    rows = []
    for child in clause.named_children:
        if child.type == "identifier":
            rows.append((_text(child), module, f"{module}#default"))
        elif child.type in ("namespace_import", "namespace_export"):
            alias = next((c for c in child.named_children), None)
            rows.append((_text(alias) if alias is not None else "*", module, f"{module}#*"))
        elif child.type in ("named_imports", "export_clause"):
            for spec in child.named_children:
                if spec.type in ("import_specifier", "export_specifier"):
                    name = _field(spec, "name")
                    rows.append((_field(spec, "alias") or name, module, f"{module}#{name}"))
    return rows


def _es_imports(node):
    """Static imports, re-exports, and literal `require(...)`/`import(...)` calls."""
    kind = node.type
    if kind == "import_statement":
        require = next((c for c in node.named_children if c.type == "import_require_clause"), None)
        if require is not None:
            module = _unquote(require.child_by_field_name("source"))
            alias = next((c for c in require.named_children if c.type == "identifier"), None)
            return [(_text(alias), module, module)] if module is not None else []
        module = _unquote(node.child_by_field_name("source"))
        if module is None:
            return []
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        return _es_clause_rows(clause, module) if clause is not None else [(None, module, module)]
    if kind == "export_statement" and node.child_by_field_name("source") is not None:
        module = _unquote(node.child_by_field_name("source"))
        if module is None:
            return []
        rows = _es_clause_rows(node, module)
        return rows or [("*", module, f"{module}#*")]
    if kind == "call_expression":
        function, arguments = node.child_by_field_name("function"), node.child_by_field_name("arguments")
        if function is None or arguments is None or len(arguments.named_children) != 1:
            return None
        if not (function.type == "import" or (function.type == "identifier" and _text(function) == "require")):
            return None
        module = _unquote(arguments.named_children[0])
        if module is None:
            return None
        parent = node.parent
        name = _field(parent, "name") if parent is not None and parent.type == "variable_declarator" else None
        return [(name, module, module)]
    return None


# --- Go -----------------------------------------------------------------------

def _go_receiver(node):
    if node is None:
        return None
    declaration = next((c for c in node.named_children if c.type == "parameter_declaration"), None)
    target = declaration.child_by_field_name("type") if declaration is not None else None
    while target is not None and target.type in ("pointer_type", "generic_type"):
        target = target.child_by_field_name("type") or (target.named_children[0] if target.named_children else None)
    return _text(target)


def _go_definition(node, enclosing):
    kind = node.type
    if kind == "function_declaration":
        name = _field(node, "name")
        return "function", name, [name]
    if kind == "method_declaration":
        name, receiver = _field(node, "name"), _go_receiver(node.child_by_field_name("receiver"))
        return ("method", name, [receiver, name]) if receiver else ("method", name, [name])
    if kind == "method_elem" and enclosing == "interface":
        name = _field(node, "name")
        return "method", name, [name]
    if kind == "type_spec":
        value = node.child_by_field_name("type")
        name = _field(node, "name")
        shape = {"struct_type": "struct", "interface_type": "interface"}.get(value.type if value else "", "type")
        return shape, name, [name]
    if kind == "type_alias":
        name = _field(node, "name")
        return "type_alias", name, [name]
    return None


def _go_imports(node):
    if node.type != "import_spec":
        return None
    path = _unquote(node.child_by_field_name("path"))
    return [(_field(node, "name") or path, path, path)] if path is not None else []


# --- Shell --------------------------------------------------------------------

def _shell_definition(node, enclosing):
    if node.type == "function_definition":
        name = _field(node, "name")
        return "function", name, [name]
    return None


def _shell_imports(node):
    """`source PATH` and `. PATH` with a literal path; expansions are skipped."""
    if node.type != "command" or _field(node, "name") not in ("source", "."):
        return None
    argument = node.child_by_field_name("argument")
    path = _unquote(argument)
    return [(path, path, path)] if path else []


LANGUAGE_RULES = {"rust": (_rust_definition, _rust_imports), "go": (_go_definition, _go_imports),
                  "javascript": (_es_definition, _es_imports), "typescript": (_es_definition, _es_imports),
                  "shell": (_shell_definition, _shell_imports)}


def symbols(data, repo_id, path, ext):
    """(rows, first syntax error or None) for one file's bytes; raises ParserUnavailable."""
    language = GRAMMARS[ext][0]
    tree = _parser(ext).parse(data)
    definition, imports = LANGUAGE_RULES[language]
    separator = SEPARATORS.get(language, ".")
    parser = f"tree-sitter-{language}"
    rows = []

    def row(kind, name, qualname, parent, module, node, statement):
        rows.append({"repo_id": repo_id, "path": path, "kind": kind, "name": name, "qualname": qualname,
                     "parent": parent, "module": module, "line": node.start_point[0] + 1,
                     "end_line": node.end_point[0] + 1, "parser": parser, "language": language,
                     "node_kind": statement})

    # Iterative walk: deep expression trees must not exhaust Python's stack.
    stack = [(child, (), None) for child in reversed(tree.root_node.named_children)]
    while stack:
        node, scope, enclosing = stack.pop()
        parent = separator.join(scope) or None
        found = imports(node)
        if found is not None:
            for name, module, qualname in found:
                row("import", name, qualname, parent, module, node, node.type)
            continue
        defined = definition(node, enclosing)
        if defined and defined[1]:
            kind, name, relative = defined
            components = scope + tuple(relative)
            row(kind, name, separator.join(components), separator.join(components[:-1]) or None, None, node,
                node.type)
            scope, enclosing = components, kind
        stack.extend((child, scope, enclosing) for child in reversed(node.named_children))
    return rows, first_error(tree.root_node)


def first_error(root):
    """'line N: ...' for the first ERROR or MISSING node, or None when the tree parsed cleanly."""
    if not root.has_error:
        return None
    node = root
    while True:
        child = next((c for c in node.children if c.type == "ERROR" or c.is_missing or c.has_error), None)
        if child is None or child.type == "ERROR" or child.is_missing:
            node = child or node
            break
        node = child
    text = (_text(node) or "").strip().splitlines()
    what = f"missing {node.type}" if node.is_missing else "unexpected " + repr((text[0] if text else "")[:60])
    return f"line {node.start_point[0] + 1}: {what}"
