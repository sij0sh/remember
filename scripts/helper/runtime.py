"""Single entrypoint runtime: deterministic plans, lazy providers and evidence expansion.

The common query path is SQLite, including graph, Git, recordings and custom
inputs.
"""
import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import catalog, continuations, delivery, commit_rank, file_observations, graph_cli, investigate, session_show, signals, target_history, trace
from .bindings import parse_params, parse_types, types_for
from .budget import Deadline, POLICIES
from .engine import Input, SQLiteQuery, IDENTIFIER, infer_type
from .envelope import SCHEMA_VERSION, load_rows
from .graph_output import compact_row
from .graph_queries import DEPENDENCIES, reference, impact
from .graph_store import Store, digest, require_current
from .operations import COMMON, OPERATIONS, derive, graph_parser
from .providers import Providers


@dataclass(frozen=True)
class Request:
    args: argparse.Namespace
    argv: tuple
    expected_signature: str | None = None


@dataclass
class Result:
    document: dict
    signature: str | None = None
    argv: list | None = None


def parser():
    return graph_parser()


def normalize_prefix(argv):
    """Move recognized leading global flags after the verb, respecting option values."""
    p = parser()
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith('-'):
            break
        name = token.split('=', 1)[0]
        option = p._option_string_actions.get(name)
        if option is None or name in ('--help', '-h'):
            break
        index += 1
        if '=' not in token and option.nargs != 0:
            index += 1
    if index and index < len(argv) and argv[index] in OPERATIONS:
        return [argv[index], *argv[:index], *argv[index + 1:]]
    return argv


def normalize(args, argv):
    args = derive(args)
    if args.action in ("query", "inspect", "show", "activity", "signals", "trace", "investigate"):
        policy = POLICIES[args.budget]
        if args.action == "investigate":
            # Old work is the point of prior_work: always read the deep commit window (cheap).
            policy = {**policy, "max_commits": POLICIES["deep"]["max_commits"]}
        for key, value in policy.items():
            flag = '--' + key.replace('_', '-')
            if not any(token == flag or token.startswith(flag + '=') for token in argv):
                setattr(args, key, value)
        # Continuations always carry an absolute scope, independent of the next cwd.
        args.repo = str(Path(args.repo).expanduser().resolve())
    if args.action == "query" and not args.sql_text:
        raise ValueError("Supply query SQL; describe TABLE lists columns")
    return args


def _counts(rows, field):
    out = {}
    for row in rows:
        out[row.get(field)] = out.get(row.get(field), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: str(kv[0])))


def custom_inputs(args):
    result, remaining = {}, 16 * 1024 * 1024
    declared = parse_types(args.type)
    for definition in args.input:
        name, sep, path = definition.partition('=')
        if not sep or not IDENTIFIER.fullmatch(name) or not path:
            raise ValueError("Use --input NAME=FILE (JSON, JSONL or -)")
        if name.lower() in {n.lower() for n in (*catalog.CATALOG, *result)}:
            raise ValueError(f"Reserved or duplicate input table: {name}")
        if path == '-':
            raw = sys.stdin.buffer.read(remaining + 1)
        else:
            with open(Path(path).expanduser(), 'rb') as stream:
                raw = stream.read(remaining + 1)
        remaining -= len(raw)
        if remaining < 0:
            raise ValueError("Custom inputs exceed the combined 16 MiB limit")
        rows, upstream = load_rows(raw.decode('utf-8'))
        if len(rows) > args.max_query_rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Custom inputs require object rows within the row budget")
        overrides = types_for(declared, name)
        names = sorted({key for row in rows for key in row} | set(overrides))
        if not names:
            raise ValueError(f"Empty table {name} needs --type {name}.COLUMN=TYPE")
        if len(names) > 200:
            raise ValueError("Custom tables allow at most 200 columns")
        types = {column: overrides.get(column) or {"str": "TEXT", "int": "INTEGER", "float": "REAL", "bool": "BOOLEAN"}[
                    infer_type([row.get(column) for row in rows])] for column in names}
        result[name] = Input(name, rows, upstream, names, types, "one supplied row")
    unknown = {view for view, _, _ in declared} - set(result)
    if unknown:
        raise ValueError(f"--type applies to supplied tables only: {', '.join(sorted(unknown))}")
    if sum(len(item.rows) for item in result.values()) > args.max_query_rows:
        raise ValueError("Custom inputs exceed the combined row budget")
    return result


