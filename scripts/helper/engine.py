"""Shared disposable query engine: in-memory standard-library SQLite over invocation-local rows.

Every input becomes a connection-local table. Nothing is written to disk; extension
loading and file functions are denied, and the connection closes before results return.
"""
import json
import math
import re
from dataclasses import dataclass, field

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REFERENCES = ("ref", "call_ref", "result_ref")
MAX_COLUMNS = 200
# SQLite heap ceiling. Process-wide and can only be lowered, so every query context sets the same value.
SQLITE_HEAP = 512 * 1024 * 1024


class EngineError(ValueError):
    """A query that cannot be answered within the disposable engine's bounds."""


@dataclass
class Input:
    """One named view: rows plus the coverage and schema supplied by its producer."""
    name: str | None
    rows: list
    upstream: dict = field(default_factory=dict)
    columns: list | None = None
    types: dict = field(default_factory=dict)
    grain: str | None = None


TYPE_ALIASES = {"BIGINT": "int", "INTEGER": "int", "INT": "int", "SMALLINT": "int",
                "DOUBLE": "float", "REAL": "float", "FLOAT": "float",
                "VARCHAR": "str", "TEXT": "str", "STRING": "str", "JSON": "str",
                "BOOLEAN": "bool", "BOOL": "bool"}


def columns(records, hint=None):
    """Union of declared and observed keys, in stable order; reject non-object rows.

    A hint keeps a known schema when a selection is empty, so a query over zero
    matched rows still binds its columns instead of failing.
    """
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise EngineError("Every input row must be an object")
    names = {key for row in records for key in row if isinstance(key, str)}
    if hint is not None:
        if not isinstance(hint, (list, tuple)) or any(not isinstance(key, str) for key in hint):
            raise EngineError("Declared columns must be field names")
        names.update(hint)
    names = sorted(names)
    if "_row" in names:
        raise EngineError("_row is reserved for the invocation-local ordinal")
    if len(names) > MAX_COLUMNS:
        raise EngineError(f"Maximum {MAX_COLUMNS} columns per input")
    return names


def infer_type(values):
    """Public column inference shared by declared schemas and summaries."""
    return _kind(values)


