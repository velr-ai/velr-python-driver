# Velr Python driver

Velr is an embedded property-graph database queried with Cypher. The `velr`
package runs the database in your Python process and includes the runtime.
Use the [driver guide](https://velr-ai.github.io/velr-python-driver/velr/guide.html)
for detailed workflows and the
[API reference](https://velr-ai.github.io/velr-python-driver/velr/driver.html)
for complete argument and lifetime contracts.

## Requirements and installation

Python 3.12 or later is required. Prebuilt wheels include the runtime for macOS
arm64, Linux x86_64/aarch64 with glibc, and Windows x86_64. Install the package:

```sh
python -m pip install 'velr>=0.2.62'
```

No separate server or runtime download is required. Fulltext and vector indexes
require a file-backed database. Optional dataframe packages are
installed only when you need those integrations:

```sh
python -m pip install 'velr[pandas]>=0.2.62'   # pandas and PyArrow
python -m pip install 'velr[polars]>=0.2.62'   # Polars and PyArrow
```

## Quick start

Bind application values as parameters and collect ordinary Python records:

```python
from velr import Velr

with Velr.open() as db:
    db.run(
        "CREATE (:Person {name: $name, age: $age})",
        params={"name": "Alice", "age": 42},
    )
    with db.exec_one("MATCH (p:Person) RETURN p.name AS name, p.age AS age") as table:
        print(table.to_records())
        # [{'name': 'Alice', 'age': 42}]
```

`Velr.open()` creates an in-memory database. `Velr.open("mygraph.db")` opens or
creates a persistent database. Context managers close the handles they own.
Use the same imports and API in Jupyter notebook cells. Public driver types and
exceptions are also available directly from `velr`, including `QueryError`,
`ConstraintError`, `VelrError`, and `ErrorPosition`. Existing imports from
`velr.driver` remain supported. `import velr` and importing the reference modules
`velr.guide` or `velr.query_language` do not load the native runtime; accessing a
public driver attribute such as `velr.Velr` loads it when needed.

Install `velr[gds]` to add graph analytics, then open a connection with
`Velr.open(extensions=[Gds()])`. The package supplies `Gds` through
`from velr_gds import Gds`. The [driver guide](https://velr-ai.github.io/velr-python-driver/velr/guide.html) has a graph analytics
section with projection, execution, and cleanup.

## Queries and results

### In-memory, persistent, and read-only databases

Use `Velr.open()` for a private in-memory database and a filesystem path when
data must survive the Python process. The parent directory must exist. Paths
must be NUL-free strings; convert `pathlib.Path` values with `str(path)`. Close
the writer before reopening the file in read-only mode:

```python
from velr.driver import Velr

with Velr.open("people.db") as db:
    db.run("CREATE (:Person {name: $name})", params={"name": "Alice"})

with Velr.open_readonly("people.db") as db:
    with db.exec_one("MATCH (p:Person) RETURN p.name AS name") as table:
        print(table.to_records())  # [{'name': 'Alice'}]
```

`open_readonly()` requires an existing supported database and performs no writes
or repairs. For a read-only snapshot while another connection writes, begin a
transaction on the read-only connection. Fulltext and vector indexes need a
file-backed database; ordinary graph queries also work in memory.

### Database creation options

`Velr.create()` creates a new database and rejects an existing file path. Use it
when selecting creation-only options; use `open()` to open or create with the
default options:

```python
from velr.driver import Velr

with Velr.create("events.db", inline_edge_properties=["timestamp"]) as db:
    assert db.inline_edge_properties() == ["timestamp"]
    db.run("CREATE (:Event)-[:NEXT {timestamp:42}]->(:Event)")
```

`inline_edge_properties` accepts zero or one nonempty, case-sensitive
relationship property name without NUL characters. The selection applies across relationship types,
persists after reopening, and cannot be changed for an existing database.
Use ordinary Cypher to read and write relationship properties. An empty sequence
leaves the option disabled.

### Choosing an execution method

| Task | API |
|---|---|
| Execute a write or discard results | `run()`; `execute()` is an alias |
| Read exactly one table | `exec_one()` |
| Read multiple tables | `exec()`, then `next_table()` or `iter_tables()` |
| Collect ordinary Python values | `Table.to_rows()` or `Table.to_records()` |
| Process rows incrementally | `Table.iter_rows()`, `for_each_row()` |
| Convert the last result of a script | Connection `to_rows()`, `to_records()`, `to_pandas()`, `to_polars()`, `to_pyarrow()` |

### Parameters and owned values

`run()`, `exec()`, `exec_one()`, and their transaction equivalents accept
`params`. Query text uses `$name`;
parameter keys omit `$`. Values may be `None`, booleans, signed 64-bit integers,
finite floats, strings, lists/tuples, and dictionaries with string keys. Values
are bound as data. Invalid types raise `TypeError`; invalid names, bounds, or
non-finite floats raise `ValueError`.

`exec_one()` raises `VelrError` if execution produces zero or multiple tables.
`to_rows()` returns lists of row values; `to_records()` returns dictionaries
keyed by column name. Both decode Cypher lists and maps into Python lists and
dictionaries and allocate memory for the collected result. Cursor APIs yield
`Cell` values; `Cell.as_python(parse_json=True)` decodes structured cells.
`to_records()` requires unique column names. Give projected expressions unique
aliases in `RETURN`; duplicate aliases fail query validation.

Materialized paths are `velr.driver.Path` values. Their `elements` tuple
alternates node and relationship dictionaries, beginning and ending with a node;
a zero-length path contains one node. These values remain usable after the
result closes. Row-level `Cell.as_python(parse_json=True)` instead decodes the
cell's JSON representation directly.

### Multiple result tables and rows

Use records for named application values and row iteration when the result
should be processed incrementally. Keep the table open while reading it:

```python
from velr.driver import Velr

with Velr.open() as db:
    with db.exec_one(
        "UNWIND $people AS p RETURN p.name AS name, p.age AS age ORDER BY age",
        params={"people": [{"name": "Alice", "age": 42}, {"name": "Bob", "age": 30}]},
    ) as table:
        with table.iter_rows() as rows:
            for row in rows:
                name, age = (cell.as_python() for cell in row)
                print(name, age)  # Bob 30, then Alice 42
```

Use `exec()` for scripts that return several tables. Consume or close each
table before advancing the stream:

```python
from velr.driver import Velr

with Velr.open() as db:
    with db.exec("RETURN 1 AS first; RETURN 2 AS second") as stream:
        for table in stream.iter_tables():
            with table:
                print(table.column_names(), table.to_rows())
                # ['first'] [[1]], then ['second'] [[2]]
```

Connection shortcuts such as `db.to_records(query)` collect the final table of
a script. Use `exec_one(query, params=...).to_records()` inside a table context
when you need parameters, deadlines, or result caps.

### Row caps, deadlines, and cancellation

`max_result_rows=None` leaves results uncapped. Zero returns columns without
rows; a positive integer caps each table separately. Query `LIMIT` clauses still
apply. A row cap does not limit execution time or prevent writes.

`timeout` is a finite, nonnegative number of seconds covering execution and
subsequent result fetching; omit it or pass `None` for no deadline, and use
zero for immediate expiry. Pass a `CancellationToken`
to request cancellation, including from another thread. Use a fresh token for
independent operations. See [query-control examples](https://velr-ai.github.io/velr-python-driver/velr/guide.html#query-cancellation-and-timeouts).

For example, combine a row cap with a deadline and a cancellation token. Give
the token to your stop handler when a query should be cancellable:

```python
from velr.driver import CancellationToken, Velr, VelrError

stop = CancellationToken()
with Velr.open() as db:
    try:
        with db.exec_one(
            "UNWIND [1, 2, 3] AS n RETURN n",
            max_result_rows=2,
            timeout=5.0,
            cancellation=stop,
        ) as table:
            assert table.to_rows() == [[1], [2]]
    except VelrError as error:
        if error.code not in {"query.cancelled", "query.deadline_exceeded"}:
            raise
        print(error.code)
```

Call `stop.cancel()` from a request handler or another thread to stop an active
operation. Catch errors around both execution and result reading.

### Resource lifetime

Close connections, transactions, streams, tables, row iterators, savepoints, and
explain traces explicitly or use their context managers. Closing a connection
closes its children. Tables from `exec()` close with their stream; tables from
`exec_one()` close with their connection or transaction. Finish results before
ending their transaction. Collected Python rows, copied cells, dataframe exports,
and explain snapshots remain usable after their producing handles close.

## Transactions

Explicitly call `commit()` to keep changes. Leaving a transaction context without
committing rolls it back:

```python
from velr.driver import Velr

with Velr.open() as db:
    with db.begin_tx() as tx:
        tx.run("CREATE (:Person {name: $name})", params={"name": "Alice"})
        tx.commit()
    assert db.to_records("MATCH (p:Person) RETURN p.name AS name") == [{"name": "Alice"}]
```

`savepoint()` creates a scoped savepoint: `release()` keeps changes and
`rollback()` undoes them; closing an active scoped savepoint rolls it back.
A savepoint context releases on normal exit and rolls back/releases on an
exception. The enclosing transaction still requires an explicit `commit()`.
`savepoint_named(name)` creates a transaction-owned marker.
`rollback_to(name)` keeps its target active and discards newer named savepoints;
`release_savepoint(name)` releases the newest named savepoint. See
[transaction and savepoint examples](https://velr-ai.github.io/velr-python-driver/velr/guide.html#transactions-and-savepoints).

### Undoing part of a transaction

Use a savepoint when one step may be undone while earlier writes are retained:

```python
from velr.driver import Velr

with Velr.open() as db:
    with db.begin_tx() as tx:
        tx.run("CREATE (:Person {name:'Alice'})")
        with tx.savepoint() as savepoint:
            tx.run("CREATE (:Person {name:'Temporary'})")
            savepoint.rollback()
        tx.commit()
    assert db.to_records("MATCH (p:Person) RETURN p.name AS name") == [{"name": "Alice"}]
```

Only one transaction can be active on a connection. On a writable connection,
`begin_tx()` reserves the writer when it starts. Use separate connections for
independent work and finish transaction results before committing or rolling
back. For a snapshot while another connection writes, use a transaction on a
separate read-only connection.

### Named savepoints

A named savepoint belongs to its transaction. Closing its Python handle leaves
the marker active. Names must be unique while active. Roll back to a marker
to discard subsequent changes, then release it before committing. A named
savepoint used as a context follows the same success/exception behavior as a
scoped savepoint; this example closes its handle explicitly to retain the marker:

```python
from velr.driver import Velr

with Velr.open() as db:
    with db.begin_tx() as tx:
        tx.run("CREATE (:Person {name:'Alice'})")
        tx.savepoint_named("before_draft").close()
        tx.run("CREATE (:Person {name:'Draft'})")
        tx.rollback_to("before_draft")
        tx.run("CREATE (:Person {name:'Grace'})")
        tx.release_savepoint("before_draft")
        tx.commit()
    assert db.to_records("MATCH (p:Person) RETURN p.name AS name ORDER BY name") == [
        {"name": "Alice"}, {"name": "Grace"}
    ]
```

## Errors and concurrency

Catch `VelrError` around execution and complete result consumption: errors can
arrive when starting a query, advancing tables, or reading rows. Catch `QueryError`
for Cypher failures, or a specific subclass such as `ParameterError`,
`ConstraintError`, or `CypherTypeError`. `HandleStateError` identifies closed,
wrong-thread, and in-use handles. All remain subclasses of `VelrError`.

Use `category`, `detail`, and `phase` for programmatic handling.
`explanation` aliases the existing concise `message`; `report` is the complete
rendered diagnostic, identical to `str(error)`. `hint` supplies optional
remediation. A known `position` is an `ErrorPosition` with `.line` and `.column`:
one-based coordinates in the submitted query, with columns measured in display
cells. It remains a tuple for indexing, unpacking, and equality. Unknown
positions and other unavailable metadata are `None`. Diagnostic wording can vary.

```python
from velr import ParameterError, Velr, VelrError

with Velr.open() as db:
    try:
        db.run("RETURN $missing")
    except ParameterError as error:
        print(error.category, error.detail)
    except VelrError as error:
        print(error.report)
```

`DatabaseBusyError` exposes `retryable` and SQLite's `sqlite_errorcode` when
available. A transient failure does not establish that repeating a write or
transaction is safe. `ReadOnlyError` and `MigrationRequiredError` identify their
required corrective actions. `status_code` is the numeric native status, or
`None` for Python-generated failures. `QueryCancelled` and `DeadlineExceeded`
preserve the existing `code` values `query.cancelled` and
`query.deadline_exceeded`; `DeadlineExceeded` also inherits `TimeoutError`.
Older runtimes without structured diagnostics retain `VelrError`, native status,
and stop handling rather than inferring categories from diagnostic text.

Invalid Python arguments and missing optional packages can raise standard Python
exceptions. Row callback exceptions propagate unchanged; embedding callback
failures surface as `VelrError` with the Python exception in `__cause__`.

Use a separate connection per thread for concurrent work and keep active
transactions and results on their owning thread. A cancellation token may be
signaled from another thread. The [lifetime and thread guide](https://velr-ai.github.io/velr-python-driver/velr/guide.html#results-lifetimes-and-threads)
covers retained values and moving an idle connection.

## Features and integrations

### Quantified path patterns

Use `{2}` to match two repetitions of a path segment. The node variables
inside the segment return lists in traversal order.

```python
from velr.driver import Velr

with Velr.open() as db:
    db.run(
        "CREATE (:Stop {name: 'Central'})-[:NEXT]->(:Stop {name: 'North'})"
        "-[:NEXT]->(:Stop {name: 'Airport'})"
    )
    with db.exec_one(
        "MATCH (:Stop {name: 'Central'}) ((departure)-[:NEXT]->(arrival)){2} "
        "RETURN [stop IN departure | stop.name] AS departures, "
        "[stop IN arrival | stop.name] AS arrivals"
    ) as table:
        print(table.to_records())
        # [{'departures': ['Central', 'North'], 'arrivals': ['North', 'Airport']}]
```

See the [quantified path patterns reference](https://velr-ai.github.io/velr-python-driver/velr/query_language.html#quantified-path-patterns)
for ranges and per-repetition filters.

| Feature | Entry point |
|---|---|
| Fulltext search | `CREATE FULLTEXT INDEX`, then `MATCH ... SEARCH ... IN (FULLTEXT INDEX ...)` |
| Vector search | `MATCH ... SEARCH ... IN (VECTOR INDEX ...)`; stored vectors and registered embedders |
| Arrow and dataframes | `bind_arrow()`, `bind_arrow_ipc()`, dataframe/NumPy/record binders, and table export methods |
| Graph introspection | `SHOW CURRENT GRAPH SHAPE` |
| Query diagnostics | `explain()`, `explain_analyze()`, `ExplainTrace.snapshot()` |

The examples here cover the main integrations. The [driver guide](https://velr-ai.github.io/velr-python-driver/velr/guide.html)
adds model callbacks, data conversion options, and extended workflows; the
[query language reference](https://velr-ai.github.io/velr-python-driver/velr/query_language.html) describes supported syntax,
functions, search options, and restrictions.

### SEARCH clause

Use `SEARCH` with `MATCH` or `OPTIONAL MATCH` to query an existing fulltext or
vector index. Bind the searched node or relationship in a pattern of at most
one hop; supply `FOR` and a required candidate `LIMIT` inside the search
parentheses. Bind application input with parameters, expose scores with
`SCORE AS name`, and request ordering with `ORDER BY name DESC`.

Fulltext supports `WITH ANALYZER`, `SKIP`/`OFFSET`, and parameterized index names.
Vector index names are identifiers. An inner vector `WHERE` selects candidates
using properties declared in the index's `WITH [...]`; an outer `WHERE` filters
the ranked candidates and can return fewer rows than the search limit.
`SEARCH` also works in correlated `EXISTS { ... }` and `COUNT { ... }`
subqueries. See the [search guide](https://velr-ai.github.io/velr-python-driver/velr/guide.html#search-clause) for complete examples
and restrictions. The search APIs below require runtime 0.2.62 or newer.

### Fulltext search

Create an index over text properties, then bind user search text as a parameter:

```python
from velr.driver import Velr

with Velr.open("search.db") as db:
    db.run("CREATE (:Paper {title:'Graph databases', abstract:'Connected data'})")
    db.run("CREATE FULLTEXT INDEX paperText IF NOT EXISTS "
           "FOR (p:Paper) ON EACH [p.title, p.abstract]")
    with db.exec_one(
        "MATCH (p:Paper) SEARCH p IN (FULLTEXT INDEX paperText FOR $text LIMIT 10) "
        "SCORE AS score RETURN p.title AS title, score ORDER BY score DESC",
        params={"text": "graph"},
    ) as table:
        results = table.to_records()
        assert results[0]["title"] == "Graph databases"
        print(results)
```

Search text supports terms, phrases, field scoping, boolean grouping, prefixes,
and boosts. Higher scores rank results within the same query; scores are not
normalized probabilities. Writes keep the index up to date.
`LIMIT` inside `SEARCH` is required and bounds candidates. Fulltext search also
supports `WITH ANALYZER`, `SKIP` / `OFFSET`, and a parameterized index name.
`SCORE AS score` exposes the relevance score. The
`CALL db.index.fulltext.queryNodes(...)` procedure remains supported with the
same fulltext score scale.

### Vector search and embeddings

Store vectors computed by your application, declare their dimensions and metric,
and pass a numeric query vector:

```python
from velr.driver import Velr

with Velr.open("vectors.db") as db:
    db.run("CREATE (:Chunk {text:'Graph databases', embedding:[1.0, 0.0, 0.0]})")
    db.run("CREATE VECTOR INDEX chunkEmbedding IF NOT EXISTS "
           "FOR (c:Chunk) ON (c.embedding) "
           "OPTIONS {indexConfig:{dimensions:3, metric:'cosine'}}")
    with db.exec_one(
        "MATCH (c:Chunk) SEARCH c IN (VECTOR INDEX chunkEmbedding FOR $vector LIMIT 5) "
        "SCORE AS score RETURN c.text AS text, score ORDER BY score DESC",
        params={"vector": [1.0, 0.0, 0.0]},
    ) as table:
        results = table.to_records()
        assert results[0]["text"] == "Graph databases"
        print(results)
```

Every stored/query vector must match the index dimensions. Cosine vectors must
have a nonzero component. Cosine `SEARCH` and vector procedure scores use
`(1 + cosine) / 2` in `0..1`. Convert an older raw cosine procedure threshold
`t` to `(1 + t) / 2`. For Euclidean/L2, `SEARCH` returns
`1 / (1 + squaredDistance)` and the procedures return `-squaredDistance`.
Dot-product scores are unbounded. Higher is better.

Vector index names are identifiers, not string parameters. Put a vector filter
inside the search to filter before nearest-neighbor selection, and declare its
properties with `WITH [c.category]` when creating the index. An outer `WHERE`
filters the selected candidates afterward and can return fewer rows than the
inner limit. The [SEARCH reference](https://velr-ai.github.io/velr-python-driver/velr/query_language.html#search-clauses) covers
filter forms, pagination bounds, relationship searches, and optional matches.

For text-to-vector search, register a callback with
`register_vector_embedder(name, callback)` and reference its name in an index
defined with `ON EACH [...]` and `indexConfig.embedder`. The callback receives a
batch of `VectorEmbeddingInput` values and returns one finite numeric vector per
input, in order. It runs synchronously; do not use the same connection inside
it. Use the `CALL` procedure for text queries through a registered embedder;
vector `SEARCH` takes a numeric query vector. Register callbacks on each
connection whose operations need embeddings, including after reopening a file.
If a transaction has pending changes to an embedder-backed index, commit or
roll back before searching that index. The
[vector guide](https://velr-ai.github.io/velr-python-driver/velr/guide.html#vector-search-and-embeddings) demonstrates the full
registration and text-query workflow.

### Bulk import from CSV

Use the standalone `velr-import` package to create a Velr database from local
node and relationship CSV files. Install it alongside the query driver.

```sh
python -m pip install velr-import
```

This example writes a node CSV, imports it into a new database, and opens the
database with the query driver after the import returns.

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from velr.driver import Velr
from velr_import import import_csv

with TemporaryDirectory() as directory:
    source = Path(directory) / "people.csv"
    source.write_text("personId:ID(people),name:string,:LABEL\n1,Ada,Person\n", encoding="utf-8")
    database = Path(directory) / "people.db"

    report = import_csv(database, files=[source])
    assert report.nodes == 1

    with Velr.open_readonly(str(database)) as db:
        assert db.to_records("MATCH (p:Person) RETURN p.name AS name") == [{"name": "Ada"}]
```

Use a new destination and an importer built for the same Velr release. The
`files` argument accepts plain or compressed CSV with embedded headers.
Use `manifest="import.toml"` to group file parts or supply separate headers.
The `velr-import` README covers header syntax, options, and reports.

### Arrow import and export

Bind Python records as an input table and read them through `BIND()`; importing
a table does not create graph nodes until your query explicitly writes them:

```python
from velr.driver import Velr

with Velr.open() as db:
    db.bind_records("_people", [{"name": "Alice", "age": 42}])
    db.run("UNWIND BIND('_people') AS p CREATE (:Person {name:p.name, age:p.age})")
    assert db.to_records("MATCH (p:Person) RETURN p.name AS name") == [{"name": "Alice"}]
```

Use `bind_pandas()`, `bind_polars()`, `bind_numpy()`, or `bind_arrow()` for
existing dataframe/columnar data. These integrations require their corresponding
packages and PyArrow. Nonempty, uniform records containing supported scalar or
JSON-compatible nested values work without optional dataframe packages. Empty
or nonuniform records, Arrow-specific values, or a supplied `types` mapping use
PyArrow. A nonempty `types` mapping defines the full Arrow schema and selected
fields; include every column you want to bind. Inferred Arrow schemas use the
first record's fields, so normalize record keys or supply a full schema to
retain fields present only in later records. See the [import guide](https://velr-ai.github.io/velr-python-driver/velr/guide.html#arrow-ipc)
for dataframe, NumPy, nullable-record, and explicit-schema examples.

With `velr[pandas]` installed, convert a result directly:

```python
from velr.driver import Velr

with Velr.open() as db:
    with db.exec_one("RETURN 'Alice' AS name, 42 AS age") as table:
        frame = table.to_pandas()
assert frame.to_dict(orient="records") == [{"name": "Alice", "age": 42}]
```

`to_polars()` and `to_pyarrow()` return Polars and Arrow tables. These exports
materialize data; the returned objects remain usable after the table closes.
Arrow/dataframe schemas are inferred from result values: structured values are
JSON strings, mixed numeric values can widen to floats, and incompatible value
kinds use strings. Nulls are preserved; empty/all-null columns use UTF-8 strings.
Use `to_rows()` or `to_records()` for decoded Python lists and dictionaries.
Default pandas conversion can round large integers in nullable columns. Use the
guide's [exact integer conversion](https://velr-ai.github.io/velr-python-driver/velr/guide.html#arrow-ipc) when those values must be
preserved.

For an Arrow IPC file without a dataframe dependency, use `table.to_ipc_bytes()`
or `table.save_ipc(path)`. `bind_arrow_ipc(name, bytes)` accepts IPC file/Feather
v2 bytes; it does not accept the Arrow IPC streaming format.

```python
from velr.driver import Velr

with Velr.open() as db:
    with db.exec_one("RETURN 'Alice' AS name") as table:
        ipc = table.to_ipc_bytes()
    db.bind_arrow_ipc("_people", ipc)
    with db.exec_one("UNWIND BIND('_people') AS p RETURN p.name AS name") as table:
        assert table.to_records() == [{"name": "Alice"}]
```

### Graph introspection and explain

```python
from velr.driver import Velr

with Velr.open() as db:
    db.run("CREATE (:Person {name:'Alice'})")
    with db.exec_one("SHOW CURRENT GRAPH SHAPE") as table:
        shape = table.to_records()
        assert shape
    print(db.to_records("SHOW INDEXES"))
    assert db.to_records("CALL db.labels() YIELD label RETURN label") == [{"label": "Person"}]
    with db.explain("MATCH (p:Person) RETURN p.name AS name") as trace:
        print(trace.to_compact_string())
```

Graph shape reports labels, relationship types, properties, observed types, and
counts. Explain traces can also be copied with `snapshot()`. Copied snapshots
and strings survive handle closure. Explain methods accept query text without
parameter bindings or query-control options. `explain()` does
not execute the query; `explain_analyze()` executes it, including writes.
`SHOW INDEXES` lists indexes and their configuration. `CALL db.labels()`,
`CALL db.relationshipTypes()`, and `CALL db.propertyKeys()` list graph catalog
names. Each supports `YIELD` and subsequent query clauses.

## Compatibility and maintenance

`open_readonly(path)` requires an existing supported database and never creates,
initializes, migrates, or repairs it. Use `schema_version()`,
`current_schema_version()`, and `needs_migration()` to inspect compatibility.
New databases use schema version 11. Supported older databases at versions
3 through 9 require explicit migration before graph queries and writes. Run `migrate()` or `MIGRATE DATABASE` on a
writable connection, then reopen any read-only connections. Opening a database
never migrates it.
Databases created before Velr 0.2.14 must be recreated by re-importing source
data. Use the migration check below before querying an older database.

Run `vacuum()` or `VACUUM DATABASE` on a writable current-schema database outside
a transaction when you need compaction. See the
[migration and maintenance examples](https://velr-ai.github.io/velr-python-driver/velr/guide.html#schema-migration).

```python
from velr.driver import Velr

with Velr.open("maintenance.db") as db:
    print(db.schema_version(), db.current_schema_version())
    if db.needs_migration():
        report = db.migrate()
        print(report.status, report.from_version, report.to_version)
    db.vacuum()
```

Choose a writable database directory for persistent deployments and close all
handles when a unit of work ends. The installed wheel supplies the runtime;
there is no separate server configuration for this embedded driver. Size memory
for collected results and dataframe exports, or process rows incrementally.

## Reference and support

- [Driver source](https://github.com/velr-ai/velr-python-driver).
- [API reference](https://velr-ai.github.io/velr-python-driver/velr/driver.html), generated from public driver docstrings.
- Installed reference: `help(Velr)`, `help(Velr.exec_one)`, `help(velr.guide)`, and `help(velr.query_language)` after importing those modules.
- [Driver guide](https://velr-ai.github.io/velr-python-driver/velr/guide.html) and [query language reference](https://velr-ai.github.io/velr-python-driver/velr/query_language.html).
- [Python examples](https://github.com/velr-ai/velr-python-examples).
- Included scripts: [basic query](https://velr-ai.github.io/velr-python-driver/examples/basic_query.py) and
  [transactions and savepoints](https://velr-ai.github.io/velr-python-driver/examples/txn_savepoints.py).
- [Questions and community](https://github.com/velr-ai/velr/discussions).
- [Bug reports and feature requests](https://github.com/velr-ai/velr/issues).

## License

The Python binding source is MIT licensed. Bundled runtime binaries may be used
and redistributed unmodified under their separate runtime license. See
[LICENSE](https://velr-ai.github.io/velr-python-driver/LICENSE) and [LICENSE.runtime](https://velr-ai.github.io/velr-python-driver/LICENSE.runtime).