# inspect bounds: source preview, executable candidate choices.
PREVIEW_LINES, CANDIDATE_CHOICES = 80, 5


class Runtime:
    def __init__(self, request):
        self.request = request
        self.args = request.args
        self.store = Store(self.args.repo, self.args.cache)

    def base_argv(self):
        argv = list(self.request.argv)
        argv = continuations.replace_option(argv, '--repo', self.store.root)
        argv = continuations.replace_option(argv, '--cache', str(self.store.path.parent))
        # Normalize both --flag VALUE and --flag=VALUE without shell parsing.
        for index, token in enumerate(argv):
            flag, equal, inline = token.partition('=')
            if flag not in ('--input', '--recordings', '--project-root'):
                continue
            at = index if equal else index + 1
            if at >= len(argv):
                continue
            value = inline if equal else argv[at]
            if flag == '--input':
                name, sep, path = value.partition('=')
                if sep and path != '-':
                    value = name + '=' + str(Path(path).expanduser().resolve())
            else:
                value = str(Path(value).expanduser().resolve())
            argv[at] = flag + '=' + value if equal else value
        return argv

    def recovery(self, argv, purpose):
        """Widen the scan boundary once: standard to deep, dropping explicit limits the deep policy replaces."""
        if self.args.budget != 'standard' or argv is None:
            return None
        for key in POLICIES['deep']:
            argv = continuations.replace_option(argv, '--' + key.replace('_', '-'), None)
        argv = continuations.replace_option(argv, '--offset', None)
        return continuations.encode(continuations.replace_option(argv, '--budget', 'deep'), None,
                                    purpose + '; changes the scan boundary to --budget deep', 'coverage')

    def query(self, deadline):
        args = self.args
        supplied = custom_inputs(args)
        schemas = {name: item.columns for name, item in catalog.CATALOG.items()}
        schemas.update({name: item.types for name, item in supplied.items()})
        with SQLiteQuery(schemas, deadline.remaining()) as engine:
            try:
                reads = engine.plan(args.sql_text, parse_params(args.param))
            except ValueError as error:
                raise ValueError(f"{error}; use describe TABLE for columns (SQLite dialect)") from None
            names = set(reads)
            plan = {"tables": reads, "providers": catalog.providers_for(names), "budget": args.budget,
                    "limits": {key: getattr(args, key) for key in POLICIES[args.budget]},
                    "dialect": "sqlite", "retention": "transient"}
            if args.plan:
                return Result({"operation": "query", "results": [{"table": name, "columns_read": columns,
                                "provider": catalog.CATALOG[name].provider if name in catalog.CATALOG else "supplied"}
                               for name, columns in reads.items()], "plan": plan}, argv=self.base_argv())
            providers = Providers(args, self.store, deadline)
            providers.load(names)
            count = 0
            fingerprint = []
            for name in sorted(names):
                item = supplied[name] if name in supplied else providers.input(name)
                count += len(item.rows)
                if count > args.max_query_rows:
                    raise ValueError("Required inputs exceed the row budget; narrow source scope or use --budget deep")
                fingerprint.append({"name": name, "rows": digest(item.rows), "coverage": item.upstream})
                engine.load(item)
            # SQL runs against full loaded inputs. Delivery paging happens afterwards.
            cap = min(args.max_query_rows, max(1000, args.offset + args.limit))
            output = engine.evaluate(cap)
        coverage = dict(providers.coverage)
        for name in names & set(supplied):
            upstream = supplied[name].upstream
            coverage[name] = {**upstream, "complete": upstream.get("complete"),
                              "basis": "supplied rows only; external completeness is not assumed"}
        partial = any(c.get("complete") is not True for c in coverage.values())
        argv = self.base_argv()
        if providers.graph:
            argv = continuations.replace_option(argv, '--snapshot', providers.graph.snapshot)
        recover = None
        if any(c.get("complete") is False for c in providers.coverage.values()):
            recover = self.recovery(argv, "Rerun with wider provider limits")
        return Result({"operation": "query", "status": "partial" if partial else "ok", "next": [recover] if recover else [],
                       "results": output["results"], "more_rows": not output["query_complete"],
                       "coverage": coverage, "scope": {"engine": "sqlite", "retention": "transient",
                       "providers": providers.loaded, "counting_scope": "full bounded provider selections before delivery"}},
                      digest(fingerprint), argv)

    def inspect(self, deadline):
        args = self.args
        stages = ['resolve', 'source', 'packet', 'relationships', 'history', 'expand']
        if args.plan:
            return Result({"operation": "inspect", "results": [{"stage": s} for s in stages],
                           "plan": {"intent": args.intent, "budget": args.budget, "limits": POLICIES[args.budget],
                                    "history": "disabled sessions" if args.no_sessions else "target-scoped"}})
        graph = graph_cli.get_graph(self.store, args)
        matches = graph.resolve(args.target)
        if len(matches) != 1:
            candidates = [graph.public(graph.nodes[n]) for n in matches]
            argv = continuations.replace_option(self.base_argv(), '--snapshot', graph.snapshot)
            at = argv.index(args.target, 1) if args.target in argv[1:] else None
            # One executable choice per candidate, pinned to the snapshot that produced the ambiguity.
            choices = [continuations.encode(argv[:at] + [c['ref']] + argv[at+1:], None,
                                            f"Inspect candidate {c['ref']}", "expansion")
                       for c in candidates[:CANDIDATE_CHOICES]] if at else []
            if len(candidates) > len(choices) or not matches:
                choices.append({"purpose": "Choose one candidate ref, then inspect it" if matches
                                else "Use map or find to locate the target"})
            return Result({"operation": "inspect", "status": "partial", "results": candidates,
                           "coverage": {"target": {"complete": False, "reason": "ambiguous" if matches else "unresolved",
                                                   "candidates": len(candidates)}},
                           "next": choices})
        node = graph.public(graph.nodes[matches[0]])
        if node['kind'] not in ('file', 'symbol'):
            raise ValueError("inspect requires a file or symbol; use map for a directory")
        pinned = copy.copy(args)
        pinned.snapshot, pinned.target = graph.snapshot, node['ref']
        rows, steps, coverage = [], [], {}
        src = next(r for r in graph.body['source']['files'] if r['path'] == node['path'])
        require_current(graph.snapshot, src)
        text = (src.get('text') or '').splitlines()
        start, end = node.get('line') or 1, node.get('end_line') or len(text)
        source_ref = reference(graph.snapshot, 'source', node['path'])
        rows.append({"section": "source", "path": node['path'], "line": start,
                     "text": '\n'.join(text[start-1:min(end, start+PREVIEW_LINES-1)]), "ref": source_ref,
                     "text_complete": src.get('text') is not None and end < start+PREVIEW_LINES})
        steps += [{"stage": "resolve", "state": "completed"}, {"stage": "source", "state": "completed"}]
        deadline.check()
        if args.intent == 'change':
            rel = impact(graph, [node['ref']], depth=args.depth, max_nodes=args.max_nodes, seconds=deadline.remaining())
            rows += [{"section": "relationships", **r} for r in rel['results']]
            rel_incomplete = graph_cli.incomplete(rel, graph)
        else:
            grain = 'symbol' if node['kind'] == 'symbol' else 'file'
            found, limits = graph.walk(graph.seeds(node['ref'], grain), 'both', DEPENDENCIES, grain,
                                       args.depth, args.max_nodes, seconds=deadline.remaining())
            rows += [{"section": "relationships", **r} for r in found]
            rel = graph.document('relationships', found, traversal=limits)
            if limits['bounds']:
                rel['gaps'] = [*rel['gaps'], {"area": "traversal", "reason": "bounded"}]
            rel_incomplete = graph_cli.incomplete(rel, graph)
        coverage['relationships'] = {"complete": not rel_incomplete, "gaps": rel_incomplete,
                                     "meaning": "static potential relationships; absence is not proof of safety"}
        steps.append({"stage": "relationships", "state": "bounded" if rel_incomplete else "completed"})
        deadline.check()
        # One scheduled scan feeds both the packet and target history.
        providers = Providers(pinned, self.store, deadline)
        name = node.get('qualname') or ''
        packet, active_state = investigate.collect(pinned, providers, deadline, name.replace('.', ' '),
                                                   [node['path']], [], sessions=not args.no_sessions, graph=graph)
        scanned = None if args.no_sessions else (providers.recordings, providers.session_coverage())
        history = target_history.run(pinned, self.store, lambda store, options, pin=None: graph, scanned=scanned)
        compact = target_history.compact(history['results'])
        leads = investigate.order(list(packet.leads.values()), [])
        views = investigate.views(leads, providers, not args.no_sessions)
        # Packet before relationships, which can fill the first delivery page.
        after_source = next((i for i, r in enumerate(rows) if r["section"] != "source"), len(rows))
        rows[after_source:after_source] = leads
        rows += [{"section": "history", **r} for r in compact]
        coverage['selection'] = {"eligible": len(leads), "suppressed": packet.suppressed}
        steps.append({"stage": "packet", "state": "not_searched" if args.no_sessions else "completed"})
        history_partial = any(c.get('state') in ('partial', 'unavailable') for c in history['coverage'].get('git', []))
        history_partial |= history['coverage'].get('sessions', {}).get('state') in ('partial', 'unavailable')
        coverage['history'] = {"complete": not history_partial, "details": target_history.summary(history['coverage']),
                               **({"allocation": providers.allocation} if providers.allocation else {})}
        steps.append({"stage": "history", "state": "bounded" if history_partial else "completed"})
        # Expand one strongest session lead. The selected event remains distinguishable from previews.
        expanded = False
        for row in history['results']:
            ref = row.get('ref')
            if isinstance(ref, dict) or (isinstance(ref, str) and '@' in ref and '#' in ref):
                deadline.check()
                try:
                    selected = session_show.expand(json.dumps(ref) if isinstance(ref, dict) else ref)
                    for event in selected.get('results', []):
                        if event.get('selected'):
                            rows.append({"section": "evidence", **event})
                            expanded = True
                    coverage['expansion'] = {"complete": expanded}
                except (ValueError, OSError) as error:
                    coverage['expansion'] = {"complete": False, "reason": str(error)}
                break
        steps.append({"stage": "expand", "state": "completed" if expanded else "not_available"})
        argv = continuations.replace_option(self.base_argv(), '--snapshot', graph.snapshot)
        frontier = investigate.frontier_token(active_state, packet.active)
        if frontier:
            argv = continuations.replace_option(argv, '--frontier', frontier)
        next_items = []
        if history_partial:
            recover = self.recovery(argv, "Rescan target history with wider recording limits")
            if recover:
                next_items.append(recover)
        if src.get('text') is not None and end >= start+PREVIEW_LINES:
            next_items.append(continuations.encode(
                ['show', source_ref, '--start-line', str(start+PREVIEW_LINES), '--lines', str(end-start-PREVIEW_LINES+1),
                 '--repo', self.store.root, '--cache', str(self.store.path.parent)], None,
                "Read the rest of the target source", "expansion"))
        if node.get('path'):
            next_items.append(self.signals_ref(node['path']))
        document = {"operation": "inspect", "target": node, "views": views, "results": rows, "steps": steps,
                    "coverage": coverage,
                    "scope": {"intent": args.intent, "snapshot": graph.snapshot, "retention": "transient",
                              "active": {k: v for k, v in active_state.items() if k != "sha256"}},
                    "status": "partial" if any(c.get('complete') is False for c in coverage.values()) else "ok", "next": next_items}
        return Result(document, digest([graph.snapshot, history['results'], history['coverage'], leads,
                                        providers.coverage.get('recordings', {}).get('signature'),
                                        active_state.get('end'), active_state.get('sha256')]), argv)

    def signals_ref(self, path):
        """Executable `signals PATH` expansion carrying this request's recording scope (D72)."""
        argv = ['signals', path, '--repo', self.store.root, '--cache', str(self.store.path.parent)]
        for name in ('recordings', 'project_root', 'exclude_session'):
            for value in getattr(self.args, name, None) or ():
                argv += ['--' + name.replace('_', '-'), value]
        return continuations.encode(argv, None, f"Recorded signals for {path}", "expansion")

    def target_operations(self, deadline, operation, scheduled=False):
        """Shared activity/signals target selection: (candidates Result, None) or (None, context)."""
        args = self.args
        target = args.target
        path = qualname = snapshot = None
        candidates = []
        if "::" in target or target.startswith(("snapshot:", "node:")):
            resolved, graph = target_history.resolve(
                args, self.store, lambda store, options, pin=None: graph_cli.get_graph(store, options, pin))
            if resolved.get("resolution") in ("ambiguous", "unresolved"):
                candidates = resolved.get("candidates") or []
                reason = resolved.get("resolution")
                return Result({"operation": operation, "status": "partial", "results": candidates,
                               "coverage": {"target": {"complete": False, "reason": reason,
                                                       "candidates": len(candidates)}},
                               "next": [{"purpose": f"Choose one candidate ref, then run {operation} on it"}] if candidates
                               else [{"purpose": "Use map or find to locate the target"}]},
                              digest(candidates), self.base_argv()), None
            if resolved.get("kind") not in ("file", "symbol"):
                raise ValueError(f"{operation} requires a file or symbol target")
            path, qualname = resolved["path"], resolved.get("qualname")
            snapshot = resolved.get("snapshot") or (graph.snapshot if graph else None)
            if snapshot:
                # Reuse the resolved snapshot for any symbol edit mapping provider.
                args.snapshot = snapshot
        else:
            path = target_history.relative(target)
        providers = Providers(args, self.store, deadline)
        providers.scheduled = scheduled
        providers.load({"operations", "commit_links"})
        operations = providers.tables.get("operations", [])
        witnesses = providers.tables.get("commit_links", [])
        symbol_events = None
        if qualname:
            providers.load({"edit_symbols"})
            symbol_events = {(row.get("recording_id"), row.get("event_id"))
                             for row in providers.tables.get("edit_symbols", [])
                             if row.get("path") == path and row.get("qualname") == qualname}
        by_operation, by_event = {}, {}
        for witness in witnesses:
            by_operation.setdefault(witness.get("operation_id"), []).append(witness)
            by_event.setdefault((witness.get("recording_id"), witness.get("event_id")), []).append(witness)
        selected = []
        for row in operations:
            if row.get("path") != path or row.get("path_location") != "repository":
                continue
            if providers.repo_path(row) != path:
                continue
            if symbol_events is not None:
                touched = {(row.get("recording_id"), row.get("call_event_id")),
                           (row.get("recording_id"), row.get("result_event_id"))}
                if not any(event in symbol_events for event in touched if event[1]):
                    continue
            found = list(by_operation.get(row.get("operation_id"), []))
            for name in ("call_event_id", "result_event_id"):
                found += by_event.get((row.get("recording_id"), row.get(name)), [])
            unique = {}
            for witness in found:
                unique[witness.get("witness_id")] = witness
            selected.append({**row, "commit_witnesses": [
                {name: w.get(name) for name in ("witness_id", "sha", "recorded_sha", "basis", "confidence",
                                                "failed", "relation", "ref", "result_ref")}
                for w in unique.values()]})
        selected.sort(key=lambda r: (r.get("lane") or "", r.get("order") or 0, r.get("association") or 0,
                                     r.get("association_key") or ""))
        return None, {"path": path, "qualname": qualname, "snapshot": snapshot, "providers": providers,
                      "selected": selected, "symbol_events": symbol_events}

    def activity(self, deadline):
        early, found = self.target_operations(deadline, "activity")
        if early:
            return early
        path, qualname, snapshot = found["path"], found["qualname"], found["snapshot"]
        providers, selected, symbol_events = found["providers"], found["selected"], found["symbol_events"]
        providers.load({"file_observations"})
        observed, changed = file_observations.target_rows(providers, path, selected, qualname)
        coverage = {name: {key: value for key, value in item.items() if key != "gaps"}
                    for name, item in providers.coverage.items()}
        operation_coverage = dict(providers.coverage.get("operations", {}))
        coverage["activity"] = {
            "matched_rows": len(selected),
            "matched_operations": len({row.get("operation_id") for row in selected}),
            "unresolved_paths": operation_coverage.get("locations", {}).get("unresolved", 0),
            "external_paths": operation_coverage.get("locations", {}).get("external", 0),
            "unsupported_tools": operation_coverage.get("unsupported_tools", {}),
            "none_found_in_scanned_scope": not selected,
            "symbol_resolution": ("symbol_event_mapping" if qualname and symbol_events else
                                  "unmapped" if qualname else "path"),
            "symbol_mapped_events": len(symbol_events) if symbol_events is not None else None,
            "file_observations": len(observed), "changes": len(changed),
            "by_completeness": _counts(observed, "completeness"), "by_attribution": _counts(changed, "attribution"),
            "scope": "scanned recordings and exact repository paths only; no baseline matching",
        }
        partial = any(item.get("complete") is not True for name, item in coverage.items() if name != "activity")
        argv = self.base_argv()
        recover = self.recovery(argv, "Widen recording limits for activity") if partial else None
        document = {"operation": "activity", "status": "partial" if partial else "ok",
                    "target": {"path": path, "qualname": qualname, "snapshot": snapshot},
                    "results": selected + observed + changed, "coverage": coverage,
                    "scope": {"repo": self.store.root, "retention": "transient",
                              "ordering": "lane, then recorded physical order",
                              "meaning": "recorded operations; matching a path does not prove a commit contains the edit"},
                    "next": [recover] if recover else []}
        return Result(document, digest([selected, observed, changed, coverage]), argv)

    def trace(self, deadline):
        providers = Providers(self.args, self.store, deadline)
        document, signature = trace.build(self.args, self.store, providers, deadline, self.base_argv())
        return Result(document, signature, self.base_argv())

    def signals(self, deadline):
        args = self.args
        if args.plan:
            return Result({"operation": "signals", "results": signals.plan(),
                           "plan": {"budget": args.budget, "sessions": not args.no_sessions, "kinds": list(signals.KINDS),
                                    "limits": {k: getattr(args, k) for k in POLICIES[args.budget]}}})
        argv = self.base_argv()
        if args.no_sessions:
            providers = Providers(args, self.store, deadline)
            views = signals.views({}, providers, False)
            return Result({"operation": "signals", "status": "ok", "views": views, "results": [],
                           "coverage": {"recordings": {"state": "not_searched"}}, "next": []}, digest(views), argv)
        early, found = self.target_operations(deadline, "signals", scheduled=True)
        if early:
            return early
        providers = found["providers"]
        rows, by_kind, selection = signals.collect(providers, found["selected"])
        views = signals.views(by_kind, providers, True)
        coverage = {name: {k: v for k, v in item.items() if k not in ("gaps", "signature")}
                    for name, item in providers.coverage.items()}
        coverage["signals"] = selection
        partial = any(item.get("complete") is False for item in providers.coverage.values())
        recover = self.recovery(argv, "Widen recording limits for signals") if partial else None
        document = {"operation": "signals", "status": "partial" if partial else "ok", "views": views,
                    "target": {"path": found["path"], "qualname": found["qualname"], "snapshot": found["snapshot"]},
                    "results": rows, "coverage": coverage,
                    "scope": {"repo": self.store.root, "retention": "transient",
                              "ordering": "round-robin across kinds, newest first within a kind",
                              "meaning": "leads with refs; repeated reads do not prove waste, errors do not prove pitfalls"},
                    "next": [recover] if recover else []}
        return Result(document, digest([rows, views, providers.coverage.get("recordings", {}).get("signature")]), argv)

    def investigate(self, deadline):
        args = self.args
        goal = ' '.join(args.goal)
        if goal and not commit_rank.terms(goal):
            raise ValueError("investigate GOAL needs descriptive words (3+ letters, not only generic verbs like 'add');"
                             " omit GOAL for repository orientation")
        paths = investigate.validate_paths(args.path)
        if args.plan:
            return Result({"operation": "investigate", "results": investigate.plan(),
                           "plan": {"budget": args.budget, "sessions": not args.no_sessions,
                                    "limits": {k: getattr(args, k) for k in POLICIES[args.budget]},
                                    "graph": "not used"}})
        providers = Providers(args, self.store, deadline)
        providers.load({"commits", "changes"})
        packet, active_state = investigate.collect(args, providers, deadline, goal, paths, args.error,
                                                   sessions=not args.no_sessions)
        active = packet.active
        deadline.check()
        leads = list(packet.leads.values())
        prior = packet.prior
        rows = investigate.order(leads, prior)
        views = investigate.views(leads, providers, not args.no_sessions)
        recordings = providers.coverage.get("recordings", {})
        coverage = {name: {k: v for k, v in item.items() if k not in ("gaps", "signature")}
                    for name, item in providers.coverage.items()}
        coverage["seeds"] = packet.seed_coverage
        coverage["selection"] = {"eligible": len(leads), "prior_work": len(prior), "suppressed": packet.suppressed,
                                 "meaning": "suppression is selection policy, not a scan gap"}
        active_scope = {k: v for k, v in active_state.items() if k not in ("sha256",)}
        scope = {"repo": self.store.root, "git": investigate.git_scope(providers), "paths": paths,
                 "goal_terms": packet.words, "errors": args.error, "active": active_scope,
                 "archive_horizon": commit_rank.horizon(providers.tables.get("commits", []), providers.tables.get("sessions", [])),
                 "retention": "transient",
                 "meaning": "leads with evidence refs, not accepted rules or task verdicts; read source before acting"}
        partial = any(c.get("complete") is False for c in providers.coverage.values())
        argv = self.base_argv()
        pinned = investigate.frontier_token(active_state, active)
        if pinned:
            argv = continuations.replace_option(argv, '--frontier', pinned)
            argv = continuations.replace_option(argv, '--session', None)
        recover = self.recovery(argv, "Rescan with the deep recording and commit allowance") if partial else None
        expansions = [self.signals_ref(path) for path in paths[:3]]
        document = {"operation": "investigate", "status": "partial" if partial else "ok", "views": views,
                    "results": rows, "coverage": coverage, "scope": scope,
                    "next": ([recover] if recover else []) + expansions}
        signature = digest([goal, paths, args.error, rows, views, recordings.get("signature"),
                            active_state.get("end"), active_state.get("sha256"), scope["git"]])
        return Result(document, signature, argv)

    def show(self, deadline):
        ref = self.args.ref
        if isinstance(ref, str) and (ref.startswith('{') or ('@' in ref and '#' in ref)):
            result = session_show.expand(ref)
            return Result(result, digest(result.get('results', [])), self.base_argv())
        if re.fullmatch(r'(?:git:)?[0-9a-fA-F]{40}', ref):
            from .git_resolve import GitResolver
            sha = ref.removeprefix('git:')
            text = GitResolver().run(self.store.root, 'show', '--no-ext-diff', '--no-textconv', '--format=fuller', sha,
                                     timeout=deadline.remaining())
            if text is None:
                raise ValueError('Commit unavailable in the selected repository')
            rows = [{"line": i+1, "text": line, "ref": sha} for i,line in enumerate(text.splitlines())]
            return Result({"operation": "show", "results": rows, "coverage": {"commit": {"complete": True}}}, digest(text), self.base_argv())
        doc = graph_cli.show(self.store, self.args)
        return Result(doc, digest(doc), self.base_argv())

    def execute(self):
        with Deadline(getattr(self.args, 'seconds', 30)) as deadline:
            result = getattr(self, self.args.action)(deadline)
        if self.request.expected_signature and result.signature != self.request.expected_signature:
            raise ValueError("Continuation source changed; rerun the original request. Paging was not silently recomputed.")
        return result