def _kind(values):
    """One column type; mixed types widen instead of failing.

    Numbers widen to float, anything with text or structured values becomes a
    string, and objects/arrays are encoded as bounded JSON strings.
    """
    kinds = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            kinds.add("bool")
        elif isinstance(value, int):
            kinds.add("int")
        elif isinstance(value, float):
            kinds.add("float")
        elif isinstance(value, str):
            kinds.add("str")
        else:
            kinds.add("other")
    if not kinds:
        return "str"
    if kinds <= {"bool"}:
        return "bool"
    if kinds <= {"bool", "int"}:
        return "int" if all(-(2 ** 63) <= v < 2 ** 63 for v in values if isinstance(v, int)) else "str"
    if kinds <= {"bool", "int", "float"}:
        return "float"
    return "str"


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"encoding": "hex", "value": bytes(value).hex()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class SQLiteQuery:
    """One disposable SQL context for planning and execution.

    SQLite's compiler/authorizer resolves tables and columns, including CTE scope.
    Planning uses empty declared tables; providers are loaded only after validation.
    No lexical SQL dependency guesses and no external access or durable databases.
    """
    def __init__(self, schemas, seconds=10.0):
        import sqlite3
        import time
        self.sqlite3, self.time = sqlite3, time
        if seconds <= 0:
            raise EngineError("Query deadline must be positive")
        self.deadline = time.monotonic() + seconds
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(f"PRAGMA hard_heap_limit={SQLITE_HEAP}")
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4 * 1024 * 1024)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 128 * 1024)
        self.connection.execute("PRAGMA temp_store=MEMORY")
        self.schemas = schemas
        self.reads = {}
        self.inputs = {}
        self.sql, self.params = None, None
        try:
            for name, schema in schemas.items():
                if not IDENTIFIER.fullmatch(name) or not schema:
                    raise EngineError(f"Invalid or empty table schema: {name}")
                declared = []
                for column, kind in schema.items():
                    if not isinstance(column, str) or not column or '\x00' in column:
                        raise EngineError("Column names must be nonempty strings without NUL")
                    value = TYPE_ALIASES.get(str(kind).upper())
                    if value is None:
                        raise EngineError(f"Unknown column type: {kind}")
                    sql_type = {"int": "INTEGER", "bool": "INTEGER", "float": "REAL", "str": "TEXT"}[value]
                    declared.append('"' + column.replace('"', '""') + '" ' + sql_type)
                self.connection.execute(f'CREATE TABLE "{name}" (' + ','.join(declared) + ')')
            self.rootpages = {page: name for name, page in self.connection.execute(
                "SELECT name, rootpage FROM sqlite_master WHERE type='table'")}
            self.connection.set_progress_handler(lambda: int(time.monotonic() > self.deadline), 1000)
        except Exception:
            self.close()
            raise

    def _authorize(self, action, arg1, arg2, database, trigger):
        s = self.sqlite3
        if action == s.SQLITE_READ:
            if arg1 not in self.schemas:
                # COUNT(*) over a CTE reads the CTE by name with no column; SQLite's own tables stay denied.
                return s.SQLITE_OK if arg2 == "" and not (arg1 or "").lower().startswith("sqlite_") else s.SQLITE_DENY
            self.reads.setdefault(arg1, set()).add(arg2)
        if action == s.SQLITE_FUNCTION and (arg2 or "").lower() in ("load_extension", "readfile", "writefile"):
            return s.SQLITE_DENY
        return s.SQLITE_OK if action in (s.SQLITE_SELECT, s.SQLITE_READ, s.SQLITE_FUNCTION, s.SQLITE_RECURSIVE) else s.SQLITE_DENY

    def plan(self, sql, params=None):
        if not isinstance(sql, str) or not sql.strip():
            raise EngineError("Supply one read-only SELECT or WITH query")
        self.sql, self.params = sql, params or {}
        self.reads = {}
        self.connection.set_authorizer(self._authorize)
        try:
            program = self.connection.execute("EXPLAIN " + sql, self.params).fetchall()
            # SQLite omits SQLITE_READ callbacks for some COUNT(*)/USING joins (the right
            # side's USING column). Its compiled OpenRead/Column bytecode still identifies
            # the exact base tables and columns; root pages are known because this private
            # schema has no indexes/views.
            cursors = {}
            for _, opcode, p1, p2, *_ in program:
                if opcode == "OpenRead" and p2 in self.rootpages:
                    cursors[p1] = self.rootpages[p2]
                    self.reads.setdefault(cursors[p1], set())
                elif opcode == "Column" and p1 in cursors:
                    names = list(self.schemas[cursors[p1]])
                    if 0 <= p2 < len(names):
                        self.reads[cursors[p1]].add(names[p2])
        except self.sqlite3.Error as error:
            raise EngineError(f"Read-only query failed: {error}") from None
        finally:
            self.connection.set_authorizer(None)
        # An empty column name is SQLite's rowid/table-only read (e.g. COUNT(*)).
        return {name: sorted(f for f in fields if f) for name, fields in self.reads.items()}

    def load(self, item):
        if item.name not in self.schemas:
            raise EngineError(f"Undeclared table: {item.name}")
        if item.name in self.inputs:
            raise EngineError(f"Table already loaded: {item.name}")
        schema = self.schemas[item.name]
        columns(item.rows, [n for n in schema if n != "_row"])
        names = list(schema)
        ordinal = iter(range(len(item.rows)))
        def record(row):
            if self.time.monotonic() > self.deadline:
                raise EngineError("Query/provider deadline exceeded; narrow the query or use --budget deep")
            values = []
            for name in names:
                value = next(ordinal) if name == "_row" else row.get(name)
                if isinstance(value, bool) and TYPE_ALIASES.get(str(schema[name]).upper()) == "str":
                    value = "true" if value else "false"  # SQLite would store 1/0 in a text column.
                elif isinstance(value, (dict, list, tuple)):
                    value = json.dumps(value, ensure_ascii=True, default=_text, separators=(",", ":"))
                elif isinstance(value, int) and not -(2**63) <= value < 2**63:
                    value = str(value)
                values.append(value)
            return values
        try:
            self.connection.executemany(f'INSERT INTO "{item.name}" VALUES (' + ','.join('?' for _ in names) + ')',
                                        (record(row) for row in item.rows))
            self.connection.commit()
        except self.sqlite3.Error as error:
            raise EngineError(f"Input table {item.name}: {error}") from None
        except MemoryError:
            raise EngineError(f"Input table {item.name} exceeded the {SQLITE_HEAP >> 20} MiB SQLite memory cap") from None
        self.inputs[item.name] = item

    def evaluate(self, limit=1000):
        if not isinstance(limit, int) or limit <= 0:
            raise EngineError("Row limit must be positive")
        if self.time.monotonic() > self.deadline:
            raise EngineError("Query/provider deadline exceeded; narrow the query or use --budget deep")
        self.connection.set_authorizer(self._authorize)
        try:
            cursor = self.connection.execute(self.sql, self.params)
            if cursor.description is None:
                raise EngineError("Supply one read-only SELECT or WITH query")
            names = [column[0] for column in cursor.description]
            if len(set(names)) != len(names):
                raise EngineError("Alias duplicate output column names")
            fetched = cursor.fetchmany(limit + 1)
            total = len(fetched)
            if total > limit:
                # Count the rest under the same deadline so a capped page states the full size.
                while chunk := cursor.fetchmany(10000):
                    total += len(chunk)
        except self.sqlite3.OperationalError as error:
            if str(error) != "interrupted":
                raise EngineError(f"Read-only query failed: {error}") from None
            if self.time.monotonic() <= self.deadline:
                raise KeyboardInterrupt from None  # Ctrl-C inside the progress handler, not the deadline.
            raise EngineError("Read-only query failed: interrupted at the query deadline; aggregate, narrow "
                              "or raise --seconds") from None
        except self.sqlite3.Error as error:
            raise EngineError(f"Read-only query failed: {error}") from None
        except MemoryError:
            raise EngineError(f"Query exceeded the {SQLITE_HEAP >> 20} MiB SQLite memory cap; aggregate or narrow it") from None
        finally:
            self.connection.set_authorizer(None)
        rows = []
        for record in fetched[:limit]:
            row = {}
            for name, value in zip(names, record):
                value = _jsonable(value)
                if name in REFERENCES and isinstance(value, str):
                    try:
                        decoded = json.loads(value)
                        if isinstance(decoded, (dict, list)):
                            value = decoded
                    except ValueError:
                        pass
                row[name] = value
            rows.append(row)
        return {"results": rows, "query_complete": len(fetched) <= limit, "total": total,
                "scope": {"engine": "sqlite", "query": self.sql,
                          "inputs": [{"view": name, "rows": len(item.rows), "grain": item.grain,
                                      "upstream": item.upstream} for name, item in self.inputs.items()]}}

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
