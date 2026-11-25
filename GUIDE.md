# Python driver guide

Task guides complement the [README](README.md), [API reference](API_REFERENCE.md),
and [query language reference](QUERY_LANGUAGE.md). Each Python block is a
complete program with public imports, setup, and cleanup. Run blocks separately.
File examples create temporary databases; use your application's path when
retaining data. Paths must be NUL-free strings; convert `pathlib.Path` with `str()`.

Core query, record, and IPC examples need only Velr. Optional Arrow, pandas,
Polars, and NumPy examples name their additional requirements in their sections.

Import public driver types and exceptions directly from `velr`; the existing
`velr.driver` imports shown in this guide remain supported. `import velr` and
imports of `velr.guide` and `velr.query_language` leave the native runtime unloaded.
It loads when a public driver attribute is requested, such as in
`from velr import Velr, QueryError`.

## Graph analytics

Install the GDS add-on with `python -m pip install 'velr[gds]'`. It installs
the binary GDS package for the same Velr release. Import `Gds` from `velr_gds`
and open a connection with `Velr.open(extensions=[Gds()])`. Pass your database
path as the first argument for a file-backed graph.

1. On the open connection, use `db.run("CALL gds.graph.project('people', 'Person', 'KNOWS')")`
   to project the `Person` nodes and their `KNOWS` relationships.
2. Call `db.exec_one("CALL gds.wcc.stream('people') YIELD nodeId, componentId RETURN nodeId, componentId")`
   to get the connected components. Use a context manager for the result table
   and collect small results with `table.to_records()`. For large results,
   use `table.iter_rows()` to consume rows incrementally.
3. Reuse the connection for further algorithms on the projection. Release it
   with `db.run("CALL gds.graph.drop('people')")` or close the connection.
   Run projection and drop outside explicit database transactions.

