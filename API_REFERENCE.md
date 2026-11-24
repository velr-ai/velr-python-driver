# Python API reference

This reference contains the public docstrings available through Python's `help()`.
Start with the [quick start](README.md#quick-start), then consult the
[usage guide](GUIDE.md) and [query language reference](QUERY_LANGUAGE.md).

## velr

Python package for the embedded Velr property-graph database.

Import the public connection, exception, value, and result types from ``velr``::

    from velr import Velr, VelrError

    with Velr.open() as db:
        with db.exec_one("RETURN 1 AS answer") as table:
            print(table.to_records())

Use parameterized ``run``, ``exec_one``, and ``exec`` methods for application
queries. A connection or result context closes its handle; a transaction
context rolls back unless explicitly committed. Scoped savepoint contexts
release on success and roll back on an exception. Copied cells, paths, records,
dataframes, Arrow exports, and explain snapshots survive parent handle closure.

Keep active result/transaction handles on their creating thread. An idle
connection with no live children can move between threads, but operations on
one connection must not overlap. ``CancellationToken`` can be signaled from
another thread; query deadlines use seconds, with ``None`` meaning no deadline
and ``0`` expiring immediately.

Database/query failures raise ``VelrError`` during execution or
result consumption. Catch its subclasses for query, constraint, contention,
read-only, migration, handle-state, and query-stop failures. ``category`` and
``detail`` identify classified failures; optional ``phase``, ``explanation``,
``hint`` and ``position`` provide context. Positions are ``ErrorPosition`` named
tuples with one-based ``line`` and display-cell ``column`` coordinates, preserving
ordinary tuple operations. ``report`` and ``str(error)`` retain the full
diagnostic; ``message`` retains its explanation behavior. ``status_code`` is the
native status, and ``code`` retains the cancellation/deadline strings. Invalid
host values, optional packages, and file operations can raise their standard
Python exceptions.

``velr.guide`` provides extended workflows and ``velr.query_language`` documents
supported syntax/functions. Optional Arrow, pandas, Polars, and NumPy methods
state their package prerequisites in their API documentation.

Package-level names are loaded on first access. Importing ``velr`` or its
reference modules does not load the native runtime or optional dataframe
packages. Accessing a driver name loads ``velr.driver`` and can report missing
runtime requirements. Existing ``from velr.driver import ...`` imports remain
supported and return the same objects.

## velr.driver

Python API for the embedded Velr graph database.

Get started with a parameterized write and ordinary Python records::

    from velr import Velr

    with Velr.open() as db:
        db.run("CREATE (:Person {name: $name})", params={"name": "Alice"})
        with db.exec_one("MATCH (p:Person) RETURN p.name AS name") as table:
            assert table.to_records() == [{"name": "Alice"}]

Execution and values
--------------------
``Velr.run`` discards results, ``Velr.exec_one`` requires exactly one result
table, and ``Velr.exec`` streams multiple tables. ``VelrTx`` offers the same
execution choices. Use ``Table.to_rows`` for row lists, ``Table.to_records``
for named records, and ``Table.iter_rows`` for incremental ``Cell`` iteration.
Connection collection/export helpers return the last result table of a script.

Query parameters are supplied separately through ``params``. Keys omit the
leading ``$`` used in Cypher text. Supported values are ``None``, bool, signed
64-bit int, finite float, str, list/tuple, and dict with string keys. Use the
query-language reference for Cypher functions and syntax; these are separate
from Python methods. ``max_result_rows`` limits each table's returned rows,
not query execution time or write effects. ``timeout`` is measured in seconds
and covers execution and result fetching; omit it or pass ``None`` for no
deadline, and use ``0`` to expire immediately. ``CancellationToken`` can be signaled
from another thread and remains canceled once signaled.

Ownership and errors
--------------------
Use context managers or explicitly close connections, transactions, streams,
tables, row iterators, savepoints, and explain traces. Closing a parent closes
its children. Stream-produced tables require the stream to remain open;
``exec_one`` tables require their connection or transaction to remain open.
A transaction context rolls back unless ``commit`` is called explicitly.
A savepoint context releases on success and rolls back on an exception;
closing an active scoped savepoint instead rolls back and releases it.
Copied ``Cell`` data, collected Python rows, dataframe/Arrow exports, and
explain snapshots remain usable after their producing handles close.

Catch ``VelrError`` around execution and full result consumption. ``str(error)``
preserves the diagnostic; subclasses and ``category``/``detail`` support
programmatic handling. ``error.status_code`` exposes native statuses, while
``error.code`` retains cancellation and deadline strings. Invalid Python
arguments and missing optional packages can raise standard Python exceptions;
row callback exceptions propagate. Cleanup failures can replace a block
exception. Operations on one connection must not overlap. An idle connection
with no live child handles can move to another thread; active result and
transaction handles must stay on their creating thread. Only one transaction
can be active on a connection. Use separate connections for independent work.

References and integrations
---------------------------
``velr.guide`` contains full usage examples for query controls, transactions,
search, Arrow/dataframes, introspection, and maintenance.
``velr.query_language`` contains the shared syntax and function reference.
Both modules are available through ``help`` and generated API documentation.
Fulltext and vector indexes require a file-backed database. Dataframe exports
and Arrow binders require the optional packages documented on those methods.
Import these APIs from ``velr`` in scripts and applications; imports from
``velr.driver`` remain supported and expose the same objects.
With a compatible runtime, use ``MATCH ... SEARCH ...`` for indexed fulltext or
vector searches; ``velr.query_language`` documents syntax, scores, and filters.

### velr.driver.Path

A materialized Cypher path returned by ``to_rows()`` or ``to_records()``.

Attributes
----------
elements:
    Alternating tuple of node and relationship dictionaries, beginning and
    ending with a node. A zero-length path contains one node. Entity fields
    match standalone node and relationship results, including ``properties``.

Paths retain this type even when nested in returned lists or maps. An ordinary
Cypher list of entities remains a Python list. ``Cell.as_python(parse_json=True)``
decodes a path's JSON representation as a list; use table/connection
materialization methods when the ``Path`` distinction matters. Arrow exports
represent structured values as JSON strings.

The tuple and its entity dictionaries survive result/connection closure.
Fields cannot be reassigned, but contained dictionaries are ordinary mutable
Python values. Instances contain copied data, not live graph handles.

Fields:

- `elements: tuple[dict[str, Any], ...]`

### velr.driver.ErrorPosition

A source location in the complete submitted Cypher query.

Parameters
----------
line:
    One-based source line.
column:
    One-based column measured in Unicode display cells, matching the rendered
    diagnostic. This can differ from byte or editor character offsets.

Values are immutable, owned Python data. They support ``position.line`` and
``position.column`` as well as tuple unpacking, indexing, and equality with
``(line, column)``. Unknown or ambiguous error positions are represented by
``None`` instead of an ``ErrorPosition``.

Fields:

- `line: int`
- `column: int`

### velr.driver.VelrError

Base exception for Velr query, database, and result-handle failures.

Catch this exception around both execution and result consumption. Errors can
surface when a query starts, a stream advances, or rows are read. ``str(error)``
and ``error.args`` retain the original human-readable diagnostic. Catch a
subclass or inspect ``category`` and ``detail`` to handle failures without
matching diagnostic wording.

Attributes
----------
category:
    Stable runtime category, such as ``"ConstraintVerificationFailed"``, or
    ``None`` when unavailable. New categories may be added by newer runtimes.
detail:
    Stable category-specific reason, such as ``"DeleteConnectedNode"``, or
    ``None``. Applications must allow unknown detail values.
phase:
    ``"compile"`` or ``"runtime"`` for classified query failures, otherwise
    ``None``. A failure's phase is independent of when it is fetched.
message:
    Human-readable explanation. Older runtimes provide the original diagnostic
    when a separate explanation is unavailable. Wording can change.
explanation:
    Alias of ``message`` for the concise explanation, consistent with the
    remote Python client. Older runtimes can supply the full diagnostic.
report:
    Complete rendered diagnostic, identical to ``str(error)``. Preserves
    the original exception arguments independently of the explanation.
hint:
    Optional human-readable remediation text, or ``None``.
position:
    Optional :class:`ErrorPosition` in the complete submitted query, with
    ``line`` and ``column`` attributes. Tuple indexing, unpacking, and equality
    with ``(line, column)`` remain supported. Both coordinates are one-based;
    columns count Unicode display cells. ``None`` means the runtime cannot
    identify a single source position.
retryable:
    Whether the runtime identifies a potentially transient condition.
    ``True`` does not guarantee that repeating a write, script, or transaction
    is safe. Resolve the condition and retry at the documented operation or
    transaction boundary. Unclassified failures default to ``False``.
status_code:
    Numeric C ABI status for a native failure, or ``None`` for Python-generated
    failures. This is separate from the cancellation-only ``code`` property.
sqlite_errorcode:
    SQLite's extended numeric result code when available, otherwise ``None``.
sqlite_errorname:
    SQLite's symbolic result-code name when available, otherwise ``None``.
io_kind:
    Portable filesystem failure kind, such as ``"not_found"`` or
    ``"permission_denied"``, when supplied by the runtime.
os_code:
    Original platform error number for a filesystem failure, or ``None``.
operation:
    Failed filesystem operation, such as ``"open"`` or ``"create"``, when known.
path:
    Database path associated with a filesystem failure, or ``None``. Native
    filesystem failures retain a corresponding ``OSError`` in ``__cause__``
    when an original platform error number is available.

Older runtimes without structured diagnostics still raise ``VelrError`` and
expose native ``status_code`` and cancellation/deadline subclasses. Other
structured fields remain unavailable rather than being inferred from text.

Invalid Python arguments can raise ``TypeError`` or ``ValueError``; optional
packages and file operations retain their ordinary Python exceptions.

#### velr.driver.VelrError.__init__

```python
__init__(*args: object, category: str | None=None, detail: str | None=None, phase: str | None=None, message: str | None=None, hint: str | None=None, position: ErrorPosition | tuple[int, int] | None=None, retryable: bool=False, status_code: int | None=None, sqlite_errorcode: int | None=None, sqlite_errorname: str | None=None, io_kind: str | None=None, os_code: int | None=None, operation: str | None=None, path: str | None=None) -> None
```

Create an exception from diagnostic ``args`` and optional metadata.

``args`` are passed unchanged to ``RuntimeError``. The keyword
arguments initialize the correspondingly named attributes documented on
this class; omitted fields use ``None`` or ``False``. ``message`` defaults
to ``str(error)``. ``position`` accepts ``ErrorPosition`` or a two-item
tuple and is stored as ``ErrorPosition``. Subclasses supply a category
when one is not supplied.
The exception owns its strings and metadata independently of native handles.

#### velr.driver.VelrError.explanation

```python
explanation: str
```

Return the human-readable explanation, an alias of ``message``.

The original rendered diagnostic remains available in ``report`` and
``str(error)``. This property uses the same name as the remote client.

#### velr.driver.VelrError.report

```python
report: str
```

Return the complete rendered diagnostic, identical to ``str(error)``.

Preserves the original exception arguments and display even when a
separate concise ``explanation`` is available.

#### velr.driver.VelrError.code

```python
code: str | None
```

Return ``"query.cancelled"`` or ``"query.deadline_exceeded"`` for a stop.

Other errors return ``None``. Inspect ``category`` and ``detail`` for
classified failures, and ``status_code`` for the numeric C ABI status.

### velr.driver.QueryError

Base exception for classified Cypher compilation and execution failures.

Includes failures caused by query text, supplied values, and graph data.
Inspect ``phase`` to distinguish compile-time from runtime failures, and
``category``/``detail`` for the stable reason. Metadata and constructor
arguments are inherited from :class:`VelrError`.

### velr.driver.CypherSyntaxError

The query violates the supported Cypher syntax or static query rules.

### velr.driver.CypherSemanticError

The query violates a Cypher semantic rule for its supplied values or graph.

### velr.driver.CypherTypeError

A Cypher value has an unsupported type or cannot be converted.

### velr.driver.ParameterError

A query parameter is missing or a Cypher argument lies outside its domain.

Invalid Python host arguments can instead raise ordinary ``TypeError`` or
``ValueError`` before execution. Inspect ``category`` and ``detail`` to
distinguish missing parameters from argument-domain errors.

### velr.driver.ProcedureNotFoundError

A Cypher ``CALL`` names a procedure unavailable on this connection.

### velr.driver.EntityNotFoundError

A query refers to a graph entity that no longer exists.

### velr.driver.ConstraintError

A query violates a graph constraint, such as deleting a connected node.

### velr.driver.CypherArithmeticError

A Cypher arithmetic operation is undefined or overflows.

### velr.driver.DatabaseBusyError

A database operation cannot proceed because of database contention.

Inspect ``retryable`` and ``sqlite_errorcode`` before deciding whether and at
what boundary to retry. This exception does not guarantee safe replay.

### velr.driver.ReadOnlyError

An operation attempted a write through a read-only database connection.

### velr.driver.MigrationRequiredError

The database requires explicit migration before the requested operation.

### velr.driver.HandleStateError

A handle is closed, on the wrong thread, or already serving an operation.

Python-generated handle guards have ``status_code is None``. The original
diagnostic is preserved, and ``detail`` identifies the failed guard.

### velr.driver.QueryCancelled

Query execution or result consumption stopped after cancellation.

The inherited ``code`` property remains ``"query.cancelled"``.

### velr.driver.DeadlineExceeded

Query execution or result consumption exceeded its deadline.

Catch this as either ``VelrError`` or ``TimeoutError``. The inherited ``code``
property remains ``"query.deadline_exceeded"``.

### velr.driver.CancellationToken

Signal query cancellation from any thread.

Pass the token as ``cancellation=`` when starting a query. Cancelling it
signals every query using it, including deferred table and row reads.
A cancelled token stays cancelled and causes later queries using it to stop.
Create a fresh token for independent work. Construct it with no arguments.
``cancel()`` requests a stop without waiting for executing queries to finish;
a query may finish before observing the signal. Check both execution and fetch
errors for ``VelrError.code == "query.cancelled"``. The token is thread-safe;
connections and active result handles remain subject to their own thread rules.

#### velr.driver.CancellationToken.cancel

```python
cancel() -> None
```

Signal a stop and return ``None``. Repeated calls are safe.

Calls may come from any thread. The query or result reader that observes
the stop raises :class:`VelrError` with ``code == "query.cancelled"``.
This call does not wait for completion or invalidate previously copied results.

### velr.driver.MigrationReport

Report returned by :meth:`Velr.migrate`.

Attributes
----------
from_version:
    Schema version observed before migration.
to_version:
    Schema version after migration.
status:
    ``"already_current"`` or ``"migrated"``.
steps:
    Diagnostic migration step identifiers applied in order; their spelling
    can change between runtime versions.

The report is owned Python data and remains usable after the connection closes.

Fields:

- `from_version: int`
- `to_version: int`
- `status: str`
- `steps: List[str]`

### velr.driver.Cell

One cell value in a result row.

Attributes
----------
ty:
    Value kind: ``0`` null, ``1`` bool, ``2`` signed 64-bit integer,
    ``3`` double, ``4`` text, or ``5`` JSON.
i64:
    Integer payload used for BOOL and INT64 cells.
f64:
    Floating-point payload used for DOUBLE cells.
data:
    Raw bytes for TEXT and JSON cells.

Cell payloads are copied Python data and remain valid after the result handles
close. Prefer ``as_python()`` over interpreting type codes directly.

Fields:

- `ty: int`
- `i64: int`
- `f64: float`
- `data: bytes`

#### velr.driver.Cell.as_python

```python
as_python(*, decode_text: bool=True, parse_json: bool=False, encoding: str='utf-8', errors: str='strict')
```

Convert this copied cell to a Python value.

Scalar cells return ``None``, ``bool``, ``int``, or ``float``. Python integers
preserve the full signed 64-bit result. With the defaults ``decode_text=True``
and ``parse_json=False``, TEXT and JSON return strings. ``decode_text=False``
returns copied bytes for TEXT and unparsed JSON.

``parse_json=True`` decodes JSON cells into Python values even when
``decode_text=False``; it does not parse ordinary TEXT cells. Lists/maps become
lists/dicts, and graph entities use their JSON data representation. Paths are
JSON lists here; table ``to_rows()``/``to_records()`` preserve :class:`Path`.

``encoding="utf-8"`` and ``errors="strict"`` control text decoding. Invalid
encoded text or JSON raises its Python decoding exception; unknown codecs or
error handlers raise their standard Python exceptions. The result is independent
of the row iterator and remains usable after handles close.

### velr.driver.VectorEmbeddingField

One property value supplied to an embedding callback.

``name`` is the property name, or ``None`` for query text. ``value`` is the
Python value (including ``bytes`` for byte properties); complex/temporal
values use their JSON representation. ``value_type`` identifies the Cypher
value type: ``null``, ``bool``, ``int64``, ``double``, ``string``, ``date``,
``local_time``, ``zoned_time``, ``local_datetime``, ``zoned_datetime``,
``duration``, ``point``, ``geometry``, ``geography``, ``list``, ``vector``, or
``bytes``. ``value_json`` is the decoded JSON representation, and ``display``
is a human-readable rendering. These copied values can be retained after the
callback returns. Fields follow ``ON EACH [...]`` order; stored-vector
indexes do not invoke the callback.

Fields:

- `name: str | None`
- `value: Any`
- `value_type: str`
- `value_json: Any`
- `display: str`

### velr.driver.VectorEmbeddingInput

One input in a vector embedding callback batch.

``index_name`` names the index and ``dimensions`` is the required output
vector length. ``purpose`` is ``"index_entity"`` or ``"query"``.
``entity_kind`` is ``"node"``, ``"relationship"``, or ``None``; ``entity_id``
is an integer identity when present, otherwise ``None``. ``fields`` contains
:class:`VectorEmbeddingField` objects in ``ON EACH [...]`` order. Query text
has one unnamed string field. Return one finite numeric vector per input,
in the same order. Inputs and their fields are copied Python data and can
be retained after the callback returns. Stored-vector indexes accept query
vectors directly without invoking an embedding callback.

Fields:

- `index_name: str`
- `dimensions: int`
- `purpose: str`
- `entity_kind: str | None`
- `entity_id: int | None`
- `fields: list[VectorEmbeddingField]`

### velr.driver.ExplainPlanMeta

Copied metadata for one explain plan.

``plan_id`` identifies the plan, ``cypher`` is its query text, and
``step_count`` is the number of steps. Instances remain usable after the
trace is closed.

Fields:

- `plan_id: str`
- `cypher: str`
- `step_count: int`

### velr.driver.ExplainStepMeta

Copied metadata for one explain step.

``step_no`` is its displayed step number; ``group_id`` and ``op_index``
identify its group and operation. ``phase``, ``title``, and ``source``
describe the step; ``note`` is optional. ``statement_count`` gives the number
of associated statements. Treat diagnostic identifiers and wording as
informational rather than stable application identifiers.

Fields:

- `step_no: int`
- `group_id: str`
- `op_index: str`
- `phase: str`
- `title: str`
- `source: str`
- `note: str | None`
- `statement_count: int`

### velr.driver.ExplainStatementMeta

Copied metadata for one explain statement.

``stmt_id`` identifies the statement, ``kind`` describes it, ``sql`` contains
its SQL text, ``note`` is optional, and ``sqlite_plan_count`` is the number of
available SQLite plan-detail lines. Diagnostic text is version-specific.

Fields:

- `stmt_id: str`
- `kind: str`
- `sql: str`
- `note: str | None`
- `sqlite_plan_count: int`

### velr.driver.ExplainStatement

An owned explain statement snapshot.

``meta`` is :class:`ExplainStatementMeta`; ``sqlite_plan`` is a list of plan
detail strings. The snapshot remains usable after the trace is closed.

Fields:

- `meta: ExplainStatementMeta`
- `sqlite_plan: list[str]`

### velr.driver.ExplainStep

An owned explain step snapshot.

``meta`` is :class:`ExplainStepMeta` and ``statements`` contains
:class:`ExplainStatement` snapshots, in trace order.

Fields:

- `meta: ExplainStepMeta`
- `statements: list[ExplainStatement]`

### velr.driver.ExplainPlan

An owned explain plan snapshot.

``meta`` is :class:`ExplainPlanMeta` and ``steps`` contains
:class:`ExplainStep` snapshots, in trace order.

Fields:

- `meta: ExplainPlanMeta`
- `steps: list[ExplainStep]`

### velr.driver.ExplainTrace

An EXPLAIN or EXPLAIN ANALYZE trace.

An explain trace can be traversed incrementally using the ``*_meta`` and
``*_count`` methods, or fully materialized with ``snapshot()``.

Keep the parent connection or transaction open while reading the trace.
Explicitly closing the parent closes the trace, even if Python still holds
a reference to it. Keep the trace on its creating thread.

Use ``close()`` when you are done, or use the trace as a context manager.

All plan/step/statement/detail indexes are zero-based. Out-of-range indexes and
operations on a closed trace raise ``VelrError``; negative or noninteger
indexes can also raise Python range/type errors. ``snapshot()`` and rendered
strings/bytes are owned Python data that survive trace closure. Obtain traces
from ``Velr.explain()`` or ``VelrTx.explain()`` rather than constructing them.

#### velr.driver.ExplainTrace.close

```python
close() -> None
```

Close the explain trace handle.

Repeated calls are harmless. Existing snapshots and rendered data remain usable.

#### velr.driver.ExplainTrace.__enter__

```python
__enter__() -> 'ExplainTrace'
```

Return this handle for use in a ``with`` block.


#### velr.driver.ExplainTrace.__exit__

```python
__exit__(exc_type, exc, tb)
```

Close this handle on block exit without suppressing the exception.


#### velr.driver.ExplainTrace.plan_count

```python
plan_count() -> int
```

Return the nonnegative number of top-level plans.

Raises ``VelrError`` if this trace or its producing connection/transaction is
closed. Plan indexes passed to other methods range from zero to this count
minus one.

#### velr.driver.ExplainTrace.plan_meta

```python
plan_meta(plan_idx: int) -> ExplainPlanMeta
```

Return an owned ``ExplainPlanMeta`` for zero-based ``plan_idx``.

The metadata remains usable after the trace closes. Invalid indexes and
closed-handle access raise ``VelrError``; incompatible index types or negative
values may instead raise ``TypeError`` or ``OverflowError``.

#### velr.driver.ExplainTrace.step_count

```python
step_count(plan_idx: int) -> int
```

Return the number of steps in the zero-based plan ``plan_idx``.

Use indexes in ``0..plan_count()-1``. Invalid indexes and closed traces raise
``VelrError``; incompatible or negative index arguments may raise Python
``TypeError`` or ``OverflowError``.

#### velr.driver.ExplainTrace.step_meta

```python
step_meta(plan_idx: int, step_idx: int) -> ExplainStepMeta
```

Return owned ``ExplainStepMeta`` for zero-based plan and step indexes.

``step_idx`` must be below ``step_count(plan_idx)``. The returned metadata
survives trace closure. Invalid indexes/closed handles raise ``VelrError``;
negative or incompatible indexes may raise Python range/type errors.

#### velr.driver.ExplainTrace.statement_count

```python
statement_count(plan_idx: int, step_idx: int) -> int
```

Return the statement count for zero-based ``plan_idx`` and ``step_idx``.

Both indexes must identify existing entries. Invalid indexes and closed handles
raise ``VelrError``; negative or incompatible indexes may raise Python
range/type errors.

#### velr.driver.ExplainTrace.statement_meta

```python
statement_meta(plan_idx: int, step_idx: int, stmt_idx: int) -> ExplainStatementMeta
```

Return owned ``ExplainStatementMeta`` for zero-based plan, step, and statement indexes.

``stmt_idx`` must be below ``statement_count(plan_idx, step_idx)``. The result
survives trace closure. Invalid indexes and closed handles raise ``VelrError``;
negative or incompatible indexes may raise Python range/type errors.

#### velr.driver.ExplainTrace.sqlite_plan_count

```python
sqlite_plan_count(plan_idx: int, step_idx: int, stmt_idx: int) -> int
```

Return the number of diagnostic plan-detail lines for the selected statement.

All three indexes are zero-based and must identify existing entries. Raises
``VelrError`` for invalid indexes or a closed trace; negative or incompatible
indexes may raise Python range/type errors.

#### velr.driver.ExplainTrace.sqlite_plan_detail

```python
sqlite_plan_detail(plan_idx: int, step_idx: int, stmt_idx: int, detail_idx: int) -> str
```

Return one owned diagnostic string for zero-based plan, step, statement, and detail indexes.

``detail_idx`` must be below ``sqlite_plan_count(plan_idx, step_idx, stmt_idx)``.
The string survives trace closure. Invalid indexes and closed handles raise
``VelrError``; negative or incompatible indexes may raise Python range/type errors.
Diagnostic wording is informational and may vary between runtime versions.

#### velr.driver.ExplainTrace.sqlite_plan_details

```python
sqlite_plan_details(plan_idx: int, step_idx: int, stmt_idx: int) -> list[str]
```

Return all owned diagnostic strings for the selected statement in detail order.

Indexes are zero-based. The returned list survives trace closure. Invalid
indexes and closed handles raise ``VelrError``; negative or incompatible index
arguments may raise Python range/type errors.

#### velr.driver.ExplainTrace.snapshot

```python
snapshot() -> list[ExplainPlan]
```

Return all plans as an owned list of ``ExplainPlan`` snapshots.

Each snapshot contains its metadata, ordered steps, statements, and diagnostic
plan details. The list remains usable after the trace closes. Closed-handle
access or a failure to retrieve a trace entry raises ``VelrError``.

#### velr.driver.ExplainTrace.compact_len

```python
compact_len() -> int
```

Return the compact rendering length in UTF-8 bytes, excluding a terminator.

This does not render a string for the caller. Raises ``VelrError`` if the trace
is closed or the rendering length cannot be obtained.

#### velr.driver.ExplainTrace.to_compact_bytes

```python
to_compact_bytes() -> bytes
```

Render the complete trace as owned UTF-8 ``bytes``.

The bytes survive trace closure. Raises ``VelrError`` for a closed trace or
rendering failure. Use ``write_compact`` for a binary file-like destination.

#### velr.driver.ExplainTrace.to_compact_string

```python
to_compact_string() -> str
```

Render the complete trace as an owned Python ``str``.

The string survives trace closure. Raises ``VelrError`` for a closed trace or
rendering failure, including invalid UTF-8.

#### velr.driver.ExplainTrace.write_compact

```python
write_compact(out) -> None
```

Write the compact rendering to a file-like object.

The destination must accept bytes and remains open. I/O errors propagate;
this method returns ``None``.

### velr.driver.Velr

A Velr database connection.

Use ``open()`` for in-memory or file-backed connections, ``open_readonly()``
for existing files, or ``create()`` to initialize a new database with options.

A query may return zero or more result tables. Choose among:

- ``exec()`` to stream tables
- ``exec_one()`` when exactly one table is expected
- ``run()`` when results can be ignored
- ``to_pyarrow()``, ``to_pandas()``, or ``to_polars()`` for convenience
  conversion of the last result table

Use ``begin_tx()`` to open the connection's single active transaction. A
transaction context rolls back unless you explicitly commit; there is no
commit-on-success transaction callback helper. Use savepoints for nested scopes.

Close the connection explicitly or use a ``with`` block. Closing it closes
child handles on their creating thread. An idle connection can move between
threads, but overlapping operations raise ``VelrError``. Keep transactions,
results, and other active handles on the thread that created them; finish
them before moving the connection. Obtain handles from the public factory
methods rather than calling the handle classes' constructors directly.

#### velr.driver.Velr.open

```python
@classmethod
open(path: Optional[str]=None, *, extensions: Sequence[Extension]=()) -> 'Velr'
```

Open a writable in-memory or file-backed Velr database.

Parameters
----------
path:
    A NUL-free Unicode ``str`` path, or ``None`` (the default) or ``":memory:"``
    for a private in-memory database. Convert filesystem path objects with
    ``str(path)`` before calling. A file path opens an existing database or
    creates one; its parent directory must exist.
    An uninitialized SQLite file containing tables or other schema objects
    is rejected without changing its contents or journal mode.
extensions:
    Native extensions to enable on this connection. For GDS, install
    the matching ``velr[gds]`` release and pass
    ``extensions=[velr_gds.Gds()]``. Extensions load in the current
    process and make their procedures available to ordinary Cypher.
    The named graph catalog and its memory budget belong to this
    connection. This option is available on ``open()``.

Returns
-------
Velr
    Caller-owned connection. Close it explicitly or use it in a ``with`` block.

Opening does not migrate supported older formats. Inspect ``needs_migration()``
and explicitly migrate through a writable connection before graph queries or
transactions. Use ``current_schema_version()`` rather than assuming a version.

Database, schema, and extension loading failures raise ``VelrError``.
Invalid Python path objects can raise standard errors such as
``AttributeError`` when not encodable. Invalid Unicode raises
``UnicodeEncodeError``. Paths must not contain NUL.

#### velr.driver.Velr.create

```python
@classmethod
create(path: Optional[str]=None, *, inline_edge_properties: Sequence[str]=()) -> 'Velr'
```

Create a new database and return its open connection.

Pass a new NUL-free ``str`` file path with an existing parent directory, or
use ``None`` (the default) or ``":memory:"`` for a private in-memory database.
Convert filesystem path objects to ``str`` first. An existing destination
raises ``VelrError`` and is never overwritten. Close the returned
connection or use it in a ``with`` block.

``inline_edge_properties`` is an ordered sequence containing zero or one
nonempty, case-sensitive relationship property name. An omitted or empty
sequence leaves inline edge properties disabled. The selection applies
to all relationship types, persists across file reopening, and is fixed at
creation. Use ordinary Cypher reads and writes for all values of that
property; the selection does not restrict supported value types or sizes.
Inspect it with ``inline_edge_properties()``. Requires a runtime providing
database creation (Velr 0.2.62 or newer).

Invalid, empty, or NUL-containing names, more than one name, unsupported
runtime features, and creation failures raise ``VelrError``. A string passed
in place of a sequence, or a non-string item, raises ``TypeError``. Invalid
path objects and Unicode may raise standard Python exceptions.

#### velr.driver.Velr.inline_edge_properties

```python
inline_edge_properties() -> list[str]
```

Return the database's inline edge property names in configured order.

The selection remains available after reopening the database. Returns
an empty list when no property is selected. The caller owns the returned
list, which remains usable after the connection closes. Raises ``VelrError``
if the connection is closed, busy, or the runtime cannot report the selection.
Requires the inspection API provided by Velr 0.2.62 or newer.

#### velr.driver.Velr.open_readonly

```python
@classmethod
open_readonly(path: str) -> 'Velr'
```

Open an existing file-backed database without modifying it.

``path`` must be a NUL-free Unicode ``str`` naming an existing, initialized
Velr database. Convert filesystem path objects with ``str(path)``. Opening never
creates, initializes, migrates, or repairs the file. Supported older formats
require explicit migration through a writable connection before graph queries
or transactions; reopen read-only connections after migration.

Returns a caller-owned ``Velr`` connection. Close it or use a context manager.
Missing files, unsupported formats, or open failures raise ``VelrError``.
Unsupported path objects can raise standard errors such as ``AttributeError``;
invalid Unicode raises ``UnicodeEncodeError``. NUL-containing paths are
unsupported and are not validated by this method before opening.

#### velr.driver.Velr.schema_version

```python
schema_version() -> int
```

Return this connection's database schema version as an integer.

Compare with ``current_schema_version()`` or use ``needs_migration()`` before
explicit maintenance. Raises ``VelrError`` for a closed connection or a
runtime that cannot report the version.

#### velr.driver.Velr.current_schema_version

```python
current_schema_version() -> int
```

Return the current schema version supported by this runtime.

Raises ``VelrError`` if the connection is closed or its runtime cannot report
the current version.

#### velr.driver.Velr.needs_migration

```python
needs_migration() -> bool
```

Return ``True`` when this database is on an older supported schema version.

With the current runtime, supported older formats require explicit
migration before graph queries or transactions.
Raises ``VelrError`` for a closed connection or a runtime that cannot
report migration state.

#### velr.driver.Velr.migrate

```python
migrate() -> MigrationReport
```

Explicitly migrate this database to the current schema version.

Opening a supported older database does not migrate it. Call this method,
or run ``MIGRATE DATABASE``, when maintenance code intentionally wants to
apply the pending schema migration.

Migration requires a writable connection outside any active transaction.
Opening never applies migrations. Reopen existing read-only connections
after the file is migrated through another connection.

Returns :class:`MigrationReport`, including ``already_current`` when no change
is needed. Raises ``VelrError`` for read-only connections, active transactions,
unsupported migration, a closed connection, or migration failure.

#### velr.driver.Velr.vacuum

```python
vacuum() -> None
```

Compact a file-backed database.

This is an explicit maintenance operation for file-backed databases
after large deletes or prune operations. It must run on a writable
current-schema connection outside any open transaction.
Returns ``None``. A closed connection, unmet requirements, or compaction
failure raises ``VelrError``.

#### velr.driver.Velr.close

```python
close() -> None
```

Close the connection and its open child handles.

Uncommitted transactions roll back. Streams, tables, row iterators, and
explain traces associated with the connection close. Copied cells, records,
Arrow/dataframe exports, and explain snapshots remain usable. Close active
children on their creating thread; cross-thread use or overlapping operations
raise ``VelrError``.
Repeated calls are harmless. Prefer ``with Velr.open(...) as db`` for
deterministic cleanup.

#### velr.driver.Velr.__enter__

```python
__enter__() -> 'Velr'
```

Return this handle for use in a ``with`` block.


#### velr.driver.Velr.__exit__

```python
__exit__(exc_type, exc, tb)
```

Close this handle on block exit without suppressing the exception.


#### velr.driver.Velr.register_vector_embedder

```python
register_vector_embedder(name: str, embedder: Callable[[list[VectorEmbeddingInput]], Sequence[Sequence[float]]]) -> None
```

Register a named embedding callback for vector indexes.

Embedder-backed vector indexes refer to this name with
``OPTIONS { indexConfig: { embedder: 'name' } }``. Velr calls the
callback for index creation/recovery, indexed writes or their commit, and
text queries supplied to node/relationship vector procedures. Registration
itself can request embeddings before returning. Register separately on each
connection whose operations need embeddings, including after reopening;
registration is not persisted with the database. If an embedder-backed index
has pending transaction updates, commit or roll back before searching it.
``indexConfig`` also carries vector dimensions, metric, cache policy,
optional embedder fingerprint metadata, and HNSW tuning keys.

Stored-vector indexes, such as ``CREATE VECTOR INDEX ... ON
(n.embedding)``, do not use a registered embedder. They index the
vector/list property directly and vector queries pass a numeric
vector/list.

The callback receives a list of :class:`VectorEmbeddingInput` and must
return one vector per input, in the same order. Each vector must contain
exactly ``input.dimensions`` finite numeric values convertible to ``float``.
It runs synchronously on the thread executing the query. Do not use or
close this connection or its handles from inside the callback.

Callbacks must return synchronously; coroutines and awaitable results are
not awaited. Perform database work on a suitable application thread when
embedding calls would block an event loop. Inputs/fields are copied and can
be retained.

Registration returns ``None`` and keeps the callback alive while the connection
is open. Use the same name to replace a callback. Invalid names/callables raise
``TypeError`` or ``ValueError``; registration and callback execution failures
raise ``VelrError`` during the corresponding operation. When the Python
callback raises, its original exception is retained as ``error.__cause__``.

#### velr.driver.Velr.run

```python
run(cypher: str, *, max_result_rows: int | None=None, params: Mapping[str, Any] | None=None, timeout: float | None=None, cancellation: CancellationToken | None=None) -> None
```

Execute a query or script and discard all results.

``max_result_rows`` controls how many rows Velr emits while this method
drains result tables:

- ``None`` preserves the default behavior and drains all rows.
- ``0`` preserves table metadata but emits no rows.
- ``n`` emits at most ``n`` rows from each result table.

Existing Cypher ``LIMIT`` clauses still apply. The option caps emitted
result rows only; it does not make the query read-only, cancel
execution, or skip write side effects.

``params`` binds openCypher parameters out of band. Query text uses
``$name`` and mapping keys omit the leading ``$``. Supported values are
``None``, booleans, signed 64-bit integers, finite floats, strings,
lists/tuples, and mappings with string keys. Values are passed as Cypher
values, so strings are never interpreted as query text. Limit nested lists/maps to 64
levels. Convert other Python values to these accepted types; construct
temporal/spatial Cypher values in the query with bound strings or maps.

``cypher`` must be NUL-free Unicode text, optionally a semicolon-separated
script.

``timeout`` is a finite, nonnegative number of seconds (not a bool). Its single
deadline covers execution and any deferred table and row reads.
``None`` leaves it unset and ``0`` expires immediately. Pass a
``CancellationToken`` as ``cancellation`` to request a stop, including
from another thread. The call that observes the stop raises
``VelrError`` with code ``query.deadline_exceeded`` or ``query.cancelled``.

Raises
------
TypeError
    If the row cap is not an ``int`` or ``None``, ``params`` is not a
    mapping or ``None``, a parameter value has an unsupported type,
    ``timeout`` is not a number or ``None``, or ``cancellation`` is not
    a ``CancellationToken`` or ``None``.
ValueError
    If ``max_result_rows`` is negative, a parameter name is empty or
    starts with ``$``, an integer is outside the Cypher ``INTEGER``
    range, a float is not finite, nesting exceeds the supported limit,
    or ``timeout`` is negative, nonfinite, or outside the supported range.
OverflowError
    If the row cap exceeds ``2**64 - 1`` on supported 64-bit platforms.
AttributeError, UnicodeError
    If query text cannot be encoded as UTF-8; pass a Unicode ``str``.
VelrError
    If the runtime reports a query error or does not expose the
    query-options/parameter support required for requested row caps, parameters,
    deadlines, or cancellation.

#### velr.driver.Velr.execute

```python
execute(cypher: str, *, max_result_rows: int | None=None, params: Mapping[str, Any] | None=None, timeout: float | None=None, cancellation: CancellationToken | None=None) -> None
```

Alias for ``run`` with identical parameters, return value, and errors.

Executes the script, discards all result tables, and returns ``None``. See
``Velr.run`` for ``params``, per-table row caps, seconds-based ``timeout``,
``cancellation``, argument validation, and query-error behavior.

#### velr.driver.Velr.exec

```python
exec(cypher: str, *, max_result_rows: int | None=None, params: Mapping[str, Any] | None=None, timeout: float | None=None, cancellation: CancellationToken | None=None) -> 'Stream'
```

Execute a query and return a stream of result tables.

Tables produced by the returned stream are stream-scoped: they remain valid
while the stream is open, and closing the stream closes any still-open
tables produced by it.

Query errors raise ``VelrError`` when starting execution or consuming
tables and rows. Catch errors around the complete result-consumption block.

``max_result_rows`` caps rows emitted from each result table:

- ``None`` preserves the default behavior and emits all rows.
- ``0`` preserves table metadata, including column names, but emits no
  rows.
- ``n`` emits at most ``n`` rows from each result table.

Existing Cypher ``LIMIT`` clauses still apply. For example, ``LIMIT 3``
with ``max_result_rows=5`` emits at most three rows, while ``LIMIT 10``
with ``max_result_rows=5`` emits at most five rows.

``params`` binds openCypher parameters out of band. Query text uses
``$name`` and mapping keys omit the leading ``$``. Supported values are
``None``, booleans, signed 64-bit integers, finite floats, strings,
lists/tuples, and mappings with string keys. Values are passed as Cypher
values, so strings are never interpreted as query text. Limit nested lists/maps to 64
levels. Convert other Python values to these accepted types; construct
temporal/spatial Cypher values in the query with bound strings or maps.

``cypher`` must be NUL-free Unicode text, optionally a semicolon-separated
script.

``timeout`` is a finite, nonnegative number of seconds (not a bool). Its single
deadline covers execution and any deferred table and row reads.
``None`` leaves it unset and ``0`` expires immediately. Pass a
``CancellationToken`` as ``cancellation`` to request a stop, including
from another thread. The call that observes the stop raises
``VelrError`` with code ``query.deadline_exceeded`` or ``query.cancelled``.

Raises
------
TypeError
    If the row cap is not an ``int`` or ``None``, ``params`` is not a
    mapping or ``None``, a parameter value has an unsupported type,
    ``timeout`` is not a number or ``None``, or ``cancellation`` is not
    a ``CancellationToken`` or ``None``.
ValueError
    If ``max_result_rows`` is negative, a parameter name is empty or
    starts with ``$``, an integer is outside the Cypher ``INTEGER``
    range, a float is not finite, nesting exceeds the supported limit,
    or ``timeout`` is negative, nonfinite, or outside the supported range.
OverflowError
    If the row cap exceeds ``2**64 - 1`` on supported 64-bit platforms.
AttributeError, UnicodeError
    If query text cannot be encoded as UTF-8; pass a Unicode ``str``.
VelrError
    If the connection is closed, the runtime reports a query error, or
    the runtime does not expose the query-options/parameter support
    required for requested row caps, parameters, deadlines, or cancellation.

#### velr.driver.Velr.exec_one

```python
exec_one(cypher: str, *, max_result_rows: int | None=None, params: Mapping[str, Any] | None=None, timeout: float | None=None, cancellation: CancellationToken | None=None) -> 'Table'
```

Execute a query expected to return exactly one table.

The returned table is parented to the connection rather than to a transient
stream, so it remains usable after this method returns.

The returned table should be closed when you are done with it. The easiest
way is to use it as a context manager.

``max_result_rows`` caps rows emitted from the table:

- ``None`` preserves the default behavior and emits all rows.
- ``0`` preserves column metadata but emits no rows.
- ``n`` emits at most ``n`` rows.

Existing Cypher ``LIMIT`` clauses still apply. This method still
requires the query to produce exactly one result table, even when
``max_result_rows=0``.

``params`` binds openCypher parameters out of band. Query text uses
``$name`` and mapping keys omit the leading ``$``. Supported values are
``None``, booleans, signed 64-bit integers, finite floats, strings,
lists/tuples, and mappings with string keys. Values are passed as Cypher
values, so strings are never interpreted as query text. Limit nested lists/maps to 64
levels. Convert other Python values to these accepted types; construct
temporal/spatial Cypher values in the query with bound strings or maps.

``cypher`` must be NUL-free Unicode text, optionally a semicolon-separated
script.

``timeout`` is a finite, nonnegative number of seconds (not a bool). Its single
deadline covers execution and any deferred table and row reads.
``None`` leaves it unset and ``0`` expires immediately. Pass a
``CancellationToken`` as ``cancellation`` to request a stop, including
from another thread. The call that observes the stop raises
``VelrError`` with code ``query.deadline_exceeded`` or ``query.cancelled``.

Raises
------
TypeError
    If the row cap is not an ``int`` or ``None``, ``params`` is not a
    mapping or ``None``, a parameter value has an unsupported type,
    ``timeout`` is not a number or ``None``, or ``cancellation`` is not
    a ``CancellationToken`` or ``None``.
ValueError
    If ``max_result_rows`` is negative, a parameter name is empty or
    starts with ``$``, an integer is outside the Cypher ``INTEGER``
    range, a float is not finite, nesting exceeds the supported limit,
    or ``timeout`` is negative, nonfinite, or outside the supported range.
OverflowError
    If the row cap exceeds ``2**64 - 1`` on supported 64-bit platforms.
AttributeError, UnicodeError
    If query text cannot be encoded as UTF-8; pass a Unicode ``str``.
VelrError
    If the connection is closed, the query produces zero or multiple
    result tables, the runtime reports a query error, or the runtime
    does not expose the query-options/parameter support required for a
    requested row caps, parameters, deadlines, or cancellation.

#### velr.driver.Velr.explain

```python
explain(cypher: str) -> ExplainTrace
```

Explain query text without executing it.

Accepts NUL-free Unicode ``str`` query text only,
without parameters, row caps, timeouts, or cancellation arguments. Keep the
trace and its parent connection open while reading; close it explicitly or
use a context manager. Closing the parent closes the trace. Copied snapshots,
metadata, and compact strings/bytes remain usable afterward.

Query/runtime failures and closed, busy, or wrong-thread handles raise
``VelrError``. Unsupported text objects or encoding failures can raise
standard Python exceptions such as ``AttributeError`` or ``UnicodeEncodeError``.

#### velr.driver.Velr.explain_analyze

```python
explain_analyze(cypher: str) -> ExplainTrace
```

Execute and profile query text, including its writes.

Use ``VelrTx.explain_analyze()`` and roll back its transaction when analyzed
writes should be discarded. Accepts NUL-free Unicode ``str`` query text only,
without parameters, row caps, timeouts, or cancellation arguments. Keep the
trace and its parent connection open while reading; close it explicitly or
use a context manager. Closing the parent closes the trace. Copied snapshots,
metadata, and compact strings/bytes remain usable afterward.

Query/runtime failures and closed, busy, or wrong-thread handles raise
``VelrError``. Unsupported text objects or encoding failures can raise
standard Python exceptions such as ``AttributeError`` or ``UnicodeEncodeError``.

#### velr.driver.Velr.to_pyarrow

```python
to_pyarrow(cypher: str)
```

Execute a query and return the last result table as ``pyarrow.Table``.

If the query produces no tables, an empty Arrow table is returned.

Requires PyArrow. The returned Arrow table remains usable after the connection
closes; temporary query handles are closed before return. Missing PyArrow raises
``ImportError``; query failures raise ``VelrError``. Use ``exec_one()`` when
query parameters or result caps are needed. Column conversion follows
:meth:`Table.to_pyarrow`, including JSON strings for structured values.

Accepts query text only; these convenience methods have no ``params``,
``max_result_rows``, ``timeout``, or ``cancellation`` arguments. Use
``exec_one(...).to_pyarrow()`` when controls are required. Earlier result
tables of a script are consumed and discarded; all statements still execute.

#### velr.driver.Velr.to_rows

```python
to_rows(cypher: str, columns=None)
```

Execute a query and return the last table as ``list[list]``.

Return ``[]`` when the query produces no tables. Values and containers are
owned Python objects; query tables/streams are closed before return. Cypher
lists/maps become Python lists/dicts. ``columns`` optionally selects result
TEXT columns containing JSON documents to decode; see :meth:`Table.to_rows`.
It does not restrict the projected columns. Query errors raise ``VelrError``;
invalid selected columns or JSON propagate their validation/decoding errors.
For query parameters or row caps, use ``exec_one(...).to_rows()`` instead.

Accepts query text only; these convenience methods have no ``params``,
``max_result_rows``, ``timeout``, or ``cancellation`` arguments. Use
``exec_one(...).to_rows()`` when controls are required. Earlier result
tables of a script are consumed and discarded; all statements still execute.

#### velr.driver.Velr.to_records

```python
to_records(cypher: str, columns=None)
```

Execute a query and return the last table as ``list[dict]``.

Return ``[]`` when the query produces no tables. Dictionaries use unique
result column names; give projected expressions unique aliases in
``RETURN``. Queries with duplicate aliases raise ``VelrError``. Values remain
usable after the query handles close. ``columns`` follows
:meth:`Table.to_rows` and selects TEXT columns to JSON-decode, not a result
projection. Use ``exec_one(...).to_records()`` when passing query parameters
or a result cap.

Accepts query text only; these convenience methods have no ``params``,
``max_result_rows``, ``timeout``, or ``cancellation`` arguments. Use
``exec_one(...).to_records()`` when controls are required. Earlier result
tables of a script are consumed and discarded; all statements still execute.

#### velr.driver.Velr.to_pandas

```python
to_pandas(cypher: str, **kwargs)
```

Execute a query and return its last table as a pandas DataFrame.

Requires pandas and PyArrow. No result table produces an empty DataFrame.
``dtype_backend`` defaults to ``"numpy_nullable"`` and is passed to
``DataFrame.convert_dtypes`` when supported; use ``None`` to skip conversion.
Other keyword arguments raise ``VelrError``. The returned DataFrame does not
require an open database handle. Query failures raise ``VelrError``; missing optional packages and dataframe
conversion errors propagate as standard Python exceptions.

Accepts query text only, without query parameters or execution controls.
Use ``exec_one(...).to_pandas()`` for parameterized or controlled queries.
Conversion follows ``Table.to_pyarrow``; structured values remain JSON strings.

Default conversion of nullable 64-bit integer columns can round values
outside the exact floating-point integer range (``abs(value) > 2**53 - 1``), even when the returned dtype is ``Int64``.
Setting ``dtype_backend`` cannot recover precision already lost. For exact
nullable integers, import pandas as ``pd`` and convert the Arrow table with
``db.to_pyarrow(cypher).to_pandas(types_mapper=pd.ArrowDtype)``.
``to_rows()``, ``to_records()``, and ``to_pyarrow()`` preserve these integers.

#### velr.driver.Velr.to_polars

```python
to_polars(cypher: str)
```

Execute a query and return its last table as a Polars DataFrame.

Requires Polars and PyArrow. No result table produces an empty DataFrame.
The result remains usable after the connection closes. Query failures raise
``VelrError`` and missing optional packages raise ``ImportError``.

Accepts query text only, without query parameters or execution controls.
Use ``exec_one(...).to_polars()`` for parameterized or controlled queries.
Conversion follows ``Table.to_pyarrow``; structured values remain JSON strings.

#### velr.driver.Velr.bind_arrow

```python
bind_arrow(logical: str, columns) -> None
```

Bind Arrow columns as a named input table for ``BIND('name')``.

``logical`` is the name used by Cypher, for example
``UNWIND BIND('_people') AS row RETURN row.name``. ``columns`` accepts a
``pyarrow.Table``, ``RecordBatch``, a mapping of names to ``Array`` or
``ChunkedArray`` objects, or a sequence of ``(name, array)`` pairs. Requires
PyArrow. Use NUL-free ``str`` for ``logical`` and string column names.
All columns must have equal total row counts; each chunked column must
contain at least one chunk. Binding makes input available to queries; it
does not insert graph nodes or relationships. Returns ``None``. Imported
data stays available without retaining the original Python references,
but treat its underlying arrays as immutable while bound.

Unsupported input shapes, column values/layouts, closed/busy connections,
and database failures raise ``VelrError``. Missing PyArrow raises
``ImportError``; encoding and Arrow conversion errors can also propagate.

#### velr.driver.Velr.bind_arrow_ipc

```python
bind_arrow_ipc(logical: str, ipc_bytes) -> None
```

Bind Arrow IPC file/Feather v2 bytes as a named Cypher input table.

``ipc_bytes`` accepts a nonempty contiguous buffer such as ``bytes``,
``bytearray``, or ``memoryview``. It need only remain valid during this call.
Read the data with ``UNWIND BIND('name') AS row ...``; binding alone does not
write graph data. Use a NUL-free ``str`` logical name. IPC streams are not
accepted. PyArrow is not required. Returns ``None``. Empty/noncontiguous buffers raise
``ValueError``; incompatible buffers raise ``TypeError``; invalid IPC or a
closed connection raises ``VelrError``.

#### velr.driver.Velr.bind_arrow_chunks

```python
bind_arrow_chunks(logical: str, columns) -> None
```

Alias for ``bind_arrow`` accepting the same single- or multi-chunk columns.

The logical name, supported Arrow inputs, required PyArrow installation,
input immutability, return value, and errors are identical to ``Velr.bind_arrow``.

#### velr.driver.Velr.bind_pandas

```python
bind_pandas(logical: str, df: 'pd.DataFrame', *, index: bool=False, jsonify_objects: bool=True, schema: "Optional['pa.Schema']"=None) -> None
```

Bind a pandas DataFrame as a named Cypher input table.

Requires pandas and PyArrow. ``index=False`` excludes the dataframe index;
set it to ``True`` to include it. ``jsonify_objects=True`` serializes nested
object values as JSON strings and stringifies unsupported objects before
conversion. Set it to ``False`` to use PyArrow's conversion directly.
``None``/NaN values remain null-like. Non-string dataframe column names
are converted to strings. ``schema`` optionally supplies a ``pyarrow.Schema``
with PyArrow's column-selection/type rules. The resulting table follows
``bind_arrow`` lifetime and input-immutability rules. Returns ``None``;
conversion errors propagate, and binding errors raise ``VelrError``. See
:meth:`bind_arrow` for querying the bound name.

#### velr.driver.Velr.bind_polars

```python
bind_polars(logical: str, df: 'pl.DataFrame', *, rechunk: bool=False) -> None
```

Bind a Polars DataFrame as a named Cypher input table.

Requires Polars and PyArrow. ``rechunk=True`` combines dataframe chunks
before conversion; the default preserves existing chunks. Returns ``None``.
Conversion errors propagate; database binding errors raise ``VelrError``.
See :meth:`bind_arrow` for ``BIND(...)`` usage.

#### velr.driver.Velr.bind_numpy

```python
bind_numpy(logical: str, data, *, names: list[str] | None=None, types: dict | None=None) -> None
```

Bind NumPy columns as a named Cypher input table.

``data`` is a mapping from column names to arrays, or a two-dimensional
``numpy.ndarray`` with one name per column in ``names``. ``types`` optionally
maps column names to PyArrow types. Requires NumPy and PyArrow. Returns
``None``. Unsupported shapes or missing/mismatched names raise ``VelrError``;
array conversion errors and missing-package ``ImportError`` propagate.
The ``types`` mapping casts named columns and leaves other columns inferred.
Input lifetime/immutability follows ``bind_arrow``. See that method for query usage.

#### velr.driver.Velr.bind_records

```python
bind_records(logical: str, rows: list[dict], *, types: dict | None=None) -> None
```

Bind a list of mapping records as a named Cypher input table.

``logical`` is a nonempty NUL-free ``str``. Query the binding with
``UNWIND BIND('name') AS row``; binding alone creates no graph entities.
Uniform nonempty records with identical string keys support ``None``, bool,
signed 64-bit int, finite float, str, and nested list/tuple/mapping values
without PyArrow. Bound records are copied; the input need not remain alive
or unchanged after the call.

Empty inputs, differing key sets, Arrow-specific values, or any supplied
``types`` mapping require PyArrow. Inferred Arrow conversion uses the first
record's fields; keys appearing only in later records are omitted. A nonempty
``types`` mapping defines the entire bound Arrow schema, including field
order: fields absent from it are
omitted. Use ``types=None`` (the default) for ordinary inferred records, or
``bind_arrow()`` for explicit Arrow arrays/columns. All records are materialized
in memory during binding.

Returns ``None``. Invalid input can raise standard Python/Arrow validation
exceptions, missing PyArrow raises ``ImportError`` when required, and runtime,
closed-connection, or binding failures raise ``VelrError``.

#### velr.driver.Velr.begin_tx

```python
begin_tx() -> 'VelrTx'
```

Begin this connection's single active explicit transaction.

Returns a caller-owned :class:`VelrTx`; keep this connection open until the
transaction finishes. Call ``commit()`` explicitly to persist writes. A
transaction context rolls back on exit unless already finalized, and closing
the connection closes the transaction. Use savepoints for nested scopes.

A writable transaction reserves the writer slot when started. For a read-only
snapshot while another connection writes, begin a transaction on a separate
read-only connection. Use transaction methods for work in the scope and finish
results/savepoints before committing or rolling back.

A closed/busy connection, an already-active transaction, unsupported older
format, or begin failure raises ``VelrError``. Do not overlap operations on
one connection or move it between threads while the transaction is active.

### velr.driver.Stream

Stream of result tables produced by ``Velr.exec()``.

Use ``next_table()`` to pull tables one by one, or ``iter_tables()`` to
iterate over the remaining tables.

Tables yielded by this stream are valid while it remains open. Closing or
exhausting the stream closes its still-open tables and row iterators.

Use ``close()`` when done, or use the stream as a context manager.

Do not construct streams directly; obtain them from ``exec()``. Use a context
manager to ensure cleanup, especially when stopping before exhaustion.

#### velr.driver.Stream.next_table

```python
next_table() -> Optional['Table']
```

Return the next :class:`Table`, or ``None`` after stream exhaustion/closure.

The table is owned by this stream and must be consumed or exported while the
stream is open. Reaching end-of-stream closes it and any remaining child
tables. Query errors, wrong-thread access, or a closed parent raise
``VelrError``. A closed stream returns ``None`` only while its parent remains open.

#### velr.driver.Stream.iter_tables

```python
iter_tables()
```

Iterate over remaining result tables.

Each yielded table is closed automatically after the iteration step
completes.

Consume each table inside the loop body: advancing or closing the generator
closes the previous table. Use a stream context manager when stopping early.
Execution errors propagate as ``VelrError``.

#### velr.driver.Stream.close

```python
close() -> None
```

Close this stream and any still-open tables and row iterators it produced.

Repeated calls are harmless. The parent connection/transaction remains open.
After stream closure ``next_table()`` returns ``None`` while its parent
remains open; a closed parent instead raises ``VelrError``. Call on the
creating thread; cross-thread access raises ``VelrError``.

#### velr.driver.Stream.__enter__

```python
__enter__() -> 'Stream'
```

Return this handle for use in a ``with`` block.


#### velr.driver.Stream.__exit__

```python
__exit__(exc_type, exc, tb)
```

Close this handle on block exit without suppressing the exception.


### velr.driver.StreamTx

Stream of result tables produced by ``VelrTx.exec()``.

This is the transaction-scoped counterpart to ``Stream``.

Tables yielded by this stream are valid while it remains open. Closing or
exhausting the stream closes its still-open tables and row iterators.

The stream keeps its parent transaction alive for as long as the stream
itself is open.

Do not construct streams directly; obtain them from ``exec()``. Use a context
manager to ensure cleanup, especially when stopping before exhaustion.

#### velr.driver.StreamTx.next_table

```python
next_table() -> Optional['Table']
```

Return the next :class:`Table`, or ``None`` after stream exhaustion/closure.

The table is owned by this stream and must be consumed or exported while the
stream is open. Reaching end-of-stream closes it and any remaining child
tables. Query errors, wrong-thread access, or a closed parent raise
``VelrError``. A closed stream returns ``None`` only while its parent remains open.

#### velr.driver.StreamTx.iter_tables

```python
iter_tables()
```

Iterate over remaining transaction-scoped result tables.

Each yielded table is closed automatically after the iteration step
completes.

Consume each table inside the loop body: advancing or closing the generator
closes the previous table. Use a stream context manager when stopping early.
Execution errors propagate as ``VelrError``.

#### velr.driver.StreamTx.close

```python
close() -> None
```

Close this stream and any still-open tables and row iterators it produced.

Repeated calls are harmless. The parent connection/transaction remains open.
After stream closure ``next_table()`` returns ``None`` while its parent
remains open; a closed parent instead raises ``VelrError``. Call on the
creating thread; cross-thread access raises ``VelrError``.

#### velr.driver.StreamTx.__enter__

```python
__enter__() -> 'StreamTx'
```

Return this handle for use in a ``with`` block.


#### velr.driver.StreamTx.__exit__

```python
__exit__(exc_type, exc, tb)
```

Close this handle on block exit without suppressing the exception.


### velr.driver.Table

One result table.

A table provides:

- column metadata via ``column_count()`` and ``column_names()``
- row access via ``iter_rows()``
- convenience export to Arrow, pandas, Polars, and IPC bytes

Keep the producing connection/transaction open while reading. A table
from ``exec()`` also requires its producing stream to remain open; stream
exhaustion closes its remaining tables. Explicit parent closure closes
the table and its row iterators. Keep active handles on their creating thread.

Obtain a table from ``exec_one()`` or a stream. Close it explicitly or use a
context manager. Operations on a closed table raise ``VelrError``. Exports
materialize data and return objects that remain usable after this table closes.

Arrow IPC and dataframe exports infer column types from the result values.
Cypher lists, maps, nodes, relationships, and paths become JSON strings in
Arrow UTF-8 columns. Numeric columns may use integer or floating-point types;
incompatible value kinds in one column become strings. Nulls remain nulls,
and empty/all-null columns use UTF-8 string types. Inspect the exported schema
before assigning application-specific dtypes. Use ``to_rows()`` or
``to_records()`` for decoded Python lists and dictionaries.

#### velr.driver.Table.to_pyarrow_zero_copy

```python
to_pyarrow_zero_copy()
```

Return a ``pyarrow.Buffer`` containing Arrow IPC file bytes.

This is an encoded IPC buffer, not a ``pyarrow.Table``. PyArrow must be
installed. The buffer retains its memory independently of the table handle;
keep the returned buffer alive while using its data. The method avoids a
Python bytes copy but still exports the result. Raises ``VelrError`` for an
unavailable export, closed table, or missing PyArrow.

#### velr.driver.Table.to_pyarrow

```python
to_pyarrow()
```

Export this result as a ``pyarrow.Table``.

Requires PyArrow. The returned table owns references to its exported data and
remains usable after the Velr table closes. Raises ``VelrError`` for a closed
table, unavailable export, or missing PyArrow; Arrow decoding errors propagate.
Types follow the inferred Arrow schema described on :class:`Table`;
structured Cypher values are JSON strings, with null values preserved.
Export materializes data and may use memory proportional to the result size.

#### velr.driver.Table.to_pandas

```python
to_pandas(**kwargs)
```

Export this result as a pandas DataFrame.

Requires pandas and PyArrow. ``dtype_backend`` defaults to
``"numpy_nullable"``; use ``None`` to skip dtype conversion. Other keyword
arguments raise ``VelrError``. The returned DataFrame remains usable after
the table closes. Export/dependency failures raise ``VelrError``; conversion
errors from pandas or PyArrow propagate. Arrow column conversion follows
:meth:`to_pyarrow`; structured Cypher values remain JSON strings.

Default conversion of nullable 64-bit integer columns can round values
outside the exact floating-point integer range (``abs(value) > 2**53 - 1``), even when the returned dtype is ``Int64``.
Setting ``dtype_backend`` cannot recover precision already lost. For exact
nullable integers, import pandas as ``pd`` and convert the Arrow table with
``table.to_pyarrow().to_pandas(types_mapper=pd.ArrowDtype)``.
``to_rows()``, ``to_records()``, and ``to_pyarrow()`` preserve these integers.

#### velr.driver.Table.to_polars

```python
to_polars()
```

Export this result as a Polars DataFrame.

Requires Polars and PyArrow. The result remains usable after this table
closes. Export/dependency failures raise ``VelrError``; conversion errors
from Polars or PyArrow propagate. Arrow column conversion follows
:meth:`to_pyarrow`; structured Cypher values remain JSON strings.

#### velr.driver.Table.to_ipc_memoryview

```python
to_ipc_memoryview() -> memoryview
```

Export Arrow IPC file bytes as a ``memoryview``.

The view retains the exported memory independently of the table handle; keep
the view alive while consuming it. No PyArrow installation is required.
Raises ``VelrError`` for a closed table or unavailable export.

#### velr.driver.Table.to_ipc_bytes

```python
to_ipc_bytes() -> bytes
```

Return an owned ``bytes`` copy of this result's Arrow IPC file.

The bytes remain valid after the table closes and can be saved or passed to
``bind_arrow_ipc()``. No PyArrow installation is required. Export failures
and closed-table access raise ``VelrError``. The file uses the inferred
Arrow column types described on :class:`Table`.

#### velr.driver.Table.to_rows

```python
to_rows(columns=None)
```

Return the table as plain Python row lists.

This materializes the result for application code as lists, dicts,
strings, numbers, booleans, and ``None``. Cypher lists and maps are
returned as Python lists and dicts; graph entities are dictionaries and
paths become :class:`Path`, including inside lists/maps. Plain lists of
entities remain lists. Integer values preserve signed 64-bit precision.
Nonfinite numeric results, when produced by the query, remain Python
``float`` NaN/infinity rather than JSON strings. Use this method when the
caller intends to read the whole result.

By default all columns from the query result are returned. ``columns``
can name projected TEXT columns that contain serialized list/map
documents and should be decoded into Python dict/list values. Ordinary
Cypher strings stay strings unless selected here. Prefer expressing
result projection in Cypher with ``RETURN``.

``columns`` accepts a column name or a sequence of names/zero-based indexes,
such as ``[0]`` for the first column. A nonzero scalar index also works;
scalar ``0`` is treated as an empty selection, so use ``[0]`` for that column.
Unknown names, invalid indexes/types,
or malformed selected JSON raise validation/decoding errors. The returned
rows remain usable after the table closes. This materializes the whole
result; use ``iter_rows()`` for incremental cell access. Closed, busy,
wrong-thread handles, observed cancellation/deadlines, and export failures
raise ``VelrError``.

#### velr.driver.Table.to_records

```python
to_records(columns=None)
```

Return the table as plain Python dict records.

This materializes the result as a list of dictionaries keyed by result
column name. It is the named counterpart to ``to_rows()``; its scalars,
lists, maps, entity dictionaries, and :class:`Path` values follow that method.
Collected records remain usable after parent handles close. Use
``bind_records()`` for records with its supported field types.

Result column names must be unique. ``columns`` follows ``to_rows()``
and can decode selected serialized list/map text columns into Python
values.

Duplicate result column names raise ``ValueError``; a closed table raises
``VelrError``. Selected TEXT-column validation and JSON errors follow
``to_rows()``.
Cypher queries reject duplicate result aliases with ``VelrError`` before
a table is created; give projected expressions unique aliases in ``RETURN``.

#### velr.driver.Table.write_ipc_to

```python
write_ipc_to(fileobj) -> int
```

Write Arrow IPC file bytes to an open binary file-like object.

Return the value returned by ``fileobj.write`` (normally the byte count).
The destination stays open. Export failures raise ``VelrError``; destination
I/O exceptions propagate. No PyArrow installation is required.

#### velr.driver.Table.save_ipc

```python
save_ipc(path: str) -> None
```

Write Arrow IPC file bytes to ``path`` and return ``None``.

Open the file in binary write mode, replacing existing contents, and close
it before return. Export failures raise ``VelrError``; filesystem errors
propagate. No PyArrow installation is required.

#### velr.driver.Table.column_count

```python
column_count() -> int
```

Return the number of projected columns as an integer.

A zero-row result still exposes its projected columns. The table must
remain open; closed-handle access raises ``VelrError``.

#### velr.driver.Table.column_names

```python
column_names() -> List[str]
```

Return a new list of projected UTF-8 column names, in result order.

The returned list survives table closure, but this method requires an open
table and raises ``VelrError`` after closure. Give projected expressions
unique aliases in ``RETURN``; duplicate aliases fail query validation
before a table is created. Invalid UTF-8 raises ``UnicodeDecodeError``.

#### velr.driver.Table.iter_rows

```python
iter_rows() -> 'Rows'
```

Open a cursor-style row iterator for this table.

Iteration yields ``Cell`` objects one row at a time. This avoids
materializing all result rows as Python values; it does not cap the
query's execution memory or time. Each returned row is a tuple of copied
``Cell`` values.

Returns a :class:`Rows` handle. Use ``with table.iter_rows() as rows`` when
stopping early; exhausting it closes the iterator automatically. Closing the
table closes its row iterators. Fetch failures raise ``VelrError``.
Iteration observes the query's original deadline and cancellation token.

#### velr.driver.Table.rows

```python
rows() -> 'Rows'
```

Deprecated alias for ``iter_rows``; prefer ``iter_rows`` in new code.

Returns the same owned row cursor and has the same close, error, and incremental
consumption contract as ``Table.iter_rows``.

#### velr.driver.Table.for_each_row

```python
for_each_row(fn) -> None
```

Call ``fn(row)`` for every row and return ``None``.

Each row is a tuple of copied :class:`Cell` objects. The temporary row
iterator is closed even when ``fn`` raises; callback exceptions propagate.
The table itself remains open. ``fn`` runs synchronously; coroutine or
awaitable return values are not awaited. Fetch failures raise ``VelrError``.
Retained cells survive cursor/table/connection closure.

#### velr.driver.Table.collect

```python
collect(map_fn=None)
```

Collect rows into a list.

Parameters
----------
map_fn:
    Optional synchronous callable applied to each row before collecting.
    Returned awaitables are retained as list entries and are not awaited.

Returns
-------
list
    Collected rows or mapped row values.

The temporary iterator closes on success or error; the table remains open.
Rows are tuples of copied ``Cell`` values unless ``map_fn`` transforms them.
Row-reading errors raise ``VelrError``; callback exceptions propagate.
Use ``to_rows()`` for plain Python values. The entire output is retained
in memory; copied cells and mapped values survive parent closure. Use a row
iterator or ``for_each_row()`` for incremental processing.

#### velr.driver.Table.close

```python
close() -> None
```

Close this table and any open row iterators it produced.

Repeated calls are harmless. Previously copied ``Cell`` values, Python
rows/records, and exported data remain usable; further table operations raise
``VelrError``. Prefer a context manager for deterministic cleanup.

#### velr.driver.Table.__enter__

```python
__enter__() -> 'Table'
```

Return this handle for use in a ``with`` block.


#### velr.driver.Table.__exit__

```python
__exit__(exc_type, exc, tb)
```

Close this handle on block exit without suppressing the exception.


### velr.driver.Rows

Iterator over rows in a table.

Iteration yields ``tuple[Cell, ...]``.

Use ``close()`` when done, or use the iterator as a context manager.

Obtain this handle from ``Table.iter_rows()``, not by direct construction.
Exhaustion closes it automatically. Closing its table also closes it. Fetch
errors raise ``VelrError``; ordinary exhaustion raises ``StopIteration``.

#### velr.driver.Rows.__iter__

```python
__iter__()
```

Return the iterator itself.

#### velr.driver.Rows.__next__

```python
__next__()
```

Return the next row as a tuple of copied :class:`Cell` objects.

Raise ``StopIteration`` and close the iterator at end-of-results or when
already closed. Query/fetch errors raise ``VelrError``. Returned cells remain
usable after further iteration and after parent handles close.

#### velr.driver.Rows.close

```python
close() -> None
```

Close this row iterator without closing its table.

Repeated calls are harmless. Future iteration stops; previously returned
``Cell`` tuples remain usable. Use a context manager when stopping early.

#### velr.driver.Rows.__enter__

```python
__enter__() -> 'Rows'
```

Return this handle for use in a ``with`` block.


#### velr.driver.Rows.__exit__

```python
__exit__(exc_type, exc, tb)
```

Close this handle on block exit without suppressing the exception.


### velr.driver.Savepoint

A savepoint within a transaction.

Keep this handle and its transaction on their creating thread, with the
transaction and connection open. A savepoint context releases on normal
exit and rolls back/releases on an exception. An exception is not suppressed;
a cleanup failure can replace it.

Scoped savepoints are handle-owned:
- dropping the handle closes the savepoint
- ``release()`` releases it
- ``rollback()`` rolls back to it and releases it

Named savepoints are transaction-owned:
- dropping the returned Python handle does not remove the named savepoint
- ``release()`` on a named handle releases that named savepoint
- ``rollback()`` on a named handle rolls back to that named savepoint and
  releases it
- ``VelrTx.rollback_to(name)`` rolls back to a named savepoint, discards any
  newer named savepoints, and keeps the target named savepoint active

#### velr.driver.Savepoint.release

```python
release() -> None
```

Release this savepoint while retaining its changes in the transaction.

This does not commit the outer transaction. Repeated calls on a closed
handle are harmless. Named savepoints must be released in reverse creation
order; invalid or wrong-thread operations raise ``VelrError``. Returns
``None``. A named marker consumed through its transaction can make further
handle operations fail even when the Python handle still exists.

#### velr.driver.Savepoint.rollback

```python
rollback() -> None
```

Undo changes after this savepoint, then release it.

The outer transaction remains active. This differs from
``VelrTx.rollback_to(name)``, which keeps the target named savepoint active.
Calling this on a closed or already-consumed handle raises ``VelrError``;
``release()`` and ``close()`` have different repeated-call behavior. Runtime
failures also raise ``VelrError``. Returns ``None`` on success.

#### velr.driver.Savepoint.close

```python
close() -> None
```

Close this handle without committing the outer transaction.

An active unnamed scoped savepoint is rolled back and released. Closing a
named handle leaves its transaction-owned savepoint active; use
``release()``, ``rollback()``, or the transaction's named-savepoint methods
to change it. Repeated calls are harmless. Prefer a ``with`` block when you
want release on success and rollback on an exception.

#### velr.driver.Savepoint.__enter__

```python
__enter__() -> 'Savepoint'
```

Return this handle for use in a ``with`` block.


#### velr.driver.Savepoint.__exit__

```python
__exit__(exc_type, exc, tb)
```

Release on normal exit, or roll back and release on exceptional exit.

Does not commit the outer transaction or suppress the block exception.
A savepoint cleanup error can replace an exception raised by the block.

### velr.driver.VelrTx

A transaction handle.

Use ``commit()`` to persist writes or ``rollback()`` to discard them.
Only one transaction may be active on its connection. Writable transactions
reserve the writer slot; use a separate read-only connection for snapshots
while another connection writes. Use transaction methods for work in its scope.

If used as a context manager, a still-open transaction is rolled back on
exit. Use ``savepoint()`` for scoped savepoints and ``savepoint_named()``
for transaction-owned named savepoints.

Obtain a transaction from ``Velr.begin_tx()``. Committing, rolling back, or
closing it closes its child handles. Query/binding operations after transaction
end raise ``VelrError``. Do not share transactions between threads.

#### velr.driver.VelrTx.run

```python
run(cypher: str, *, max_result_rows: int | None=None, params: Mapping[str, Any] | None=None, timeout: float | None=None, cancellation: CancellationToken | None=None) -> None
```

Execute a query or script inside the transaction and discard results.

``max_result_rows`` controls how many rows Velr emits while this method
drains result tables:

- ``None`` preserves the default behavior and drains all rows.
- ``0`` preserves table metadata but emits no rows.
- ``n`` emits at most ``n`` rows from each result table.

Existing Cypher ``LIMIT`` clauses still apply. The option caps emitted
result rows only; it does not make the query read-only, cancel
execution, or skip write side effects.

``params`` binds openCypher parameters out of band. Query text uses
``$name`` and mapping keys omit the leading ``$``. Supported values are
``None``, booleans, signed 64-bit integers, finite floats, strings,
lists/tuples, and mappings with string keys. Values are passed as Cypher
values, so strings are never interpreted as query text. Limit nested lists/maps to 64
levels. Convert other Python values to these accepted types; construct
temporal/spatial Cypher values in the query with bound strings or maps.

``cypher`` must be NUL-free Unicode text, optionally a semicolon-separated
script.

``timeout`` is a finite, nonnegative number of seconds (not a bool). Its single
deadline covers execution and any deferred table and row reads.
``None`` leaves it unset and ``0`` expires immediately. Pass a
``CancellationToken`` as ``cancellation`` to request a stop, including
from another thread. The call that observes the stop raises
``VelrError`` with code ``query.deadline_exceeded`` or ``query.cancelled``.

Raises
------
TypeError
    If the row cap is not an ``int`` or ``None``, ``params`` is not a
    mapping or ``None``, a parameter value has an unsupported type,
    ``timeout`` is not a number or ``None``, or ``cancellation`` is not
    a ``CancellationToken`` or ``None``.
ValueError
    If ``max_result_rows`` is negative, a parameter name is empty or
    starts with ``$``, an integer is outside the Cypher ``INTEGER``
    range, a float is not finite, nesting exceeds the supported limit,
    or ``timeout`` is negative, nonfinite, or outside the supported range.
OverflowError
    If the row cap exceeds ``2**64 - 1`` on supported 64-bit platforms.
AttributeError, UnicodeError
    If query text cannot be encoded as UTF-8; pass a Unicode ``str``.
VelrError
    If the transaction is closed, the runtime reports a query error, or
    the runtime does not expose the transaction query-options/parameter
    support required for requested row caps, parameters, deadlines, or cancellation.

#### velr.driver.VelrTx.execute

```python
execute(cypher: str, *, max_result_rows: int | None=None, params: Mapping[str, Any] | None=None, timeout: float | None=None, cancellation: CancellationToken | None=None) -> None
```

Alias of `run()` with the same arguments and behavior.

Execute a query or script inside the transaction and discard results.

``max_result_rows`` controls how many rows Velr emits while this method
drains result tables:

- ``None`` preserves the default behavior and drains all rows.
- ``0`` preserves table metadata but emits no rows.
- ``n`` emits at most ``n`` rows from each result table.

Existing Cypher ``LIMIT`` clauses still apply. The option caps emitted
result rows only; it does not make the query read-only, cancel
execution, or skip write side effects.

``params`` binds openCypher parameters out of band. Query text uses
``$name`` and mapping keys omit the leading ``$``. Supported values are
``None``, booleans, signed 64-bit integers, finite floats, strings,
lists/tuples, and mappings with string keys. Values are passed as Cypher
values, so strings are never interpreted as query text. Limit nested lists/maps to 64
levels. Convert other Python values to these accepted types; construct
temporal/spatial Cypher values in the query with bound strings or maps.

``cypher`` must be NUL-free Unicode text, optionally a semicolon-separated
script.

``timeout`` is a finite, nonnegative number of seconds (not a bool). Its single
deadline covers execution and any deferred table and row reads.
``None`` leaves it unset and ``0`` expires immediately. Pass a
``CancellationToken`` as ``cancellation`` to request a stop, including
from another thread. The call that observes the stop raises
``VelrError`` with code ``query.deadline_exceeded`` or ``query.cancelled``.

Raises
------
TypeError
    If the row cap is not an ``int`` or ``None``, ``params`` is not a
    mapping or ``None``, a parameter value has an unsupported type,
    ``timeout`` is not a number or ``None``, or ``cancellation`` is not
    a ``CancellationToken`` or ``None``.
ValueError
    If ``max_result_rows`` is negative, a parameter name is empty or
    starts with ``$``, an integer is outside the Cypher ``INTEGER``
    range, a float is not finite, nesting exceeds the supported limit,
    or ``timeout`` is negative, nonfinite, or outside the supported range.
OverflowError
    If the row cap exceeds ``2**64 - 1`` on supported 64-bit platforms.
AttributeError, UnicodeError
    If query text cannot be encoded as UTF-8; pass a Unicode ``str``.
VelrError
    If the transaction is closed, the runtime reports a query error, or
    the runtime does not expose the transaction query-options/parameter
    support required for requested row caps, parameters, deadlines, or cancellation.

#### velr.driver.VelrTx.exec

```python
exec(cypher: str, *, max_result_rows: int | None=None, params: Mapping[str, Any] | None=None, timeout: float | None=None, cancellation: CancellationToken | None=None) -> 'StreamTx'
```

Execute a query inside the transaction and return a stream of tables.

Tables produced by the returned stream are stream-scoped: they remain valid
while the stream is open, and closing the stream closes any still-open
tables produced by it.

``max_result_rows`` caps rows emitted from each result table:

- ``None`` preserves the default behavior and emits all rows.
- ``0`` preserves table metadata, including column names, but emits no
  rows.
- ``n`` emits at most ``n`` rows from each result table.

Existing Cypher ``LIMIT`` clauses still apply. For example, ``LIMIT 3``
with ``max_result_rows=5`` emits at most three rows, while ``LIMIT 10``
with ``max_result_rows=5`` emits at most five rows.

``params`` binds openCypher parameters out of band. Query text uses
``$name`` and mapping keys omit the leading ``$``. Supported values are
``None``, booleans, signed 64-bit integers, finite floats, strings,
lists/tuples, and mappings with string keys. Values are passed as Cypher
values, so strings are never interpreted as query text. Limit nested lists/maps to 64
levels. Convert other Python values to these accepted types; construct
temporal/spatial Cypher values in the query with bound strings or maps.

``cypher`` must be NUL-free Unicode text, optionally a semicolon-separated
script.

``timeout`` is a finite, nonnegative number of seconds (not a bool). Its single
deadline covers execution and any deferred table and row reads.
``None`` leaves it unset and ``0`` expires immediately. Pass a
``CancellationToken`` as ``cancellation`` to request a stop, including
from another thread. The call that observes the stop raises
``VelrError`` with code ``query.deadline_exceeded`` or ``query.cancelled``.

Raises
------
TypeError
    If the row cap is not an ``int`` or ``None``, ``params`` is not a
    mapping or ``None``, a parameter value has an unsupported type,
    ``timeout`` is not a number or ``None``, or ``cancellation`` is not
    a ``CancellationToken`` or ``None``.
ValueError
    If ``max_result_rows`` is negative, a parameter name is empty or
    starts with ``$``, an integer is outside the Cypher ``INTEGER``
    range, a float is not finite, nesting exceeds the supported limit,
    or ``timeout`` is negative, nonfinite, or outside the supported range.
OverflowError
    If the row cap exceeds ``2**64 - 1`` on supported 64-bit platforms.
AttributeError, UnicodeError
    If query text cannot be encoded as UTF-8; pass a Unicode ``str``.
VelrError
    If the transaction is closed, the runtime reports a query error, or
    the runtime does not expose the transaction query-options/parameter
    support required for requested row caps, parameters, deadlines, or cancellation.

#### velr.driver.VelrTx.exec_one

```python
exec_one(cypher: str, *, max_result_rows: int | None=None, params: Mapping[str, Any] | None=None, timeout: float | None=None, cancellation: CancellationToken | None=None) -> 'Table'
```

Execute a query inside the transaction expecting exactly one table.

The returned table remains usable until it or its transaction is closed.
Use a context manager to close the result when finished.

``max_result_rows`` caps rows emitted from the table:

- ``None`` preserves the default behavior and emits all rows.
- ``0`` preserves column metadata but emits no rows.
- ``n`` emits at most ``n`` rows.

Existing Cypher ``LIMIT`` clauses still apply. This method still
requires the query to produce exactly one result table, even when
``max_result_rows=0``.

``params`` binds openCypher parameters out of band. Query text uses
``$name`` and mapping keys omit the leading ``$``. Supported values are
``None``, booleans, signed 64-bit integers, finite floats, strings,
lists/tuples, and mappings with string keys. Values are passed as Cypher
values, so strings are never interpreted as query text. Limit nested lists/maps to 64
levels. Convert other Python values to these accepted types; construct
temporal/spatial Cypher values in the query with bound strings or maps.

``cypher`` must be NUL-free Unicode text, optionally a semicolon-separated
script.

``timeout`` is a finite, nonnegative number of seconds (not a bool). Its single
deadline covers execution and any deferred table and row reads.
``None`` leaves it unset and ``0`` expires immediately. Pass a
``CancellationToken`` as ``cancellation`` to request a stop, including
from another thread. The call that observes the stop raises
``VelrError`` with code ``query.deadline_exceeded`` or ``query.cancelled``.

Raises
------
TypeError
    If the row cap is not an ``int`` or ``None``, ``params`` is not a
    mapping or ``None``, a parameter value has an unsupported type,
    ``timeout`` is not a number or ``None``, or ``cancellation`` is not
    a ``CancellationToken`` or ``None``.
ValueError
    If ``max_result_rows`` is negative, a parameter name is empty or
    starts with ``$``, an integer is outside the Cypher ``INTEGER``
    range, a float is not finite, nesting exceeds the supported limit,
    or ``timeout`` is negative, nonfinite, or outside the supported range.
OverflowError
    If the row cap exceeds ``2**64 - 1`` on supported 64-bit platforms.
AttributeError, UnicodeError
    If query text cannot be encoded as UTF-8; pass a Unicode ``str``.
VelrError
    If the transaction is closed, the query produces zero or multiple
    result tables, the runtime reports a query error, or the runtime
    does not expose the transaction query-options/parameter support
    required for requested row caps, parameters, deadlines, or cancellation.

#### velr.driver.VelrTx.explain

```python
explain(cypher: str) -> ExplainTrace
```

Explain query text without executing it.

Accepts NUL-free Unicode ``str`` query text only,
without parameters, row caps, timeouts, or cancellation arguments. Keep the
trace and its parent transaction open while reading; close it explicitly or
use a context manager. Closing the parent closes the trace. Copied snapshots,
metadata, and compact strings/bytes remain usable afterward.

Query/runtime failures and closed, busy, or wrong-thread handles raise
``VelrError``. Unsupported text objects or encoding failures can raise
standard Python exceptions such as ``AttributeError`` or ``UnicodeEncodeError``.

#### velr.driver.VelrTx.explain_analyze

```python
explain_analyze(cypher: str) -> ExplainTrace
```

Execute and profile query text, including its writes.

Changes remain part of this transaction and follow its commit/rollback
outcome. Accepts NUL-free Unicode ``str`` query text only,
without parameters, row caps, timeouts, or cancellation arguments. Keep the
trace and its parent transaction open while reading; close it explicitly or
use a context manager. Closing the parent closes the trace. Copied snapshots,
metadata, and compact strings/bytes remain usable afterward.

Query/runtime failures and closed, busy, or wrong-thread handles raise
``VelrError``. Unsupported text objects or encoding failures can raise
standard Python exceptions such as ``AttributeError`` or ``UnicodeEncodeError``.

#### velr.driver.VelrTx.savepoint

```python
savepoint() -> Savepoint
```

Return an unnamed :class:`Savepoint` scoped to this transaction.

Use it as a context manager to release on success or undo its work on an
exception. Closing an active scoped savepoint rolls back and releases it.
A finished transaction or a savepoint-creation failure raises ``VelrError``.

#### velr.driver.VelrTx.savepoint_named

```python
savepoint_named(name: str) -> Savepoint
```

Create a transaction-owned named savepoint.

The named savepoint remains active in the transaction even if the returned
Python handle is dropped. Use ``rollback_to(name)`` or
``release_savepoint(name)`` to act on it later by name.

The returned handle supports the same success/exception context-manager
behavior as scoped savepoints. Savepoint failures or a finished transaction
raise ``VelrError``. Names must be unique, nonempty NUL-free strings
while active; invalid names or duplicate markers raise runtime errors.
Unsupported Python name objects can raise ``AttributeError`` on encoding.
Release named savepoints in reverse creation order.

#### velr.driver.VelrTx.rollback_to

```python
rollback_to(name: str) -> None
```

Roll back to a named savepoint.

On success, changes made after the target named savepoint are undone, newer
named savepoints are discarded, and the target named savepoint remains
active and may be used again.

An unknown name, a finished transaction, or rollback failure raises ``VelrError``.
This returns ``None`` and does not commit the outer transaction.

#### velr.driver.VelrTx.release_savepoint

```python
release_savepoint(name: str) -> None
```

Release a named savepoint.

The named savepoint must be the most recent active named savepoint.

Returns ``None``. An unknown name, out-of-order release, or a finished
transaction raises ``VelrError``; changes remain part of the outer transaction.

#### velr.driver.VelrTx.commit

```python
commit() -> None
```

Commit the transaction.

Any still-active named savepoints are released first so that surviving
changes inside the transaction are preserved.

After the commit operation consumes the transaction, it can no longer
be used, including if that operation reports a failure. A failure while
closing children or releasing savepoints can occur before consumption.

Closes child result handles. Commit failures raise ``VelrError``; the
transaction cannot be reused after the commit operation consumes it.

#### velr.driver.VelrTx.rollback

```python
rollback() -> None
```

Roll back the transaction.

After this call, the transaction can no longer be used.

Closes child result handles. Runtime failures or a finished transaction
raise ``VelrError``. Returns ``None``.

#### velr.driver.VelrTx.close

```python
close() -> None
```

Close this transaction, rolling back any uncommitted changes.

Also close child streams, result tables, row iterators, and explain traces.
Calling ``close()`` after commit, rollback, or a previous close is harmless.
It returns ``None``; use ``commit()`` to retain changes.

#### velr.driver.VelrTx.bind_arrow

```python
bind_arrow(logical: str, columns) -> None
```

Bind Arrow columns for queries within this transaction.

Accepts the same ``logical`` name and ``columns`` shapes as
:meth:`Velr.bind_arrow`, including Tables, RecordBatches, arrays and chunked
arrays. Requires PyArrow and returns ``None``. Binding alone does not create
graph data. Conversion errors propagate; a finished transaction or binding
failure raises ``VelrError``. Input lifetime/immutability and required
column lengths/chunks follow ``Velr.bind_arrow``. Transaction-local bindings
are available only until the transaction ends.

#### velr.driver.VelrTx.bind_arrow_ipc

```python
bind_arrow_ipc(logical: str, ipc_bytes) -> None
```

Bind Arrow IPC file/Feather v2 bytes for this transaction's queries.

Accepts the nonempty contiguous buffers described by
:meth:`Velr.bind_arrow_ipc`; the buffer need only survive this call. Returns
``None``. Invalid buffers raise ``TypeError`` or ``ValueError``; invalid IPC,
a finished transaction, or a binding failure raises ``VelrError``. PyArrow
is not required. IPC streams are unsupported, and the logical name must be a
NUL-free string. Transaction-local bindings end with the transaction.

#### velr.driver.VelrTx.bind_arrow_chunks

```python
bind_arrow_chunks(logical: str, columns) -> None
```

Alias for this transaction's ``bind_arrow``.

Supports the same Arrow inputs and chunk layouts, requires PyArrow, returns
``None``, and has the same ownership/immutability and validation rules. The
transaction must remain active; closed transactions raise ``VelrError``.

#### velr.driver.VelrTx.bind_pandas

```python
bind_pandas(logical: str, df: 'pd.DataFrame', *, index: bool=False, jsonify_objects: bool=True, schema: "Optional['pa.Schema']"=None) -> None
```

Bind a pandas DataFrame for queries within this transaction.

``index``, ``jsonify_objects``, and optional PyArrow ``schema`` have the same
meaning as :meth:`Velr.bind_pandas`. Requires pandas/PyArrow, returns ``None``,
and propagates conversion errors; binding failures or a finished transaction
raise ``VelrError``.

#### velr.driver.VelrTx.bind_polars

```python
bind_polars(logical: str, df: 'pl.DataFrame', *, rechunk: bool=False) -> None
```

Bind a Polars DataFrame for queries within this transaction.

``rechunk`` follows :meth:`Velr.bind_polars`. Requires Polars/PyArrow and
returns ``None``. Conversion errors propagate; binding failures or a finished
transaction raise ``VelrError``.

#### velr.driver.VelrTx.bind_numpy

```python
bind_numpy(logical: str, data, *, names: list[str] | None=None, types: dict | None=None) -> None
```

Bind NumPy columns for queries within this transaction.

Input shapes, ``names``, and optional PyArrow ``types`` follow
:meth:`Velr.bind_numpy`. Requires NumPy/PyArrow and returns ``None``. Shape
validation and binding failures raise ``VelrError``; conversion errors
propagate.

#### velr.driver.VelrTx.bind_records

```python
bind_records(logical: str, rows: list[dict], *, types: dict | None=None) -> None
```

Bind a list of dict records within the transaction.

Use this for JSON-compatible Python records. Use ``bind_arrow()`` when
the caller intentionally wants Arrow-backed columnar binding. Passing
``types=...`` builds an Arrow table from the records for explicit type
control.

Input and optional-dependency rules follow :meth:`Velr.bind_records`. Returns
``None``. Invalid records raise Python validation exceptions; database errors
or a finished transaction raise ``VelrError``.

#### velr.driver.VelrTx.__enter__

```python
__enter__() -> 'VelrTx'
```

Return this handle for use in a ``with`` block.


#### velr.driver.VelrTx.__exit__

```python
__exit__(exc_type, exc, tb)
```

Roll back an open transaction on context-manager exit, then close it.

This never commits automatically. It does not suppress an exception from
the block; rollback/close failures can replace that exception.