def resume(p, args, argv, incoming):
    """Replace a continue: token with its saved request; presentation may change, evidence scope may not."""
    saved = continuations.decode(args.ref)
    argv = saved['argv']
    for flag in ('--max-bytes', '--format', '--fields', '--limit'):
        for index, token in enumerate(incoming):
            if token == flag and index + 1 < len(incoming):
                argv = continuations.replace_option(argv, flag, incoming[index + 1])
            elif token.startswith(flag + '='):
                argv = continuations.replace_option(argv, flag, token.split('=', 1)[1])
    if continuations.stdin_tables(argv) and sys.stdin.isatty():
        # Never block waiting on a terminal; the signature rejects different piped rows.
        raise ValueError("This continuation replays stdin: pipe the same input again")
    return normalize(p.parse_args(argv), argv), argv, saved.get('signature')



def run_describe(args, argv, expected):
    rows = catalog.describe(args.table)
    scope = {"common_commands": COMMON, "dialect": "sqlite"}
    if args.table is None:
        rows = [{"kind": "command", "name": name, "description": description}
                for name, description in COMMON.items()] + [{"kind": "table", **r} for r in rows]
    doc = {"operation": "describe", "results": rows, "snapshots": [], "scope": {**scope, "retention": "transient"}}
    signature = digest(rows)
    if expected and signature != expected:
        raise ValueError("Continuation source changed; rerun the original request. Paging was not silently recomputed.")
    base = list(argv) if argv and argv[0] == "describe" else ["describe", *argv]

    def next_page(offset, empty, need=None):
        following = continuations.replace_option(base, '--offset', offset)
        if empty:
            following = continuations.replace_option(following, '--max-bytes', max(args.max_bytes * 2, need or 0))
        return continuations.encode(following, signature, 'Read the next page')
    delivery.deliver(doc, args, continuation=next_page)
    return 0