GDS is enabled per connection through `Velr.open()`. Each enabled connection
has its own named graphs and a 512 MiB budget for managed GDS allocations. Pass
`Gds(memory_limit_bytes=1024 * 1024 * 1024)` for a 1 GiB budget.
The [Velr GDS package guide](https://pypi.org/project/velr-gds/) includes a
complete program, projection options, algorithm arguments, and result columns.

## Query parameter binding

Use `$name` in Cypher and omit `$` from `params` keys. Values are bound as data,
not interpolated query text. Supported values are `None`, booleans, signed
64-bit integers, finite floats, strings, lists/tuples, and dictionaries with
string keys. Python integers outside the signed 64-bit range are invalid inputs.

```python
from velr.driver import Velr

with Velr.open() as db:
    db.run(
        "UNWIND $people AS person CREATE (:Person {name:person.name, age:person.age})",
        params={"people": [{"name": "Alice", "age": 42}, {"name": "Bob", "age": 17}]},
    )
    with db.exec_one(
        "MATCH (p:Person) WHERE p.age >= $minimum RETURN p.name AS name ORDER BY name",
        params={"minimum": 18},
    ) as table:
        records = table.to_records()
    assert records == [{"name": "Alice"}]
    print(records)
```

Invalid Python values can raise `TypeError` or `ValueError`. Missing query
parameters raise `VelrError` with a query diagnostic. Use `exec_one()` when
passing parameters to result-conversion methods; connection shortcuts such as
`to_records(cypher)` accept query text and optional JSON-column selection,
not query parameter or timeout keywords.

## Query execution

| Task | API | Cleanup |
|---|---|---|
| Writes, DDL, or scripts whose rows are unnecessary | `run()` / `execute()` | Results close automatically |
| Exactly one result table | `exec_one()` | Close the table or use `with` |
| Multiple result tables | `exec()` and `iter_tables()` | Close tables and stream |
| Owned Python results | `Table.to_rows()` / `to_records()` | Close the source table |
| Convert the final table of a script | `Velr.to_rows()` / `to_records()` | Query handles close automatically |

`exec_one()` rejects zero or multiple result tables. Connection conversion
shortcuts return the last table, or an empty result when no table is produced;
previous statements still execute. Prefer one table and unique `RETURN` aliases.
Cypher rejects conflicting result names before creating a table.

```python
from velr.driver import Velr

with Velr.open() as db:
    db.execute("CREATE (:Person {name:$name, age:$age})", params={"name": "Alice", "age": 42})
    with db.exec_one("MATCH (p:Person) RETURN p.name AS name, p.age AS age") as table:
        columns = table.column_names()
        rows = table.to_rows()
    assert columns == ["name", "age"]
    assert rows == [["Alice", 42]]
    assert db.to_records("RETURN 'first' AS value; RETURN 'last' AS value") == [{"value": "last"}]
    print(columns, rows)
```

## Streaming multiple result tables

Consume each result before requesting the next. A table from `exec()` requires
its producing stream to remain open. Context managers close handles on success,
failure, and early exit.

```python
from velr.driver import Velr

with Velr.open() as db:
    results = []
    with db.exec(
        "RETURN $first AS value; RETURN $second AS value",
        params={"first": "first", "second": "second"},
    ) as stream:
        for table in stream.iter_tables():
            with table:
                results.append(table.to_records())
    assert results == [[{"value": "first"}], [{"value": "second"}]]
    print(results)
```

## Bounded result previews

`max_result_rows` caps rows independently for each table. Zero preserves column
metadata without rows. Existing Cypher `LIMIT` clauses still apply. A row cap
does not prevent writes or limit execution time; combine it with a deadline
when needed.

```python
from velr.driver import Velr

with Velr.open() as db:
    with db.exec_one("UNWIND range(1, 100) AS n RETURN n", max_result_rows=2) as table:
        sample = table.to_rows()
    with db.exec_one("RETURN 'Alice' AS name", max_result_rows=0) as table:
        columns, empty = table.column_names(), table.to_rows()
    assert sample == [[1], [2]]
    assert columns == ["name"] and empty == []
    print(sample, columns, empty)
```

## Query cancellation and timeouts

Pass `timeout` in seconds to `run()`, `execute()`, `exec()`, or `exec_one()`.
Connection and transaction queries share these controls. A finite nonnegative
timeout covers execution and all later table/row reads. `None` means no deadline;
`0` expires immediately. Catch `VelrError` around both execution and fetching.

```python
from velr.driver import Velr, VelrError

with Velr.open() as db:
    with db.exec_one("RETURN 1 AS n", timeout=5.0) as table:
        assert table.to_rows() == [[1]]
    try:
        with db.exec_one("RETURN 1 AS n", timeout=0) as table:
            table.to_rows()
    except VelrError as error:
        assert error.code == "query.deadline_exceeded"
        print(error.code)
    else:
        raise AssertionError("A zero timeout must expire immediately")
```

### Cancellation during fetching

Pass a `CancellationToken` as `cancellation` when starting the query. Call
`cancel()` from the query thread or another request/UI thread. Keep connections
and result handles on their owning thread. A token stays cancelled and signals
all queries using it; create a fresh token for independent work.

```python
from threading import Thread
from velr.driver import CancellationToken, Velr, VelrError

stop = CancellationToken()
with Velr.open() as db:
    try:
        with db.exec_one(
            "UNWIND range(1, 100) AS n RETURN n", cancellation=stop, timeout=30.0,
        ) as table, table.iter_rows() as rows:
            assert next(rows)[0].as_python() == 1
            requester = Thread(target=stop.cancel)
            requester.start()
            requester.join()
            next(rows)
    except VelrError as error:
        assert error.code == "query.cancelled"
        print(error.code)
    else:
        raise AssertionError("Fetching after cancellation must stop")
```

Stops use `error.code` values `query.cancelled` and `query.deadline_exceeded`.
Fast operations can complete before a stop request. Keep cancellation controls
available until fetching finishes and close results on completion or failure.
If interruption ends an explicit transaction, close its handle before starting
a new transaction.

## Results, lifetimes, and threads

`Cell.as_python()` converts nulls, booleans, integers, and doubles to `None`,
`bool`, `int`, and `float`. TEXT becomes `str`. JSON cells are strings by default;
set `parse_json=True` to decode their lists/maps into Python objects. Python's
JSON decoder retains integer precision. `decode_text=False` returns copied
bytes for TEXT and unparsed JSON cells; `parse_json=True` still decodes JSON
into Python objects. `encoding` and `errors` control text decoding.

`to_rows()` returns owned row lists and `to_records()` returns dictionaries.
They decode Cypher lists and maps by default. Both collect all rows in memory;
use `iter_rows()` or a visitor for large results. Text and JSON payloads in
`Cell` objects are copied and remain usable after the next fetch or after all
result handles close.

```python
from velr.driver import Velr

value = 9007199254740993
with Velr.open() as db:
    with db.exec_one(
        "RETURN $id AS id, $items AS items",
        params={"id": value, "items": [{"id": value}]},
    ) as table, table.iter_rows() as rows:
        cells = next(rows)
assert cells[0].as_python() == value
assert isinstance(cells[1].as_python(), str)
assert isinstance(cells[1].as_python(decode_text=False), bytes)
assert cells[1].as_python(parse_json=True) == [{"id": value}]
print(cells[0].as_python(), cells[1].as_python(parse_json=True))
```

Tables from `exec_one()` belong to their connection or transaction; those from
`exec()` also require the stream. Closing a parent closes its remaining result
handles. Keep parents open while reading. `for_each_row(fn)` and `collect(map_fn)`
run synchronous callbacks; returning a coroutine does not make them asynchronous.
A row-callback exception stops iteration, closes its temporary cursor, and
propagates unchanged. Embedding callback failures instead surface as `VelrError`.

### Graph values and paths

Nodes and relationships are returned as dictionaries by `to_rows()` and
`to_records()`. Paths become `velr.driver.Path`, whose `elements` tuple
alternates node and relationship dictionaries, beginning and ending with a node.
A zero-length path contains one node. These owned values outlive their handles.

```python
from velr.driver import Path, Velr

with Velr.open() as db:
    db.run("CREATE (:Person {name:'Alice'})-[:KNOWS {since:2020}]->(:Person {name:'Grace'})")
    with db.exec_one(
        "MATCH p=(a:Person {name:'Alice'})-[:KNOWS]->(b) RETURN p AS path",
    ) as table:
        path = table.to_records()[0]["path"]
assert isinstance(path, Path)
assert len(path.elements) == 3
assert path.elements[0]["properties"]["name"] == "Alice"
assert path.elements[2]["properties"]["name"] == "Grace"
print([element["properties"] for element in path.elements])
```

If mixed graph values need a type discriminator, project one alongside the
value. Use application properties for identifiers that must remain distinct
across entity kinds.

### Decode JSON stored in text properties

Ordinary Cypher strings stay strings. `columns=` on `to_rows()` or
`to_records()` explicitly selects projected TEXT columns containing JSON to
decode; it does not select which columns are returned. It accepts a name or a
sequence of names/zero-based indexes. Unknown columns and malformed selected
JSON raise validation/decoding errors.

```python
import json
from velr.driver import Velr

with Velr.open() as db:
    db.run(
        "CREATE (:Document {id:$id, metadata:$metadata})",
        params={"id": "doc-1", "metadata": json.dumps({"tags": ["graph"]})},
    )
    with db.exec_one("MATCH (d:Document) RETURN d.id AS id, d.metadata AS metadata") as table:
        plain = table.to_records()
        decoded = table.to_records(columns=("metadata",))
    assert isinstance(plain[0]["metadata"], str)
    assert decoded == [{"id": "doc-1", "metadata": {"tags": ["graph"]}}]
    print(decoded)
```

### Threads and errors

Operations are synchronous. An idle connection can move to another thread only
after all transactions, results, savepoints, and explain traces have finished.
Do not use one connection concurrently. Keep active child handles on the thread
that created them. Separate connections are the normal choice for parallel work:

```python
from concurrent.futures import ThreadPoolExecutor
from velr.driver import Velr


def read_person(name: str) -> list[dict]:
    with Velr.open() as db:
        db.run("CREATE (:Person {name:$name})", params={"name": name})
        with db.exec_one("MATCH (p:Person) RETURN p.name AS name") as table:
            return table.to_records()


with ThreadPoolExecutor(max_workers=2) as workers:
    results = list(workers.map(read_person, ["Alice", "Grace"]))
assert results == [[{"name": "Alice"}], [{"name": "Grace"}]]
print(results)
```

Database/query and invalid-handle operations raise `VelrError` or one of its
subclasses. `QueryError` covers Cypher compilation and runtime failures, including
`CypherSyntaxError`, `CypherSemanticError`, `CypherTypeError`, `ParameterError`,
`ProcedureNotFoundError`, `EntityNotFoundError`, `ConstraintError`, and
`CypherArithmeticError`. Catch failures while starting queries, advancing streams,
and reading rows. `HandleStateError` covers closed, wrong-thread, and in-use
handles; ordinary exhausted row iterators still raise `StopIteration`.

Use `category` and `detail` to branch on stable runtime reasons without inspecting
diagnostic wording. `phase` is `"compile"`, `"runtime"`, or `None`; runtime errors
can occur before fetching starts. `explanation` aliases the existing concise
`message`; `report` is the complete rendered diagnostic returned by `str(error)`.
`hint` is optional remediation. `position`, when known, is an `ErrorPosition`
named tuple with integer `.line` and `.column` fields in the complete submitted
query. Both coordinates are one-based, with Unicode display-cell columns.
Tuple indexing, unpacking, and comparison with `(line, column)` still work.
Convert these coordinates when an editor uses character, UTF-16, or byte offsets;
wide and combining characters can have different display widths. An unknown
position is `None`, as are other unavailable optional fields.

```python
from velr import ConstraintError, ErrorPosition, QueryError, Velr, VelrError

with Velr.open() as db:
    db.run("CREATE (:Person {name: 'Alice'})-[:KNOWS]->(:Person {name: 'Bob'})")
    try:
        db.run("MATCH (p:Person {name: 'Alice'}) DELETE p")
    except ConstraintError as error:
        print(error.explanation)
        position: ErrorPosition | None = error.position
        if position is not None:
            print(position.line, position.column)
            line, column = position
            assert position == (line, column)
        print(error.report)
    except QueryError as error:
        print(error.category, error.detail, error.explanation)
    except VelrError as error:
        print(error.report)
```

`DatabaseBusyError` describes database contention. Its `retryable` attribute only
identifies a potentially transient condition; retrying a partially completed
script, write, or transaction is a separate application decision. Inspect the
SQLite extended `sqlite_errorcode` and optional `sqlite_errorname` when available.
`ReadOnlyError` requires a writable connection; `MigrationRequiredError` requires
explicit migration. Local handle guards have `status_code is None`; native errors
expose their numeric C ABI status through `status_code`.

Filesystem failures can include `io_kind`, `os_code`, `operation`, and `path`.
When a native filesystem failure supplies an original platform error number,
its corresponding Python `OSError` remains available through `__cause__`.

Catch `QueryCancelled` or `DeadlineExceeded` for query stops. Their existing `code`
values remain `query.cancelled` and `query.deadline_exceeded`; `DeadlineExceeded`
also inherits Python's `TimeoutError`. Older runtimes without structured diagnostics
still expose native statuses and stop subclasses, and other failures remain
`VelrError` with unavailable metadata instead of inferred classifications.

Invalid Python arguments, missing optional dependencies, JSON/text decoding, and
file operations can raise standard Python exceptions. Row callback exceptions
propagate unchanged. Embedding callback failures are reported as `VelrError` with
their original Python exception retained in `__cause__`.

## Transactions and savepoints

Only one transaction can be active per connection. Writable `begin_tx()`
reserves the writer slot when it starts. Prepare external inputs first and keep
transactions short. For a read-only snapshot of an existing file, use
`open_readonly()` and `begin_tx()`.

A transaction context rolls back on exit unless explicitly committed, including
on a normal exit. `commit()` and `rollback()` finish the transaction and consume
its handle. Close results and scoped savepoints before finalizing it. Do not
interleave unrelated connection operations with the transaction.

### Scoped savepoints

A scoped savepoint context releases on success and rolls back/releases on an
exception. Calling `rollback()` explicitly consumes it. Closing an active
anonymous handle also rolls back and releases; do not rely on garbage collection
for timely cleanup. Reusing a consumed savepoint with `rollback()` is an error;
`close()` is idempotent.

```python
from velr.driver import Velr

with Velr.open() as db:
    with db.begin_tx() as tx:
        tx.run("CREATE (:Person {name:$name})", params={"name": "Alice"})
        with tx.savepoint() as savepoint:
            tx.run("CREATE (:Person {name:'Temporary'})")
            savepoint.rollback()
        with tx.savepoint():
            tx.run("CREATE (:Person {name:$name})", params={"name": "Grace"})
        tx.commit()
    records = db.to_records("MATCH (p:Person) RETURN p.name AS name ORDER BY name")
assert records == [{"name": "Alice"}, {"name": "Grace"}]
print(records)
```

### Named savepoints

Named savepoints belong to the transaction, with unique names while active.
Closing a named Python handle leaves its savepoint active. `rollback_to(name)`
discards newer named savepoints and keeps its target; `release_savepoint(name)`
releases the newest active named savepoint. The handle's `release()` or
`rollback()` consumes that named savepoint. Its context manager releases on
normal exit and rolls back/releases on an exception. Commit releases surviving
named savepoints and retains their surviving changes.

```python
from velr.driver import Velr

with Velr.open() as db:
    with db.begin_tx() as tx:
        tx.run("CREATE (:Person {name:'Alice'})")
        tx.savepoint_named("before_import").close()
        tx.run("CREATE (:Person {name:'Temporary'})")
        tx.savepoint_named("newer").close()
        tx.run("CREATE (:Person {name:'Also temporary'})")
        tx.rollback_to("before_import")
        tx.run("CREATE (:Person {name:'Grace'})")
        tx.release_savepoint("before_import")
        tx.commit()
    records = db.to_records("MATCH (p:Person) RETURN p.name AS name ORDER BY name")
assert records == [{"name": "Alice"}, {"name": "Grace"}]
print(records)
```

## Quantified path patterns

Bind the starting stop name as the `origin` query parameter. The example
follows one to three consecutive `NEXT` relationships, checking the arrival
node at every repetition.

```cypher
MATCH (origin:Stop {name: $origin})
OPTIONAL MATCH (origin) ((departure)-[:NEXT]->(arrival WHERE arrival.open = true)){1,3} (destination)
RETURN [stop IN arrival | stop.name] AS stops, destination.name AS destination
ORDER BY size(stops)
```

The `arrival` group contains the arrival nodes in traversal order.
`OPTIONAL MATCH` returns null for the group and destination when no path
qualifies. Use a zero lower bound such as `{0,3}` when a match at the starting
stop should return an empty group. See [quantified path patterns](QUERY_LANGUAGE.md#quantified-path-patterns)
for bound and predicate syntax.

## SEARCH clause

`SEARCH` requires a Velr 0.2.62+ runtime with the corresponding search features
and a file-backed database. Use it inside `MATCH` or `OPTIONAL MATCH`, bind the
node/relationship in the pattern, and provide a mandatory candidate `LIMIT`.
`SCORE AS alias` exposes scores; use `ORDER BY alias DESC` for ranked output.
Driver row caps and candidate limits control different parts of a query.

Vector index names are identifiers; backticks allow spaces. Fulltext names can
also be string parameters. Query values can reference earlier variables but
not the searched element. `LIMIT` accepts integers in `0..2147483647`; fulltext
`SKIP`/`OFFSET` accepts `0..9223372036854775807` before `LIMIT`. Pagination uses
static expressions or parameters, not variables bound by earlier rows.

### Candidate filters and score filters

Declare vector filter properties with index DDL `WITH [n.property]`. An inner
`WHERE` filters before nearest-neighbor selection. An enclosing `WHERE`, before
or after `SEARCH`, filters selected candidates and can leave fewer rows than the
limit. Inner filters allow `=`, `<`, `<=`, `>`, `>=`, `IN`, boolean/null property
tests joined by `AND`. Each property permits one lower plus one upper bound,
or one equality, `IN`, or null test. Use the enclosing `WHERE` for other predicates.

```python
from math import isclose
from pathlib import Path
from tempfile import TemporaryDirectory
from velr.driver import Velr

with TemporaryDirectory(prefix="velr-filtered-search-") as directory:
    with Velr.open(str(Path(directory) / "graph.db")) as db:
        db.run(
            "UNWIND $chunks AS c CREATE (:Chunk {text:c.text, category:c.category, embedding:c.embedding})",
            params={"chunks": [
                {"text": "Graph guide", "category": "docs", "embedding": [1.0, 0.0]},
                {"text": "Other material", "category": "other", "embedding": [0.0, 1.0]},
            ]},
        )
        db.run("CREATE VECTOR INDEX filteredChunks FOR (n:Chunk) ON (n.embedding) "
               "WITH [n.category] OPTIONS {indexConfig:{dimensions:2, metric:'cosine'}}")
        with db.exec_one(
            "MATCH (chunk) SEARCH chunk IN (VECTOR INDEX filteredChunks FOR $vector "
            "WHERE chunk.category = $category LIMIT $limit) SCORE AS score "
            "WHERE score >= $minimum RETURN chunk.text AS text, score ORDER BY score DESC",
            params={"vector": [1.0, 0.0], "category": "docs", "limit": 10, "minimum": 0.5},
        ) as table:
            records = table.to_records()
    assert [record["text"] for record in records] == ["Graph guide"], records
    assert 0.0 <= records[0]["score"] <= 1.0, records
    assert isclose(records[0]["score"], 1.0, rel_tol=1e-6, abs_tol=1e-6), records
    print(records)
```

Cosine `SEARCH` and vector procedure scores use `(1 + cosine) / 2` in `0..1`.
Convert an older raw cosine procedure threshold `t` to `(1 + t) / 2`. For
Euclidean/L2, `SEARCH` returns `1 / (1 + squaredDistance)` and the procedures
return `-squaredDistance`. Dot-product scores are unbounded.
Fulltext scores are query-specific and unnormalized in both forms.

Scores use floating-point arithmetic. Compare computed scores with a tolerance
rather than exact equality.

### Fulltext analyzers and pagination

Fulltext accepts `WITH ANALYZER` and inner `SKIP`/`OFFSET`; it has no inner
`WHERE`. An outer filter applies after the selected page. Analyzer names are
`standard` (default; aliases `standard-no-stop-words` and `default`), `keyword`,
`whitespace`, `simple`, and `english`. Analyzer expressions can refer to earlier
query variables. Vector SEARCH does not accept an analyzer or inner pagination.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from velr.driver import Velr

with TemporaryDirectory(prefix="velr-paged-search-") as directory:
    with Velr.open(str(Path(directory) / "graph.db")) as db:
        db.run("UNWIND $titles AS title CREATE (:Paper {title:title})",
               params={"titles": ["Graph databases", "Graph search", "Graph theory"]})
        db.run("CREATE FULLTEXT INDEX paperText FOR (n:Paper) ON EACH [n.title]")
        with db.exec_one(
            "MATCH (paper) SEARCH paper IN (FULLTEXT INDEX $index FOR $text "
            "WITH ANALYZER $analyzer OFFSET $offset LIMIT $limit) SCORE AS relevance "
            "RETURN paper.title AS title, relevance ORDER BY relevance DESC, title",
            params={"index": "paperText", "text": "graph", "analyzer": "standard", "offset": 1, "limit": 2},
        ) as table:
            records = table.to_records()
    assert len(records) == 2 and all(record["relevance"] > 0 for record in records)
    print(len(records))
```

### Subqueries and relationship indexes

A search pattern contains exactly one node or one relationship hop. Only the
searched element can be named or constrained. Multi-pattern matches, path
bindings, shortest paths, and variable-length hops are unsupported in a search
pattern. Use `MATCH ()-[r]->()` and `SEARCH r` for relationship indexes.
Null queries and zero limits produce no matches; `OPTIONAL MATCH` preserves an
unmatched input with null entity and score values.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from velr.driver import Velr

with TemporaryDirectory(prefix="velr-search-subqueries-") as directory:
    with Velr.open(str(Path(directory) / "graph.db")) as db:
        db.run("CREATE (:Paper {title:'Graph databases'})-[:CITES {note:'graph reference'}]->(:Paper {title:'Other topic'})")
        db.run("CREATE FULLTEXT INDEX paperText FOR (n:Paper) ON EACH [n.title]")
        db.run("CREATE FULLTEXT INDEX citationText FOR ()-[r:CITES]-() ON EACH [r.note]")
        with db.exec_one(
            "UNWIND $terms AS term RETURN term, COUNT { "
            "MATCH (paper) SEARCH paper IN (FULLTEXT INDEX paperText FOR term LIMIT 10) "
            "} AS matches ORDER BY term", params={"terms": ["graph", "missing"]},
        ) as table:
            counts = table.to_records()
        with db.exec_one(
            "MATCH ()-[r]->() SEARCH r IN (FULLTEXT INDEX citationText FOR $text LIMIT 1) "
            "RETURN r.note AS note", params={"text": "graph"},
        ) as table:
            relationships = table.to_records()
        with db.exec_one(
            "OPTIONAL MATCH (paper) SEARCH paper IN (FULLTEXT INDEX paperText FOR null LIMIT 1) "
            "SCORE AS score RETURN paper.title AS title, score",
        ) as table:
            optional = table.to_records()
    assert counts == [{"term": "graph", "matches": 1}, {"term": "missing", "matches": 0}]
    assert relationships == [{"note": "graph reference"}]
    assert optional == [{"title": None, "score": None}]
    print(counts, relationships, optional)
```

`EXISTS { ... }` supports SEARCH too. `COUNT { OPTIONAL MATCH ... }` counts
preserved optional rows, including a null row for an unmatched input.

## Fulltext search

Create fulltext indexes over text properties in a file-backed database. Writes
keep them current. Search text supports terms, quoted phrases, field scopes
such as `title:graph`, boolean grouping, required/excluded terms, phrase slop,
term/phrase prefixes, boosts, and `*` to match all indexed entities. Adjacent
terms use `OR`; a field scope applies only to the next term or phrase. Bind text
containing apostrophes as a parameter. `?` wildcards and term-level fuzzy matching
are unsupported. See the [fulltext grammar](QUERY_LANGUAGE.md#fulltext-search).

The node and relationship `CALL` procedures remain supported. They default to
at most 1,000 matches; pass a third options argument to change that cap.
`WHERE` after `YIELD` filters selected candidates. Scores rank results within
one query; do not compare them across queries.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from velr.driver import Velr

with TemporaryDirectory(prefix="velr-fulltext-") as directory:
    with Velr.open(str(Path(directory) / "graph.db")) as db:
        db.run("CREATE (:Paper {title:$title, abstract:$abstract})",
               params={"title": "Graph databases", "abstract": "Connected data and vector search"})
        db.run("CREATE FULLTEXT INDEX paperText FOR (n:Paper) ON EACH [n.title, n.abstract]")
        with db.exec_one(
            "CALL db.index.fulltext.queryNodes('paperText', $text, {limit: 10}) "
            "YIELD node, score RETURN node.title AS title, score ORDER BY score DESC",
            params={"text": "title:graph AND abstract:vector"},
        ) as table:
            records = table.to_records()
    assert [record["title"] for record in records] == ["Graph databases"]
    assert records[0]["score"] > 0
    print([record["title"] for record in records])
```

Writable opens repair missing or corrupt fulltext index data. Read-only opens
never repair indexes.

## Vector search and embeddings

### Stored-vector indexes and score scales

Store precomputed numeric vector/list properties and index them with
`CREATE VECTOR INDEX ... ON (n.embedding)`. Stored and query vectors must match
the index's dimensions. Cosine queries require a nonzero component. The scalar
`vector.similarity.cosine(left, right)` needs no index and returns `0..1`,
including `0.5` for orthogonal vectors.

```python
from math import isclose
from pathlib import Path
from tempfile import TemporaryDirectory
from velr.driver import Velr

with TemporaryDirectory(prefix="velr-vector-scores-") as directory:
    with Velr.open(str(Path(directory) / "graph.db")) as db:
        db.run("CREATE (:Chunk {text:$text, embedding:$embedding})",
               params={"text": "Connected data", "embedding": [1.0, 0.0]})
        db.run("CREATE VECTOR INDEX chunkEmbedding FOR (n:Chunk) ON (n.embedding) "
               "OPTIONS {indexConfig:{dimensions:2, metric:'cosine'}}")
        params = {"vector": [0.0, 1.0], "stored": [1.0, 0.0], "limit": 1}
        queries = {
            "search": "MATCH (node) SEARCH node IN (VECTOR INDEX chunkEmbedding FOR $vector LIMIT $limit) SCORE AS score RETURN score",
            "procedure": "CALL db.index.vector.queryNodes('chunkEmbedding', $limit, $vector) YIELD score RETURN score",
            "exact": "RETURN vector.similarity.cosine($stored, $vector) AS score",
        }
        scores = {}
        for name, query in queries.items():
            with db.exec_one(query, params=params) as table:
                scores[name] = table.to_records()[0]["score"]
    expected = {"search": 0.5, "procedure": 0.5, "exact": 0.5}
    assert scores.keys() == expected.keys(), scores
    assert all(isclose(scores[name], score, rel_tol=1e-6, abs_tol=1e-6)
               for name, score in expected.items()), scores
    print(scores)
```

For stored-vector indexes, searches in a transaction see its own writes. For an
embedder-backed index with pending changes, commit or roll back before searching
it. A neighbor count of zero returns no candidates. `queryRelationships`
provides the relationship-index procedure counterpart.

### Typed text-embedding callbacks

Register a connection-owned callback and name it in
`CREATE VECTOR INDEX ... ON EACH [...]`. Return one finite numeric vector per
input, in input order and with `input.dimensions` values. `VectorEmbeddingInput`
provides `index_name`, `dimensions`, `purpose`, `entity_kind`, `entity_id`, and
ordered `fields`. Each `VectorEmbeddingField` provides `name`, `value`,
`value_type`, `value_json`, and `display`.

The synchronous demonstration callback below selects string fields and returns
three-dimensional vectors. Replace it with your embedding model. The example
seeds papers, runs a bound text query, then reopens the database and registers
the callback again before another text query:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from velr.driver import Velr, VectorEmbeddingInput


def embed(inputs: list[VectorEmbeddingInput]) -> list[list[float]]:
    vectors = []
    for item in inputs:
        assert item.purpose in {"index_entity", "query"}
        if item.purpose == "query":
            assert len(item.fields) == 1 and item.fields[0].name is None
        text = " ".join(
            field.value for field in item.fields
            if field.value_type == "string" and isinstance(field.value, str)
        ).lower()
        vector = [float("graph" in text), float("vector" in text), 1.0]
        assert len(vector) == item.dimensions
        vectors.append(vector)
    return vectors


query = ("CALL db.index.vector.queryNodes('paperEmbedding', $limit, $text) "
         "YIELD node, score RETURN node.title AS title, score ORDER BY score DESC")
params = {"text": "graph", "limit": 1}
with TemporaryDirectory(prefix="velr-text-embeddings-") as directory:
    path = str(Path(directory) / "graph.db")
    with Velr.open(path) as db:
        db.register_vector_embedder("demo", embed)
        db.run(
            "UNWIND $papers AS p CREATE (:Paper {title:p.title, abstract:p.abstract})",
            params={"papers": [
                {"title": "Graph databases", "abstract": "Connected data"},
                {"title": "Semantic vectors", "abstract": "Vector retrieval"},
            ]},
        )
        db.run("CREATE VECTOR INDEX paperEmbedding FOR (n:Paper) ON EACH [n.title, n.abstract] "
               "OPTIONS {indexConfig:{dimensions:3, metric:'cosine', embedder:'demo'}}")
        with db.exec_one(query, params=params) as table:
            first = table.to_records()
    with Velr.open(path) as reopened:
        reopened.register_vector_embedder("demo", embed)
        with reopened.exec_one(query, params=params) as table:
            second = table.to_records()
    assert [record["title"] for record in first] == ["Graph databases"]
    assert [record["title"] for record in second] == ["Graph databases"]
    print([record["title"] for record in first], [record["title"] for record in second])
```

Registration lasts until replacement or connection close. Register the callback
on each connection before operations requiring generated embeddings: text
queries, relevant writes, or recovery that needs the model. Numeric query vectors
can bypass embedding. Indexed fields arrive in `ON EACH [...]` order; query text
is one unnamed string field with `purpose == "query"`.

Do not call or close the same connection or its handles from its embedder.
Callbacks must finish synchronously; coroutine callbacks are unsupported.
Callback failures fail the requesting operation with `VelrError`. Writable
connections repair index data according to its configuration; read-only opens
never repair it.

### Index configuration

Pass options in `CREATE VECTOR INDEX ... OPTIONS {indexConfig:{...}}`.
Quote dotted option keys with backticks in Cypher.

| Option | Accepted aliases | Contract |
|---|---|---|
| `vector.dimensions` | `dimensions` | Required width of stored vectors, callback outputs, and query vectors. |
| `vector.similarity_function` | `metric`, `similarity_function` | `cosine` (default), `l2`/`euclidean`, or `dot`/`inner_product`/`max_inner_product`. |
| `embedder` | `velr.embedder` | Callback name for `ON EACH [...]`; omit for stored-vector indexes. |
| `embedder_fingerprint` | `embedderFingerprint`, `velr.embedder_fingerprint` | Application model/version metadata; does not select or invoke a model. |
| `cache_policy` | `cachePolicy` | `required` retains embeddings for recovery; `best_effort` tolerates failures to retain them; `none` requires recomputation. Defaults: `required` with an embedder, `none` without. Stored-vector indexes require `none`. |
| `vector.hnsw.m` | — | Integer `1..512`. Higher values can improve recall with more memory, disk use, and build time. |
| `vector.hnsw.ef_construction` | — | Integer `1..3200`. Higher values can improve quality with slower builds. |
| `vector.hnsw.ef_search` | — | Integer `1..3200`, default `64`. Higher values can improve recall with slower queries. |

`WITH [n.property]` declares properties available to inner vector SEARCH filters.

## Arrow IPC

`to_ipc_bytes()` exports owned Arrow IPC file/Feather v2 bytes without a PyArrow
dependency. `to_ipc_memoryview()` returns an independently retained view; keep
the view alive while using it. `bind_arrow_ipc()` borrows a contiguous nonempty
buffer only during its call. IPC streams are unsupported.

This complete example exports bytes, a retained memoryview, and a file, then
binds the bytes as queryable input. Bind operations alone do not create graph data.

```python
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from velr.driver import Velr

with TemporaryDirectory(prefix="velr-arrow-ipc-") as directory:
    with Velr.open() as db:
        with db.exec_one("UNWIND $names AS name RETURN name ORDER BY name",
                         params={"names": ["Grace", "Ada"]}) as table:
            ipc = table.to_ipc_bytes()
            view = table.to_ipc_memoryview()
            output = BytesIO()
            written = table.write_ipc_to(output)
            path = Path(directory) / "people.arrow"
            table.save_ipc(str(path))
        assert ipc and view.nbytes > 0 and written == len(output.getvalue())
        assert path.stat().st_size > 0
        db.bind_arrow_ipc("people", ipc)
        records = db.to_records("UNWIND BIND('people') AS row RETURN row.name AS name ORDER BY name")
    assert records == [{"name": "Ada"}, {"name": "Grace"}]
    assert view.nbytes > 0
    view.release()
    print(records)
```

Arrow and dataframe exports infer a schema per result. Numeric columns may be
integers/floats; incompatible mixed kinds become strings. Lists, maps, nodes,
relationships, and paths become JSON UTF-8 strings, rather than nested Arrow
arrays. Nulls remain null; empty/all-null columns use UTF-8. Inspect exported
schemas before choosing application dtypes. Exports materialize data and can
use memory proportional to result size.

### PyArrow exports and retained IPC buffers

Requires `pyarrow` 15 or newer. `to_pyarrow()` returns a `pyarrow.Table`;
`to_pyarrow_zero_copy()` returns an encoded IPC `pyarrow.Buffer`, not a table.
Both retain their data independently of Velr handles. Keep a returned buffer
alive while using its data.

```python
import pyarrow as pa
from velr.driver import Velr

with Velr.open() as db:
    with db.exec_one("RETURN $name AS name, $age AS age",
                     params={"name": "Alice", "age": 42}) as table:
        arrow = table.to_pyarrow()
        ipc_buffer = table.to_pyarrow_zero_copy()
expected = [{"name": "Alice", "age": 42}]
assert arrow.to_pylist() == expected
assert pa.ipc.open_file(ipc_buffer).read_all().to_pylist() == expected
print(arrow.to_pylist())
```

Connection shortcuts `to_pyarrow(cypher)`, `to_pandas(cypher)`, and
`to_polars(cypher)` convert the final table. Use `exec_one()` followed by a table
conversion to supply parameters, row caps, cancellation, or deadlines.

### Bind Arrow columns and typed records

Requires PyArrow. `bind_arrow()` accepts a `Table`, `RecordBatch`, mapping of
column names to Arrow arrays/chunked arrays, or `(name, array)` pairs.
`bind_arrow_chunks()` is an alias supporting the same inputs. Treat bound input
arrays as immutable. Read them using `UNWIND BIND('name') AS row`.

For record type control, pass `types=` to `bind_records()`. Any `types` mapping
selects the Arrow conversion path; a nonempty mapping defines the complete
schema and field selection, not partial per-field casts. Include every field
you want retained.

```python
import pyarrow as pa
from velr.driver import Velr

with Velr.open() as db:
    source = pa.table({"name": ["Alice", "Grace"], "age": [42, 37]})
    db.bind_arrow("people", source)
    imported = db.to_pyarrow("UNWIND BIND('people') AS row RETURN row.name AS name ORDER BY name")
    chunks = {"name": pa.chunked_array([["Ada"], ["Lin"]])}
    db.bind_arrow_chunks("chunked", chunks)
    chunked = db.to_records("UNWIND BIND('chunked') AS row RETURN row.name AS name ORDER BY name")
    db.bind_records("typed", [{"name": "Ada", "age": 36}],
                    types={"name": pa.string(), "age": pa.int64()})
    typed = db.to_records("UNWIND BIND('typed') AS row RETURN row.name AS name, row.age AS age")
assert imported.to_pylist() == [{"name": "Alice"}, {"name": "Grace"}]
assert chunked == [{"name": "Ada"}, {"name": "Lin"}]
assert typed == [{"name": "Ada", "age": 36}]
print(imported.to_pylist(), chunked, typed)
```

### pandas import and export

Requires pandas 2 or newer and PyArrow. Install the `velr[pandas]` extra.
`bind_pandas()` excludes the dataframe index by default; set `index=True` to
include it. `jsonify_objects=True` serializes nested object values as JSON text
and stringifies unsupported objects before conversion. Set it to `False` for
PyArrow's conversion directly. `schema=` accepts a complete PyArrow schema.

```python
import pandas as pd
from velr.driver import Velr

source = pd.DataFrame({"name": ["Alice", "Grace"], "age": pd.Series([42, None], dtype="Int64")})
with Velr.open() as db:
    db.bind_pandas("people", source)
    db.run("UNWIND BIND('people') AS row CREATE (:Person {name:row.name, age:row.age})")
    output = db.to_pandas("MATCH (p:Person) RETURN p.name AS name, p.age AS age ORDER BY name")
    with db.exec_one("UNWIND $values AS value RETURN value",
                     params={"values": [9007199254740993, None]}) as table:
        exact = table.to_pyarrow().to_pandas(types_mapper=pd.ArrowDtype)
assert output["name"].tolist() == ["Alice", "Grace"]
assert output["age"].isna().tolist() == [False, True]
assert str(output["age"].dtype) == "Int64"
assert int(exact["value"].iloc[0]) == 9007199254740993
assert pd.isna(exact["value"].iloc[1])
print(output["name"].tolist(), output["age"].isna().tolist())
```

The default pandas `dtype_backend="numpy_nullable"` selects nullable dtypes;
use `None` to skip that conversion. Other keyword arguments are unsupported.
Default pandas export can round nullable int64 values outside the exact float
integer range (`2**53`), even when the result shows `Int64` dtype. Changing
`dtype_backend` does not recover lost precision. Velr's `to_records()` and
`to_pyarrow()` preserve these integers. For exact nullable integers in pandas,
convert the Arrow table with `types_mapper=pd.ArrowDtype`, as above.
Exported dataframes remain usable after database handles close.

### Polars import and export

Requires Polars 0.20 or newer and PyArrow. Install `velr[polars]`.
`bind_polars(..., rechunk=True)` combines chunks before conversion; its default
preserves them. Exported dataframes retain their data after handles close.

```python
import polars as pl
from velr.driver import Velr

source = pl.concat([
    pl.DataFrame({"name": ["Alice"], "age": [42]}),
    pl.DataFrame({"name": ["Grace"], "age": [37]}),
], rechunk=False)
with Velr.open() as db:
    db.bind_polars("people", source, rechunk=True)
    output = db.to_polars("UNWIND BIND('people') AS row RETURN row.name AS name, row.age AS age ORDER BY name")
expected = [{"name": "Alice", "age": 42}, {"name": "Grace", "age": 37}]
assert output.to_dicts() == expected
print(output.to_dicts())
```

### NumPy import

Requires NumPy and PyArrow. Pass a mapping of names to arrays, or a
2-dimensional array with one name per column. `types=` optionally supplies
PyArrow types for the NumPy columns.

```python
import numpy as np
import pyarrow as pa
from velr.driver import Velr

source = np.array([[1, 42], [2, 37]], dtype=np.int64)
with Velr.open() as db:
    db.bind_numpy("people", source, names=["id", "age"],
                  types={"id": pa.int64(), "age": pa.int64()})
    rows = db.to_rows("UNWIND BIND('people') AS row RETURN row.id AS id, row.age AS age ORDER BY id")
assert rows == [[1, 42], [2, 37]]
print(rows)
```

### Bind ordinary records in a transaction

`bind_records()` makes records queryable; it does not insert nodes by itself.
Nonempty records with identical key sets and supported scalar or JSON-compatible
nested list/tuple/mapping values need no PyArrow. Empty/nonuniform records,
Arrow-specific values, or a supplied `types` mapping use the Arrow path and
require PyArrow. When Arrow infers a record schema, it uses the
first record's fields; normalize key sets or supply a complete schema to retain
later-only keys. Supply an explicit complete schema when an empty batch needs
known columns. Bound values can then be written in a transaction:

```python
from velr.driver import Velr

records = [{"name": "Alice", "age": 42}, {"name": "Grace", "age": 37}]
with Velr.open() as db:
    with db.begin_tx() as tx:
        tx.bind_records("people", records)
        tx.run("UNWIND BIND('people') AS row CREATE (:Person {name:row.name, age:row.age})")
        tx.commit()
    output = db.to_records("MATCH (p:Person) RETURN p.name AS name, p.age AS age ORDER BY name")
assert output == records
print(output)
```

## Introspection

`SHOW INDEXES` lists range, fulltext, and vector indexes with `name`, `state`,
`type`, `entityType`, `labelsOrTypes`, and `properties`. Catalog procedures list
names from graph data and index declarations; property keys can include
registered unused keys. `SHOW CURRENT GRAPH SHAPE` describes observed data,
not a declared schema. Migrate supported older formats before graph queries or
introspection.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from velr.driver import Velr

with TemporaryDirectory(prefix="velr-inspection-") as directory:
    with Velr.open(str(Path(directory) / "graph.db")) as db:
        db.run("CREATE (:Person {name:'Alice', age:42})-[:KNOWS {weight:0.5}]->(:Person {name:'Grace', age:37})")
        db.run("CREATE INDEX personName FOR (p:Person) ON (p.name)")
        indexes = db.to_records("SHOW INDEXES YIELD name, type RETURN name, type ORDER BY name")
        labels = db.to_records("CALL db.labels() YIELD label RETURN label ORDER BY label")
        relations = db.to_records("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")
        keys = db.to_records("CALL db.propertyKeys() YIELD propertyKey RETURN propertyKey ORDER BY propertyKey")
        properties = db.to_records(
            "SHOW CURRENT GRAPH SHAPE YIELD element_kind, property_name "
            "WHERE element_kind = 'node_property' RETURN property_name ORDER BY property_name"
        )
    assert indexes == [{"name": "personName", "type": "RANGE"}]
    assert labels == [{"label": "Person"}]
    assert relations == [{"relationshipType": "KNOWS"}]
    assert keys == [{"propertyKey": "age"}, {"propertyKey": "name"}, {"propertyKey": "weight"}]
    assert properties == [{"property_name": "age"}, {"property_name": "name"}]
    print(indexes, labels, relations, keys, properties)
```

Use `YIELD`, `WHERE`, and `RETURN` to project/filter results. Plain graph shape
returns `element_kind`, `element_name`, `property_name`, `observed_type`,
`owner_count`, `present_count`, and `missing_count`; `YIELD *` exposes additional
fields including `surface`, `source_label`, `target_label`, `required`,
`storage_class`, and `tag`.

## Explain plans

`explain()` returns an `ExplainTrace` without executing the query.
`explain_analyze()` executes it, including writes; use a transaction and rollback
when those writes should be discarded. These methods accept query text only.
Indexed methods inspect plans/steps/statements, `snapshot()` returns owned
metadata dataclasses, and `to_compact_string()` returns display text. Diagnostic
text and identifiers can change between versions.

```python
from velr.driver import Velr

with Velr.open() as db:
    db.run("CREATE (:Person {name:'Alice'})")
    with db.explain("MATCH (p:Person) RETURN p.name AS name") as trace:
        snapshot = trace.snapshot()
        assert trace.plan_count() == len(snapshot) == 1
        assert isinstance(trace.to_compact_string(), str)
    with db.begin_tx() as tx:
        with tx.explain_analyze("CREATE (:Person {name:'Temporary'})") as trace:
            assert trace.plan_count() == 1
        tx.rollback()
    records = db.to_records("MATCH (p:Person) RETURN count(p) AS count")
assert len(snapshot) == 1 and records == [{"count": 1}]
print(len(snapshot), records)
```

## Opening an existing database read-only

`open_readonly()` requires an existing supported database and never creates,
initializes, migrates, or repairs it. Migrate supported older files through a
writable connection first, then reopen read-only connections. This example
creates a file, closes its writer, and opens it for reads:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from velr.driver import Velr

with TemporaryDirectory(prefix="velr-readonly-") as directory:
    path = str(Path(directory) / "graph.db")
    with Velr.open(path) as writer:
        writer.run("CREATE (:Person {name:$name})", params={"name": "Alice"})
    with Velr.open_readonly(path) as reader:
        records = reader.to_records("MATCH (p:Person) RETURN p.name AS name")
    assert records == [{"name": "Alice"}]
    print(records)
```

## Database creation options

`Velr.create(path, inline_edge_properties=...)` creates a fresh file and fails
if it exists. Omit the path or pass `None` or `":memory:"` for an in-memory
database. Choose zero or one nonempty, case-sensitive relationship property
name without NUL bytes. The selection applies to every relationship type,
persists on reopen, and cannot be changed afterward. Read/write all values
through normal Cypher. Use `open()` or `open_readonly()` to reopen a file.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from velr.driver import Velr

with TemporaryDirectory(prefix="velr-create-") as directory:
    path = str(Path(directory) / "events.db")
    with Velr.create(path, inline_edge_properties=("timestamp",)) as db:
        db.run("CREATE ()-[:EVENT {timestamp:$time}]->()", params={"time": 1700000000})
    with Velr.open_readonly(path) as reader:
        names = reader.inline_edge_properties()
        records = reader.to_records("MATCH ()-[r:EVENT]->() RETURN r.timestamp AS timestamp")
    assert names == ["timestamp"] and records == [{"timestamp": 1700000000}]
    print(names, records)
```

## Schema migration

Opening a database never migrates it. The 0.2.62 runtime creates schema version
11 and supports migration from schema versions 3 through 9. Migrate supported
older formats on a writable connection before graph queries or transactions.
Reopen read-only connections afterward. Databases created before Velr 0.2.14
require reimporting source data.

This complete example uses a fresh database, so the report is `already_current`.
In application maintenance code, open the existing path and inspect
`needs_migration()` before deciding to migrate:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from velr.driver import Velr

with TemporaryDirectory(prefix="velr-migrate-") as directory:
    with Velr.open(str(Path(directory) / "graph.db")) as db:
        assert db.schema_version() == db.current_schema_version() == 11
        assert db.needs_migration() is False
        report = db.migrate()
        assert report.status == "already_current" and report.from_version == report.to_version == 11
        with db.exec_one("MIGRATE DATABASE") as table:
            command_report = table.to_records()
        assert command_report[0]["status"] == "already_current"
        print(report.status, report.from_version, report.to_version, command_report[0]["status"])
```

Migration requires a writable connection outside an active transaction.
`MIGRATE DATABASE` is its equivalent command for scripts and tools.

## Maintenance

After large deletes or pruning, use `vacuum()` to compact a file-backed database.
It requires a writable, current-schema connection outside a transaction.
`VACUUM DATABASE` is the equivalent command.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from velr.driver import Velr

with TemporaryDirectory(prefix="velr-vacuum-") as directory:
    with Velr.open(str(Path(directory) / "graph.db")) as db:
        db.run("CREATE (:Person {name:'Alice'}), (:Expired {name:'temporary'})")
        db.run("MATCH (n:Expired) DETACH DELETE n")
        db.vacuum()
        records = db.to_records("MATCH (n) RETURN count(n) AS count")
    assert records == [{"count": 1}]
    print(records)
```

## Deployment configuration

Use Python 3.12 or newer and a wheel for your supported Python/platform target.
The wheel includes its runtime; a separate server is unnecessary. The embedded
Python package loads its bundled runtime and does not provide runtime-path or
runtime-cache environment overrides. Reinstall the matching wheel if its runtime
files are missing. See [requirements and installation](README.md#requirements-and-installation).

Install `velr[pandas]` for pandas/PyArrow, `velr[polars]` for Polars/PyArrow, or
`velr[all]` for both integrations. NumPy binding additionally requires NumPy.
Keep Arrow-backed inputs immutable while bound. Use ordinary Python imports in
scripts and applications, and deterministic context-manager cleanup for every
connection, transaction, result, and savepoint.