def run_runtime(args, argv, expected):
    result = Runtime(Request(args, tuple(argv), expected)).execute()

    def next_page(offset, empty, need=None):
        following = continuations.replace_option(result.argv, '--offset', offset)
        effect = 'delivery'
        if args.action == 'query' and offset >= args.max_query_rows:
            following = continuations.replace_option(following, '--max-query-rows', args.max_query_rows * 2)
            effect = 'coverage'
        if empty:
            following = continuations.replace_option(following, '--max-bytes', max(args.max_bytes * 2, need or 0))
        return continuations.mark_stdin(continuations.encode(following, result.signature,
            'Increase output budget to read the next row' if empty else 'Read the next page', effect))
    for item in result.document.get('next') or []:
        continuations.mark_stdin(item)
    delivery.deliver(result.document, args, continuation=next_page if result.argv else None)
    return 0


def run_graph(args, argv, expected):
    store = Store(args.repo, args.cache)
    doc = graph_cli.show(store, args) if args.action == "show" else graph_cli.locate(args, store)
    if 'text' in doc:
        sys.stdout.write(doc['text'])
        return 0
    limited = doc.pop('incomplete')
    # Presentation (--fields, --verbose) may change on replay; the evidence rows may not.
    signature = digest(doc['results'])
    rows = doc['results']
    if not args.verbose and not args.fields:
        rows = [compact_row(r) for r in rows]
    if expected and signature != expected:
        raise ValueError("Continuation source changed; rerun the original request. Paging was not silently recomputed.")
    status = ('unknown' if doc['scope'].get('assessment') == 'unknown' else 'partial' if limited else 'ok')
    out = {"operation": doc['operation'], "status": status, "snapshots": doc['snapshots'],
           "scope": {**doc['scope'], "retention": "transient"}, "coverage": doc['coverage'], "results": rows,
           "gaps": {"count": len(doc['gaps']), "incomplete": len(limited), "query": "SELECT * FROM gaps"}}
    if 'primer' in doc:
        primer = doc['primer']
        if args.format == 'table' and isinstance(primer, dict):
            from .orientation import render as render_primer
            primer = render_primer(primer, args.primer_bytes)
        out['primer'] = primer
        # A requested primer that would crowd out every row says so instead of vanishing.
        if (isinstance(primer, (dict, list))
                and len(delivery.render({**out, "results": rows[:1]}, args, args.fields)) + delivery.NEXT_MARGIN > args.max_bytes):
            out['primer'] = f"omitted: byte cap; raise --max-bytes above {args.max_bytes}"
    # Later pages pin this capture; a newer capture supersedes it and the replay fails explicitly.
    base = list(argv) if argv and argv[0] == args.action else [args.action, *argv]
    base = continuations.replace_option(base, '--repo', store.root)
    base = continuations.replace_option(base, '--cache', str(store.path.parent))
    base = continuations.replace_option(base, '--snapshot', doc['snapshots'][0])

    def next_page(offset, empty, need=None):
        following = continuations.replace_option(base, '--offset', offset)
        if empty:
            following = continuations.replace_option(following, '--max-bytes', max(args.max_bytes * 2, need or 0))
        return continuations.encode(following, signature,
                                    'Increase output budget to read the next row' if empty else 'Read the next page')
    delivery.deliver(out, args, continuation=next_page)
    return 0


ROUTES = {"runtime": run_runtime, "describe": run_describe, "graph": run_graph}


def main(argv=None):
    incoming = list(sys.argv[1:] if argv is None else argv)
    argv = normalize_prefix(incoming)
    p = parser()
    try:
        args = normalize(p.parse_args(argv), argv)
        expected = None
        if args.action == 'show' and args.ref.startswith(continuations.PREFIX):
            args, argv, expected = resume(p, args, argv, incoming)
        return ROUTES[OPERATIONS[args.action].route](args, argv, expected)
    except (ValueError, OSError, RecursionError) as error:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "error", "results": [], "error": str(error)[:4000]}))
        return 2
    except KeyboardInterrupt:
        # Cancellation is not partial evidence: nothing was delivered or retained for this request.
        print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "error", "results": [], "error": "cancelled"}))
        return 130
