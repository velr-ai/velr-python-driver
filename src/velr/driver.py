"""Python API for the embedded Velr graph database.

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
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial, wraps
from threading import Lock, RLock, get_ident, local
from typing import Any, List, NamedTuple, Optional, TYPE_CHECKING
import json
import math
import sys
import warnings
import weakref

from ._ffi import ffi, lib
from .extensions import Extension

__all__ = ['Path', 'ErrorPosition', 'VelrError', 'QueryError', 'CypherSyntaxError', 'CypherSemanticError', 'CypherTypeError', 'ParameterError', 'ProcedureNotFoundError', 'EntityNotFoundError', 'ConstraintError', 'CypherArithmeticError', 'DatabaseBusyError', 'ReadOnlyError', 'MigrationRequiredError', 'HandleStateError', 'QueryCancelled', 'DeadlineExceeded', 'CancellationToken', 'MigrationReport', 'Cell', 'VectorEmbeddingField', 'VectorEmbeddingInput', 'ExplainPlanMeta', 'ExplainStepMeta', 'ExplainStatementMeta', 'ExplainStatement', 'ExplainStep', 'ExplainPlan', 'ExplainTrace', 'Extension', 'Velr', 'Stream', 'StreamTx', 'Table', 'Rows', 'Savepoint', 'VelrTx']

if TYPE_CHECKING:
    import pyarrow as pa  # type: ignore[import-not-found]
    import pandas as pd  # type: ignore[import-not-found]
    import polars as pl  # type: ignore[import-not-found]


def _ipc_buffer(ipc_bytes):
    view = memoryview(ipc_bytes)
    if view.nbytes == 0:
        raise ValueError("Arrow IPC bytes cannot be empty")
    if not view.c_contiguous:
        raise ValueError("Arrow IPC bytes must be contiguous")
    cbuf = ffi.from_buffer("uint8_t[]", view)
    return cbuf, view.nbytes, view


def _normalize_arrow_columns(cols_like):
    """Normalize supported Arrow-like inputs into ``[(name, column), ...]``.

    Accepted inputs are:

    - ``Mapping[str, pyarrow.Array | pyarrow.ChunkedArray]``
    - ``Sequence[tuple[str, pyarrow.Array | pyarrow.ChunkedArray]]``
    - ``pyarrow.RecordBatch``
    - ``pyarrow.Table``

    Returns
    -------
    list[tuple[str, pyarrow.Array | pyarrow.ChunkedArray]]
        Normalized column name/value pairs.

    Raises
    ------
    VelrError
        If the input shape is not supported.
    """
    import pyarrow as pa  # type: ignore

    if hasattr(cols_like, "items"):
        items = list(cols_like.items())
        for k, v in items:
            if not isinstance(v, (pa.Array, pa.ChunkedArray)):
                raise VelrError(f"Column '{k}' must be a pyarrow.Array or ChunkedArray")
        return items

    if isinstance(cols_like, (list, tuple)) and cols_like and isinstance(cols_like[0], (list, tuple)):
        out = []
        for k, v in cols_like:
            if not isinstance(v, (pa.Array, pa.ChunkedArray)):
                raise VelrError(f"Column '{k}' must be a pyarrow.Array or ChunkedArray")
            out.append((k, v))
        return out

    if isinstance(cols_like, pa.RecordBatch):
        return [(f.name, cols_like.column(i)) for i, f in enumerate(cols_like.schema)]

    if isinstance(cols_like, pa.Table):
        return [(n, cols_like[n]) for n in cols_like.schema.names]

    raise VelrError(
        "Unsupported columns object; pass {name: pa.Array|pa.ChunkedArray}, "
        "list[(name, pa.Array|pa.ChunkedArray)], pyarrow.RecordBatch, or pyarrow.Table"
    )


def _pandas_to_arrow_table(
    df: "pd.DataFrame",
    *,
    index: bool = False,
    jsonify_objects: bool = True,
    schema: "Optional[pa.Schema]" = None,
):
    """Convert a pandas DataFrame to a PyArrow table.

    This helper applies defaults that work well with the Velr Arrow binder.

    Parameters
    ----------
    df:
        Input pandas DataFrame.
    index:
        Whether to include the pandas index as a column.
    jsonify_objects:
        When true, object-dtype cells containing ``dict``, ``list``, ``tuple``,
        or ``set`` are serialized as JSON strings. Other non-JSONable values are
        stringified. ``None`` and ``NaN`` remain null-like.
    schema:
        Optional explicit PyArrow schema.

    Returns
    -------
    pyarrow.Table
        Converted Arrow table.
    """
    import math
    import json
    import pandas as pd  # type: ignore
    import pyarrow as pa  # type: ignore

    work = df.copy(deep=False)

    if any(not isinstance(c, str) for c in work.columns):
        work.columns = [str(c) for c in work.columns]

    if jsonify_objects:
        from pandas.api import types as ptypes

        def _jsonify_series(s: "pd.Series") -> "pd.Series":
            def _coerce(x):
                if x is None or (isinstance(x, float) and math.isnan(x)):
                    return None
                if isinstance(x, (dict, list, tuple, set)):
                    if isinstance(x, set):
                        x = list(x)
                    return json.dumps(x, ensure_ascii=False)
                if isinstance(x, str):
                    return x
                if isinstance(x, (bool, int, float)):
                    return x
                return str(x)

            return s.map(_coerce)

        for name in work.columns:
            s = work[name]
            if ptypes.is_object_dtype(s.dtype):
                work[name] = _jsonify_series(s)

        for name in work.columns:
            s = work[name]
            if ptypes.is_bool_dtype(s.dtype):
                continue
            if ptypes.is_integer_dtype(s.dtype):
                try:
                    work[name] = s.astype("Int64")
                except Exception:
                    pass
            elif ptypes.is_float_dtype(s.dtype):
                work[name] = s.astype("float64")
            elif ptypes.is_string_dtype(s.dtype):
                try:
                    work[name] = s.astype("string[python]")
                except Exception:
                    pass

    return pa.Table.from_pandas(work, schema=schema, preserve_index=index)


def _polars_to_arrow_table(df: "pl.DataFrame", *, rechunk: bool = False):
    """Convert a Polars DataFrame to a PyArrow table.

    Parameters
    ----------
    df:
        Input Polars DataFrame.
    rechunk:
        When true, rechunk before conversion.

    Returns
    -------
    pyarrow.Table
        Converted Arrow table.
    """
    import polars as pl  # type: ignore

    if rechunk:
        df = df.rechunk()
    return df.to_arrow()


def _numpy_to_arrow_table(data, *, names: list[str] | None = None, types: dict | None = None):
    """Convert NumPy data to a PyArrow table.

    Accepted inputs are:

    - ``dict[str, np.ndarray]``
    - ``2D np.ndarray`` together with ``names=[...]``

    Parameters
    ----------
    data:
        NumPy input data.
    names:
        Required when passing a bare 2D ndarray.
    types:
        Optional mapping from column name to Arrow type.

    Returns
    -------
    pyarrow.Table
        Converted Arrow table.

    Raises
    ------
    VelrError
        If the input shape is not supported.
    """
    import numpy as np  # type: ignore
    import pyarrow as pa  # type: ignore

    if isinstance(data, dict):
        cols = {}
        for name, arr in data.items():
            dt = types.get(name) if types else None
            cols[name] = pa.array(arr, type=dt) if dt is not None else pa.array(arr)
        return pa.table(cols)

    if isinstance(data, np.ndarray):
        if data.ndim != 2:
            raise VelrError("2D numpy.ndarray expected when passing a bare array")
        if not names:
            raise VelrError("You must supply 'names=[...]' for a 2D numpy array")
        if data.shape[1] != len(names):
            raise VelrError("len(names) must equal array.shape[1]")
        cols = {}
        for i, name in enumerate(names):
            col = data[:, i]
            dt = types.get(name) if types else None
            cols[name] = pa.array(col, type=dt) if dt is not None else pa.array(col)
        return pa.table(cols)

    raise VelrError("Unsupported NumPy input; pass dict[str, ndarray] or a 2D ndarray with names=[]")


def _records_to_arrow_table(rows, *, types: dict | None = None):
    """Convert a list of dict records to a PyArrow table."""
    import pyarrow as pa  # type: ignore

    schema = pa.schema([pa.field(n, t) for n, t in types.items()]) if types else None
    return pa.Table.from_pylist(rows, schema=schema)


_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1
_U32_MAX = (1 << 32) - 1
_ROW_MAP_REQUIRED_COLUMNS_CACHE_MAX = 256
_row_map_required_columns_cache: dict[tuple[str, str], list[str] | None] = {}


def _try_records_to_scalar_cells(rows):
    """Pack JSON-compatible dict records for the native row bind ABI.

    Returns ``None`` when the input needs Arrow semantics, for example bytes,
    non-string column names, missing/extra keys, non-finite floats, or integers
    outside signed i64.
    """
    if not rows:
        return None
    if not isinstance(rows[0], Mapping):
        return None

    names = list(rows[0].keys())
    if not names or any(not isinstance(name, str) for name in names):
        return None

    name_set = set(names)
    if len(name_set) != len(names):
        return None

    col_count = len(names)
    row_count = len(rows)
    cells = ffi.new("velr_cell[]", row_count * col_count)
    text_backing = []

    for row_idx, row in enumerate(rows):
        if not isinstance(row, Mapping) or set(row.keys()) != name_set:
            return None
        base = row_idx * col_count
        for col_idx, name in enumerate(names):
            cell = cells[base + col_idx]
            value = row[name]
            if value is None:
                cell.ty = lib.VELR_NULL
            elif isinstance(value, bool):
                cell.ty = lib.VELR_BOOL
                cell.i64_ = 1 if value else 0
            elif isinstance(value, int):
                if value < _I64_MIN or value > _I64_MAX:
                    return None
                cell.ty = lib.VELR_INT64
                cell.i64_ = value
            elif isinstance(value, float):
                if not math.isfinite(value):
                    return None
                cell.ty = lib.VELR_DOUBLE
                cell.f64_ = value
            elif isinstance(value, str):
                data = value.encode("utf-8")
                cell.ty = lib.VELR_TEXT
                cell.len = len(data)
                if data:
                    data_view = ffi.from_buffer("uint8_t[]", data)
                    text_backing.append((data, data_view))
                    cell.ptr = data_view
                else:
                    cell.ptr = ffi.NULL
            elif isinstance(value, (Mapping, list, tuple)):
                try:
                    data = json.dumps(
                        _json_param_value(value),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                except (TypeError, ValueError):
                    return None
                cell.ty = lib.VELR_JSON
                cell.len = len(data)
                if data:
                    data_view = ffi.from_buffer("uint8_t[]", data)
                    text_backing.append((data, data_view))
                    cell.ptr = data_view
                else:
                    cell.ptr = ffi.NULL
            else:
                return None

    name_views = ffi.new("velr_strview[]", col_count)
    name_backing = []
    for idx, name in enumerate(names):
        data = name.encode("utf-8")
        buf = ffi.new("uint8_t[]", data)
        name_backing.append(buf)
        name_views[idx].ptr = buf
        name_views[idx].len = len(data)

    return names, cells, name_views, name_backing, text_backing, row_count


def _bind_records_scalar(handle, logical: str, rows, *, tx: bool) -> bool:
    packed = _try_records_to_scalar_cells(rows)
    if packed is None:
        return False
    _names, cells, name_views, name_backing, text_backing, row_count = packed
    out_err = ffi.new("char **")
    logical_c = ffi.new("char[]", logical.encode("utf-8"))
    symbol = "velr_tx_bind_cells" if tx else "velr_bind_cells"
    rc = _runtime_symbol(symbol)(
        handle,
        logical_c,
        name_views,
        len(_names),
        cells,
        row_count,
        out_err,
    )
    _ = (name_backing, text_backing)
    _check(rc, out_err)
    return True


def _export_arrow_columns_for_bind(pairs):
    """Export plain Arrow arrays to C Data Interface structures."""
    import pyarrow as pa  # type: ignore
    from pyarrow.cffi import ffi as paffi

    n = len(pairs)
    c_schemas = [paffi.new("struct ArrowSchema*") for _ in range(n)]
    c_arrays = [paffi.new("struct ArrowArray*") for _ in range(n)]

    for i, (name, arr) in enumerate(pairs):
        if not isinstance(arr, pa.Array):
            raise VelrError(f"Column '{name}' is not a pyarrow.Array")
        ptr_schema = int(paffi.cast("uintptr_t", c_schemas[i]))
        ptr_array = int(paffi.cast("uintptr_t", c_arrays[i]))
        arr._export_to_c(ptr_array, ptr_schema)

    schemas_pp = ffi.new("const struct ArrowSchema * []", n)
    arrays_pp = ffi.new("const struct ArrowArray  * []", n)
    for i in range(n):
        schemas_pp[i] = ffi.cast(
            "const struct ArrowSchema *",
            int(paffi.cast("uintptr_t", c_schemas[i])),
        )
        arrays_pp[i] = ffi.cast(
            "const struct ArrowArray *",
            int(paffi.cast("uintptr_t", c_arrays[i])),
        )

    name_views = ffi.new("velr_strview[]", n)
    name_backing = []
    for i, (name, _) in enumerate(pairs):
        b = name.encode("utf-8", "strict")
        buf = ffi.new("unsigned char[]", b)
        name_backing.append(buf)
        name_views[i].ptr = buf
        name_views[i].len = len(b)

    return schemas_pp, arrays_pp, name_views, name_backing, c_schemas, c_arrays


def _export_arrow_columns_for_bind_chunked(pairs):
    """Export Arrow arrays or chunked arrays to chunked C Data structures."""
    import pyarrow as pa  # type: ignore
    from pyarrow.cffi import ffi as paffi

    col_count = len(pairs)

    per_col_chunks = []
    for _, arr in pairs:
        if isinstance(arr, pa.ChunkedArray):
            chunks = list(arr.chunks)
        elif isinstance(arr, pa.Array):
            chunks = [arr]
        else:
            raise VelrError("Expected pa.Array or pa.ChunkedArray")
        if not chunks:
            raise VelrError("ChunkedArray has zero chunks")
        per_col_chunks.append(chunks)

    keep_schemas_nested = []
    keep_arrays_nested = []
    schema_pp_per_col = []
    array_pp_per_col = []

    for chunks in per_col_chunks:
        n = len(chunks)

        ks = [paffi.new("struct ArrowSchema*") for _ in range(n)]
        ka = [paffi.new("struct ArrowArray*") for _ in range(n)]

        schemas_pp = ffi.new("const struct ArrowSchema * []", n)
        arrays_pp = ffi.new("const struct ArrowArray  * []", n)

        for j, ch in enumerate(chunks):
            ptr_schema = int(paffi.cast("uintptr_t", ks[j]))
            ptr_array = int(paffi.cast("uintptr_t", ka[j]))
            ch._export_to_c(ptr_array, ptr_schema)

            schemas_pp[j] = ffi.cast(
                "const struct ArrowSchema *",
                int(paffi.cast("uintptr_t", ks[j])),
            )
            arrays_pp[j] = ffi.cast(
                "const struct ArrowArray *",
                int(paffi.cast("uintptr_t", ka[j])),
            )

        keep_schemas_nested.append(ks)
        keep_arrays_nested.append(ka)
        schema_pp_per_col.append(schemas_pp)
        array_pp_per_col.append(arrays_pp)

    cols_c = ffi.new("struct velr_arrow_chunks[]", col_count)
    for i, chunks in enumerate(per_col_chunks):
        cols_c[i].schemas = schema_pp_per_col[i]
        cols_c[i].arrays = array_pp_per_col[i]
        cols_c[i].chunk_count = len(chunks)

    name_views = ffi.new("velr_strview[]", col_count)
    name_backing = []
    for i, (name, _) in enumerate(pairs):
        b = name.encode("utf-8", "strict")
        buf = ffi.new("unsigned char[]", b)
        name_backing.append(buf)
        name_views[i].ptr = buf
        name_views[i].len = len(b)

    return (
        cols_c,
        name_views,
        name_backing,
        keep_schemas_nested,
        keep_arrays_nested,
        schema_pp_per_col,
        array_pp_per_col,
    )


class _ForeignIPC:
    """Own an Arrow IPC buffer allocated by the runtime.

    The underlying memory is released automatically when this object is
    garbage-collected.
    """

    __slots__ = ("_cdata", "_len", "_mv")

    def __init__(self, ptr, length: int):
        n = int(length)
        self._len = n

        if ptr == ffi.NULL or n == 0:
            self._cdata = ffi.NULL
            self._mv = memoryview(b"")
            return

        raw = ffi.cast("unsigned char *", ptr)

        def _finalize(p):
            if p != ffi.NULL and n >= 0:
                lib.velr_free(p, n)

        self._cdata = ffi.gc(raw, _finalize)
        self._mv = memoryview(ffi.buffer(self._cdata, n))

    def memoryview(self) -> memoryview:
        """Return the IPC bytes as a Python memoryview."""
        return self._mv

    def to_buffer(self):
        """Return a zero-copy ``pyarrow.Buffer`` over the IPC bytes."""
        try:
            import pyarrow as pa  # type: ignore[import-not-found]
        except Exception as e:
            raise VelrError(f"pyarrow is required for zero-copy buffer: {e}") from e
        return pa.py_buffer(self._mv)

    def to_pyarrow(self):
        """Decode the IPC buffer to a ``pyarrow.Table``."""
        try:
            import pyarrow as pa  # type: ignore[import-not-found]
        except Exception as e:
            raise VelrError(f"pyarrow is required: {e}") from e
        buf = self.to_buffer()
        with pa.BufferReader(buf) as reader:
            return pa.ipc.open_file(reader).read_all()

    def to_pandas(self, *, dtype_backend: str = "numpy_nullable", **kwargs):
        """Decode the IPC buffer to a pandas DataFrame.

        Parameters
        ----------
        dtype_backend:
            Passed to ``DataFrame.convert_dtypes(...)`` when supported by the
            installed pandas version.
        """
        tbl = self.to_pyarrow()
        try:
            import pandas as pd  # type: ignore[import-not-found]
        except Exception as e:
            raise VelrError(f"pandas is required for to_pandas(): {e}") from e

        df = tbl.to_pandas()

        backend = kwargs.pop("dtype_backend", dtype_backend)
        if backend is not None:
            try:
                df = df.convert_dtypes(dtype_backend=backend)
            except TypeError:
                df = df.convert_dtypes()

        if kwargs:
            raise VelrError(f"Unsupported to_pandas() kwargs: {list(kwargs.keys())}")

        return df

    def to_polars(self):
        """Decode the IPC buffer to a Polars DataFrame."""
        try:
            import polars as pl  # type: ignore[import-not-found]
        except Exception as e:
            raise VelrError(f"polars is required for to_polars(): {e}") from e
        return pl.from_arrow(self.to_pyarrow())

    def raw_memoryview(self) -> memoryview:
        """Return the raw IPC bytes as a memoryview."""
        return self._mv

    def to_bytes(self) -> bytes:
        """Return the raw IPC bytes as ``bytes``."""
        return bytes(self._mv)

    def write_to(self, fileobj) -> int:
        """Write the raw IPC bytes to a file-like object."""
        return fileobj.write(self._mv)

    def save(self, path: str) -> None:
        """Save the raw IPC bytes to a file."""
        with open(path, "wb") as f:
            f.write(self._mv)


@dataclass(frozen=True)
class Path:
    """A materialized Cypher path returned by ``to_rows()`` or ``to_records()``.

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
    """

    elements: tuple[dict[str, Any], ...]


class ErrorPosition(NamedTuple):
    """A source location in the complete submitted Cypher query.

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
    """

    line: int
    column: int


class VelrError(RuntimeError):
    """Base exception for Velr query, database, and result-handle failures.

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
    """

    _stop_code: str | None = None
    _category: str | None = None

    def __init__(
        self,
        *args: object,
        category: str | None = None,
        detail: str | None = None,
        phase: str | None = None,
        message: str | None = None,
        hint: str | None = None,
        position: ErrorPosition | tuple[int, int] | None = None,
        retryable: bool = False,
        status_code: int | None = None,
        sqlite_errorcode: int | None = None,
        sqlite_errorname: str | None = None,
        io_kind: str | None = None,
        os_code: int | None = None,
        operation: str | None = None,
        path: str | None = None,
    ) -> None:
        """Create an exception from diagnostic ``args`` and optional metadata.

        ``args`` are passed unchanged to ``RuntimeError``. The keyword
        arguments initialize the correspondingly named attributes documented on
        this class; omitted fields use ``None`` or ``False``. ``message`` defaults
        to ``str(error)``. ``position`` accepts ``ErrorPosition`` or a two-item
        tuple and is stored as ``ErrorPosition``. Subclasses supply a category
        when one is not supplied.
        The exception owns its strings and metadata independently of native handles.
        """
        super().__init__(*args)
        self.category = category if category is not None else self._category
        self.detail = detail
        self.phase = phase
        self.message = str(self) if message is None else message
        self.hint = hint
        self.position = (
            position if position is None or isinstance(position, ErrorPosition)
            else ErrorPosition(*position)
        )
        self.retryable = bool(retryable) and status_code not in {-5, -6} and self._stop_code is None
        self.status_code = status_code
        self.sqlite_errorcode = sqlite_errorcode
        self.sqlite_errorname = sqlite_errorname
        self.io_kind = io_kind
        self.os_code = os_code
        self.operation = operation
        self.path = path

    @property
    def explanation(self) -> str:
        """Return the human-readable explanation, an alias of ``message``.

        The original rendered diagnostic remains available in ``report`` and
        ``str(error)``. This property uses the same name as the remote client.
        """
        return self.message

    @property
    def report(self) -> str:
        """Return the complete rendered diagnostic, identical to ``str(error)``.

        Preserves the original exception arguments and display even when a
        separate concise ``explanation`` is available.
        """
        return str(self)

    @property
    def code(self) -> str | None:
        """Return ``"query.cancelled"`` or ``"query.deadline_exceeded"`` for a stop.

        Other errors return ``None``. Inspect ``category`` and ``detail`` for
        classified failures, and ``status_code`` for the numeric C ABI status.
        """
        return self._stop_code


class QueryError(VelrError):
    """Base exception for classified Cypher compilation and execution failures.

    Includes failures caused by query text, supplied values, and graph data.
    Inspect ``phase`` to distinguish compile-time from runtime failures, and
    ``category``/``detail`` for the stable reason. Metadata and constructor
    arguments are inherited from :class:`VelrError`.
    """


class CypherSyntaxError(QueryError):
    """The query violates the supported Cypher syntax or static query rules."""

    _category = "SyntaxError"


class CypherSemanticError(QueryError):
    """The query violates a Cypher semantic rule for its supplied values or graph."""

    _category = "SemanticError"


class CypherTypeError(QueryError):
    """A Cypher value has an unsupported type or cannot be converted."""

    _category = "TypeError"


class ParameterError(QueryError):
    """A query parameter is missing or a Cypher argument lies outside its domain.

    Invalid Python host arguments can instead raise ordinary ``TypeError`` or
    ``ValueError`` before execution. Inspect ``category`` and ``detail`` to
    distinguish missing parameters from argument-domain errors.
    """


class ProcedureNotFoundError(QueryError):
    """A Cypher ``CALL`` names a procedure unavailable on this connection."""

    _category = "ProcedureNotFound"


class EntityNotFoundError(QueryError):
    """A query refers to a graph entity that no longer exists."""

    _category = "EntityNotFound"


class ConstraintError(QueryError):
    """A query violates a graph constraint, such as deleting a connected node."""

    _category = "ConstraintVerificationFailed"


class CypherArithmeticError(QueryError):
    """A Cypher arithmetic operation is undefined or overflows."""

    _category = "ArithmeticError"


class DatabaseBusyError(VelrError):
    """A database operation cannot proceed because of database contention.

    Inspect ``retryable`` and ``sqlite_errorcode`` before deciding whether and at
    what boundary to retry. This exception does not guarantee safe replay.
    """

    _category = "DatabaseBusy"


class ReadOnlyError(VelrError):
    """An operation attempted a write through a read-only database connection."""

    _category = "ReadOnly"


class MigrationRequiredError(VelrError):
    """The database requires explicit migration before the requested operation."""

    _category = "MigrationRequired"


class HandleStateError(VelrError):
    """A handle is closed, on the wrong thread, or already serving an operation.

    Python-generated handle guards have ``status_code is None``. The original
    diagnostic is preserved, and ``detail`` identifies the failed guard.
    """

    _category = "HandleState"


class QueryCancelled(VelrError):
    """Query execution or result consumption stopped after cancellation.

    The inherited ``code`` property remains ``"query.cancelled"``.
    """

    _category = "QueryCancelled"
    _stop_code = "query.cancelled"


class DeadlineExceeded(VelrError, TimeoutError):
    """Query execution or result consumption exceeded its deadline.

    Catch this as either ``VelrError`` or ``TimeoutError``. The inherited ``code``
    property remains ``"query.deadline_exceeded"``.
    """

    _category = "DeadlineExceeded"
    _stop_code = "query.deadline_exceeded"


class CancellationToken:
    """Signal query cancellation from any thread.

    Pass the token as ``cancellation=`` when starting a query. Cancelling it
    signals every query using it, including deferred table and row reads.
    A cancelled token stays cancelled and causes later queries using it to stop.
    Create a fresh token for independent work. Construct it with no arguments.
    ``cancel()`` requests a stop without waiting for executing queries to finish;
    a query may finish before observing the signal. Check both execution and fetch
    errors for ``VelrError.code == "query.cancelled"``. The token is thread-safe;
    connections and active result handles remain subject to their own thread rules.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._cancelled = False
        self._queries: set[Any] = set()

    def cancel(self) -> None:
        """Signal a stop and return ``None``. Repeated calls are safe.

        Calls may come from any thread. The query or result reader that observes
        the stop raises :class:`VelrError` with ``code == "query.cancelled"``.
        This call does not wait for completion or invalidate previously copied results.
        """
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            for control in self._queries:
                lib.velr_query_control_cancel(control)

    def _attach(self, control) -> None:
        with self._lock:
            self._queries.add(control)
            if self._cancelled:
                lib.velr_query_control_cancel(control)

    def _free(self, control) -> None:
        with self._lock:
            self._queries.discard(control)
            lib.velr_query_control_free(control)


_handle_threads = local()
_callback_causes: dict[int, Exception] = {}
_callback_causes_lock = Lock()


def _take_callback_cause(pointer) -> Exception | None:
    if pointer == ffi.NULL:
        return None
    with _callback_causes_lock:
        return _callback_causes.pop(int(ffi.cast("size_t", pointer)), None)


class _HandleThread:
    __slots__ = (
        "ident", "connections", "deferred_closes", "draining_closes", "native_depth",
        "exiting", "exited", "_lock",
    )

    def __init__(self) -> None:
        self.ident = get_ident()
        self.connections = weakref.WeakSet()
        self.deferred_closes = deque()
        self.draining_closes = False
        self.native_depth = 0
        self.exiting = False
        self.exited = False
        self._lock = RLock()

    def defer_close(self, close) -> None:
        with self._lock:
            if not self.exited:
                self.deferred_closes.append(close)

    def drain_closes(self, *, finishing: bool = False) -> None:
        self.draining_closes = True
        try:
            while True:
                with self._lock:
                    if not self.deferred_closes:
                        if finishing:
                            self.exited = True
                        break
                    close = self.deferred_closes.popleft()
                try:
                    close()
                except Exception:
                    pass
        finally:
            self.draining_closes = False


class _CloseHandlesOnThreadExit:
    def __init__(self, owner: _HandleThread) -> None:
        self.owner = owner

    def __del__(self):
        owner = self.owner
        if sys.is_finalizing() or get_ident() != owner.ident:
            return
        owner.exiting = True
        try:
            owner.drain_closes()
            for connection in list(owner.connections):
                try:
                    connection._close_from_owner(owner, only_with_children=True)
                except Exception:
                    pass
            owner.drain_closes(finishing=True)
        finally:
            owner.exiting = False


def _current_handle_thread() -> _HandleThread:
    owner = getattr(_handle_threads, "owner", None)
    if owner is None:
        owner = _handle_threads.owner = _HandleThread()
        _handle_threads.exit_hook = _CloseHandlesOnThreadExit(owner)
    return owner


def _connection_operation(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        self._start_operation()
        try:
            return method(self, *args, **kwargs)
        finally:
            self._finish_operation()

    return guarded


class _ChildHandles:
    def __init__(self) -> None:
        self._refs = {}

    def __bool__(self) -> bool:
        return bool(self._refs)

    def add(self, child) -> None:
        # GC may clear the weak reference before the child queues native cleanup.
        self._refs[id(child)] = weakref.ref(child)

    def discard(self, child) -> None:
        self._refs.pop(id(child), None)

    def close(self) -> None:
        if not self._refs:
            return
        for ref in list(self._refs.values()):
            child = ref()
            if child is not None:
                child.close()
        if self._refs:
            raise HandleStateError("Velr has live handles awaiting cleanup on this thread", detail="PendingHandleCleanup")


class _NativeHandle:
    __slots__ = ("_owner", "_connection_handle")

    def _init_child(self, parent) -> None:
        self._parent = parent
        self._owner = parent._owner
        self._connection_handle = parent._connection()

    def _connection(self) -> Velr:
        return self._connection_handle

    def _call_native(self, function, /, *args, **kwargs):
        connection = self._connection()
        owner = self._owner
        with connection._state_lock:
            if connection._native_busy:
                raise HandleStateError("Velr connection is already in use", detail="ConnectionInUse")
            connection._native_busy = True
            connection._callback_error = None
        owner.native_depth += 1
        try:
            result = function(*args, **kwargs)
            cause = connection._callback_error
            if cause is not None:
                # Bind a Python callback cause to this particular owned error,
                # not an ambient last-error slot that cleanup could overwrite.
                for argument in args:
                    try:
                        is_error_pointer = ffi.typeof(argument) == ffi.typeof("char **")
                    except TypeError:
                        continue
                    if is_error_pointer and argument[0] != ffi.NULL:
                        with _callback_causes_lock:
                            _callback_causes[int(ffi.cast("size_t", argument[0]))] = cause
                        break
            return result
        finally:
            connection._callback_error = None
            owner.native_depth -= 1
            with connection._state_lock:
                connection._native_busy = False

    def _check_thread(self) -> None:
        owner = self._owner
        # The exit hook runs on the owner after its thread-local data has gone.
        if getattr(_handle_threads, "owner", None) is not owner and not (
            owner.exiting and get_ident() == owner.ident
        ):
            raise HandleStateError("Velr handles must be used on the thread that created them", detail="WrongThread")
        if self._connection()._native_busy:
            raise HandleStateError("Velr connection is already in use", detail="ConnectionInUse")
        if owner.deferred_closes and not owner.draining_closes and not owner.native_depth:
            owner.drain_closes()

    def __del__(self):
        try:
            if sys.is_finalizing():
                return
            owner = self._owner
            on_owner_thread = getattr(_handle_threads, "owner", None) is owner or (
                owner.exiting and get_ident() == owner.ident
            )
            if on_owner_thread and not self._connection()._native_busy:
                self.close()
            else:
                # Keep the handle and its parents alive until the owner can close it.
                owner.defer_close(self.close)
        except Exception:
            pass


@dataclass(frozen=True)
class MigrationReport:
    """Report returned by :meth:`Velr.migrate`.

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
    """

    from_version: int
    to_version: int
    status: str
    steps: List[str]


def _runtime_symbol(name: str):
    try:
        return getattr(lib, name)
    except AttributeError as e:
        raise VelrError(f"loaded Velr runtime does not expose {name}") from e


def _strview(data: bytes):
    buf = ffi.from_buffer("uint8_t[]", data)
    view = ffi.new("velr_strview *")
    view.ptr = buf
    view.len = len(data)
    return view[0], buf


def _validate_query_param_name(name: str) -> None:
    if not isinstance(name, str):
        raise TypeError("query parameter names must be strings")
    if not name:
        raise ValueError("query parameter name cannot be empty")
    if name.startswith("$"):
        raise ValueError("query parameter name should not include the leading '$'")


def _json_param_value(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if value < -(2**63) or value > 2**63 - 1:
            raise ValueError("integer query parameters must fit Cypher INTEGER")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("floating point query parameters must be finite")
        return value
    if depth >= 64 and isinstance(value, (Mapping, list, tuple)):
        raise ValueError("query parameter lists and maps can be nested at most 64 levels")
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("query parameter map keys must be strings")
            out[key] = _json_param_value(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_json_param_value(item, depth + 1) for item in value]
    raise TypeError(f"unsupported query parameter value type: {type(value).__name__}")


def _json_param_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_param_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _is_query_map(value: Any) -> bool:
    return isinstance(value, Mapping)


def _is_list_of_query_maps(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and all(_is_query_map(row) for row in value)


def _validate_row_map_keys(rows: Sequence[Mapping[str, Any]]) -> None:
    for row in rows:
        for key in row.keys():
            if not isinstance(key, str):
                raise TypeError("query parameter row-map keys must be strings")


def _query_param_row_map_required_columns(cypher: str, param_name: str) -> list[str] | None:
    cache_key = (param_name, cypher)
    if cache_key in _row_map_required_columns_cache:
        return _row_map_required_columns_cache[cache_key]

    cypher_view, cypher_buf = _strview(cypher.encode("utf-8"))
    param_name_view, param_name_buf = _strview(param_name.encode("utf-8"))
    out_json = ffi.new("char **")
    out_err = ffi.new("char **")
    rc = _runtime_symbol("velr_query_param_row_map_required_columns")(
        cypher_view,
        param_name_view,
        out_json,
        out_err,
    )
    _ = (cypher_buf, param_name_buf)
    _check(rc, out_err)

    columns: list[str] | None = None
    if out_json[0] != ffi.NULL:
        try:
            decoded = json.loads(ffi.string(out_json[0]).decode("utf-8"))
            if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
                raise VelrError("row-map required columns did not decode to a string array")
            columns = decoded
        finally:
            lib.velr_string_free(out_json[0])

    _row_map_required_columns_cache[cache_key] = columns
    if len(_row_map_required_columns_cache) > _ROW_MAP_REQUIRED_COLUMNS_CACHE_MAX:
        del _row_map_required_columns_cache[next(iter(_row_map_required_columns_cache))]
    return columns


def _row_map_column_hints_for_cypher(
    cypher: str | None,
    params: Mapping[str, Any],
) -> dict[str, list[str]] | None:
    if cypher is None:
        return None

    hints: dict[str, list[str]] | None = None
    for name, value in params.items():
        _validate_query_param_name(name)
        if not _is_list_of_query_maps(value):
            continue
        columns = _query_param_row_map_required_columns(cypher, name)
        if columns is None:
            continue
        if hints is None:
            hints = {}
        hints[name] = columns
    return hints


def _fill_param_cell(cell, value: Any, backing: list, row_index: int, column: str) -> None:
    if value is None:
        cell.ty = lib.VELR_NULL
    elif isinstance(value, bool):
        cell.ty = lib.VELR_BOOL
        cell.i64_ = 1 if value else 0
    elif isinstance(value, int):
        if value < _I64_MIN or value > _I64_MAX:
            raise ValueError(f"query parameter row {row_index}.{column} is outside signed 64-bit range")
        cell.ty = lib.VELR_INT64
        cell.i64_ = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"query parameter row {row_index}.{column} must be finite")
        cell.ty = lib.VELR_DOUBLE
        cell.f64_ = value
    elif isinstance(value, str):
        data = value.encode("utf-8")
        cell.ty = lib.VELR_TEXT
        cell.len = len(data)
        if data:
            data_view = ffi.from_buffer("uint8_t[]", data)
            backing.append((data, data_view))
            cell.ptr = data_view
        else:
            cell.ptr = ffi.NULL
    elif isinstance(value, (Mapping, list, tuple)):
        data = _json_param_bytes(value)
        cell.ty = lib.VELR_JSON
        cell.len = len(data)
        if data:
            data_view = ffi.from_buffer("uint8_t[]", data)
            backing.append((data, data_view))
            cell.ptr = data_view
        else:
            cell.ptr = ffi.NULL
    else:
        raise TypeError(f"unsupported query parameter value type: {type(value).__name__}")


def _write_packed_param_cell(
    value: Any,
    cell_index: int,
    cell_types,
    i64_values,
    f64_values,
    byte_offsets,
    byte_lens,
    byte_buffer: bytearray,
    row_index: int,
    column: str,
) -> bool:
    if value is None:
        cell_types[cell_index] = lib.VELR_NULL
        return True
    if isinstance(value, bool):
        cell_types[cell_index] = lib.VELR_BOOL
        i64_values[cell_index] = 1 if value else 0
        return True
    if isinstance(value, int):
        if value < _I64_MIN or value > _I64_MAX:
            raise ValueError(f"query parameter row {row_index}.{column} is outside signed 64-bit range")
        cell_types[cell_index] = lib.VELR_INT64
        i64_values[cell_index] = value
        return True
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"query parameter row {row_index}.{column} must be finite")
        cell_types[cell_index] = lib.VELR_DOUBLE
        f64_values[cell_index] = value
        return True
    if isinstance(value, str):
        cell_types[cell_index] = lib.VELR_TEXT
        data = value.encode("utf-8")
    elif isinstance(value, (Mapping, list, tuple)):
        cell_types[cell_index] = lib.VELR_JSON
        data = _json_param_bytes(value)
    else:
        raise TypeError(f"unsupported query parameter value type: {type(value).__name__}")

    offset = len(byte_buffer)
    length = len(data)
    if offset > _U32_MAX or length > _U32_MAX:
        return False
    byte_offsets[cell_index] = offset
    byte_lens[cell_index] = length
    byte_buffer.extend(data)
    return True


def _column_name_views(columns: Sequence[str]):
    if not columns:
        return ffi.NULL, []
    col_views = ffi.new("velr_strview[]", len(columns))
    backing = []
    for idx, column in enumerate(columns):
        if not isinstance(column, str):
            raise TypeError("query parameter row-map column names must be strings")
        data = column.encode("utf-8")
        buf = ffi.new("uint8_t[]", data)
        backing.append(buf)
        col_views[idx].ptr = buf
        col_views[idx].len = len(data)
    return col_views, backing


def _try_set_query_param_packed_row_map_columns(
    handle,
    name_view,
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
    preserve_missing: bool,
) -> bool:
    total_cells = len(rows) * len(columns)
    col_views, col_backing = _column_name_views(columns)
    cell_types = ffi.NULL if total_cells == 0 else ffi.new("uint8_t[]", total_cells)
    i64_values = ffi.NULL if total_cells == 0 else ffi.new("int64_t[]", total_cells)
    f64_values = ffi.NULL if total_cells == 0 else ffi.new("double[]", total_cells)
    byte_offsets = ffi.NULL if total_cells == 0 else ffi.new("uint32_t[]", total_cells)
    byte_lens = ffi.NULL if total_cells == 0 else ffi.new("uint32_t[]", total_cells)
    present = ffi.NULL
    if preserve_missing and total_cells > 0:
        present = ffi.new("uint8_t[]", total_cells)
    byte_buffer = bytearray()

    column_set = set(columns)
    for row_index, row in enumerate(rows):
        if not preserve_missing and (len(row) != len(columns) or set(row.keys()) != column_set):
            return False
        for column_index, column in enumerate(columns):
            cell_index = row_index * len(columns) + column_index
            if column not in row:
                if preserve_missing:
                    cell_types[cell_index] = lib.VELR_NULL
                    continue
                return False
            if present != ffi.NULL:
                present[cell_index] = 1
            if not _write_packed_param_cell(
                row[column],
                cell_index,
                cell_types,
                i64_values,
                f64_values,
                byte_offsets,
                byte_lens,
                byte_buffer,
                row_index,
                column,
            ):
                return False

    bytes_ptr = ffi.NULL if not byte_buffer else ffi.from_buffer("uint8_t[]", byte_buffer)
    out_err = ffi.new("char **")
    rc = _runtime_symbol("velr_query_params_set_row_map_packed_columns_u8_u32")(
        handle,
        name_view,
        len(rows),
        col_views,
        len(columns),
        cell_types,
        i64_values,
        f64_values,
        byte_offsets,
        byte_lens,
        bytes_ptr,
        len(byte_buffer),
        present,
        out_err,
    )
    _ = col_backing
    _check(rc, out_err)
    return True


def _set_query_param_row_map_columns(
    handle,
    name_view,
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
    preserve_missing: bool,
) -> None:
    if _try_set_query_param_packed_row_map_columns(handle, name_view, rows, columns, preserve_missing):
        return

    total_cells = len(rows) * len(columns)
    col_views, col_backing = _column_name_views(columns)
    cells = ffi.NULL if total_cells == 0 else ffi.new("velr_cell[]", total_cells)
    present = ffi.NULL
    if preserve_missing and total_cells > 0:
        present = ffi.new("uint8_t[]", total_cells)
    cell_backing = []

    for row_index, row in enumerate(rows):
        for column_index, column in enumerate(columns):
            cell_index = row_index * len(columns) + column_index
            if column not in row:
                if preserve_missing:
                    cells[cell_index].ty = lib.VELR_NULL
                    continue
                raise TypeError("query parameter row-map rows must have the same shape")
            if present != ffi.NULL:
                present[cell_index] = 1
            _fill_param_cell(cells[cell_index], row[column], cell_backing, row_index, column)

    out_err = ffi.new("char **")
    rc = _runtime_symbol("velr_query_params_set_row_map_columns")(
        handle,
        name_view,
        len(rows),
        col_views,
        len(columns),
        cells,
        present,
        out_err,
    )
    _ = (col_backing, cell_backing)
    _check(rc, out_err)


def _try_set_query_param_dense_row_map_columns(handle, name_view, rows: Sequence[Mapping[str, Any]]) -> bool:
    columns = list(rows[0].keys()) if rows else []
    if any(not isinstance(column, str) for column in columns):
        raise TypeError("query parameter row-map keys must be strings")
    column_set = set(columns)
    if len(column_set) != len(columns):
        return False
    for row in rows:
        if len(row) != len(columns) or set(row.keys()) != column_set:
            return False
    _set_query_param_row_map_columns(handle, name_view, rows, columns, False)
    return True


def _set_query_param_sparse_row_maps(handle, name_view, rows: Sequence[Mapping[str, Any]]) -> None:
    entries_count = sum(len(row) for row in rows)
    entries = ffi.NULL if entries_count == 0 else ffi.new("velr_param_map_entry[]", entries_count)
    backing = []
    entry_index = 0
    for row_index, row in enumerate(rows):
        for key, value in row.items():
            key_data = key.encode("utf-8")
            key_buf = ffi.new("uint8_t[]", key_data)
            backing.append(key_buf)
            entry = entries[entry_index]
            entry.row_index = row_index
            entry.key.ptr = key_buf
            entry.key.len = len(key_data)
            _fill_param_cell(entry.value, value, backing, row_index, key)
            entry_index += 1

    out_err = ffi.new("char **")
    rc = _runtime_symbol("velr_query_params_set_row_maps")(
        handle,
        name_view,
        len(rows),
        entries,
        entries_count,
        out_err,
    )
    _ = backing
    _check(rc, out_err)


def _set_query_param_row_maps(
    handle,
    name_view,
    rows: Sequence[Mapping[str, Any]],
    projected_columns: Sequence[str] | None,
) -> None:
    _validate_row_map_keys(rows)
    if projected_columns is not None:
        _set_query_param_row_map_columns(handle, name_view, rows, list(projected_columns), True)
        return

    if _try_set_query_param_dense_row_map_columns(handle, name_view, rows):
        return

    columns: list[str] = []
    seen: set[str] = set()
    entry_count = 0
    for row in rows:
        for key in row.keys():
            entry_count += 1
            if key not in seen:
                seen.add(key)
                columns.append(key)

    if len(rows) * len(columns) <= entry_count * 2:
        _set_query_param_row_map_columns(handle, name_view, rows, columns, True)
        return

    _set_query_param_sparse_row_maps(handle, name_view, rows)


def _set_query_param(
    handle,
    name: str,
    value: Any,
    row_map_columns: Sequence[str] | None = None,
) -> None:
    _validate_query_param_name(name)

    name_view, name_buf = _strview(name.encode("utf-8"))
    out_err = ffi.new("char **")
    if value is None:
        rc = _runtime_symbol("velr_query_params_set_null")(handle, name_view, out_err)
    elif isinstance(value, bool):
        rc = _runtime_symbol("velr_query_params_set_bool")(handle, name_view, int(value), out_err)
    elif isinstance(value, int):
        if value < -(2**63) or value > 2**63 - 1:
            raise ValueError("integer query parameters must fit Cypher INTEGER")
        rc = _runtime_symbol("velr_query_params_set_i64")(handle, name_view, value, out_err)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("floating point query parameters must be finite")
        rc = _runtime_symbol("velr_query_params_set_f64")(handle, name_view, value, out_err)
    elif isinstance(value, str):
        value_view, value_buf = _strview(value.encode("utf-8"))
        rc = _runtime_symbol("velr_query_params_set_text")(handle, name_view, value_view, out_err)
        _ = value_buf
    elif _is_list_of_query_maps(value) and (row_map_columns is not None or len(value) > 0):
        _set_query_param_row_maps(handle, name_view, value, row_map_columns)
        _ = name_buf
        return
    elif isinstance(value, (Mapping, list, tuple)):
        encoded = _json_param_bytes(value)
        value_view, value_buf = _strview(encoded)
        rc = _runtime_symbol("velr_query_params_set_json")(handle, name_view, value_view, out_err)
        _ = value_buf
    else:
        raise TypeError(f"unsupported query parameter value type: {type(value).__name__}")
    _ = name_buf
    _check(rc, out_err)


def _query_params(params: Mapping[str, Any] | None, cypher: str | None = None):
    if params is None:
        return ffi.NULL
    if not isinstance(params, Mapping):
        raise TypeError("params must be a mapping or None")
    if not params:
        return ffi.NULL

    row_map_hints = _row_map_column_hints_for_cypher(cypher, params)
    handle = _runtime_symbol("velr_query_params_new")()
    if handle == ffi.NULL:
        raise VelrError("velr_query_params_new returned null")
    try:
        for name, value in params.items():
            _set_query_param(handle, name, value, row_map_hints.get(name) if row_map_hints else None)
    except Exception:
        _runtime_symbol("velr_query_params_free")(handle)
        raise
    return handle


def _free_query_params(params_handle) -> None:
    if params_handle != ffi.NULL:
        _runtime_symbol("velr_query_params_free")(params_handle)


def _query_options(
    max_result_rows: int | None,
    params: Mapping[str, Any] | None = None,
    cypher: str | None = None,
):
    params_handle = _query_params(params, cypher)
    if max_result_rows is None and params_handle == ffi.NULL:
        return ffi.NULL, ffi.NULL
    if max_result_rows is not None and not isinstance(max_result_rows, int):
        _free_query_params(params_handle)
        raise TypeError("max_result_rows must be an int or None")
    if max_result_rows is not None and max_result_rows < 0:
        _free_query_params(params_handle)
        raise ValueError("max_result_rows must be >= 0")

    opts = ffi.new("velr_query_options *")
    opts.has_max_result_rows = 0 if max_result_rows is None else 1
    opts.max_result_rows = 0 if max_result_rows is None else max_result_rows
    opts.params = params_handle
    return opts, params_handle


def _query_control(timeout: float | None, cancellation: CancellationToken | None):
    if timeout is None and cancellation is None:
        return None
    if cancellation is not None and not isinstance(cancellation, CancellationToken):
        raise TypeError("cancellation must be a CancellationToken or None")
    timeout_ns = 0
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a number of seconds or None")
        max_ns = (1 << 64) - 1
        if not 0 <= timeout <= max_ns / 1_000_000_000:
            raise ValueError("timeout must be a finite non-negative number of seconds within the supported range")
        timeout_ns = max(1, min(max_ns, math.ceil(timeout * 1_000_000_000)))

    free = _runtime_symbol("velr_query_control_free")
    if cancellation is not None:
        _runtime_symbol("velr_query_control_cancel")
    control = _runtime_symbol("velr_query_control_new")(timeout_ns)
    if control == ffi.NULL:
        raise VelrError("the runtime could not create query cancellation control")
    if cancellation is None:
        return ffi.gc(control, free)
    cancellation._attach(control)
    return ffi.gc(control, cancellation._free)


def _call_query(
    call_native,
    name: str,
    handle,
    cypher: str,
    max_result_rows: int | None,
    params: Mapping[str, Any] | None,
    timeout: float | None,
    cancellation: CancellationToken | None,
    *outputs,
):
    control = _query_control(timeout, cancellation)
    cypher_utf8 = cypher.encode("utf-8")
    opts, params_handle = _query_options(max_result_rows, params, cypher)
    out_err = ffi.new("char **")
    try:
        if control is not None:
            rc = call_native(
                _runtime_symbol(f"{name}_with_control"), handle, cypher_utf8, opts, control, *outputs, out_err
            )
        elif opts != ffi.NULL:
            rc = call_native(
                _runtime_symbol(f"{name}_with_options"), handle, cypher_utf8, opts, *outputs, out_err
            )
        else:
            rc = call_native(_runtime_symbol(name), handle, cypher_utf8, *outputs, out_err)
    finally:
        _free_query_params(params_handle)
    _check(rc, out_err)
    return control


def _free_unexpected_err(errpp) -> None:
    """Free a stray error string returned on success."""
    if errpp[0] != ffi.NULL:
        _take_callback_cause(errpp[0])
        lib.velr_string_free(errpp[0])
        errpp[0] = ffi.NULL


def _take_err(errpp) -> str:
    """Take, decode, free, and clear a runtime-owned error string."""
    if errpp[0] == ffi.NULL:
        return "(unknown)"
    s = ffi.string(errpp[0]).decode("utf-8", errors="replace")
    _take_callback_cause(errpp[0])
    lib.velr_string_free(errpp[0])
    errpp[0] = ffi.NULL
    return s


def _error_details(error_ptr) -> dict[str, Any]:
    """Copy supported metadata while its owning native error is still alive."""
    if error_ptr == ffi.NULL:
        return {}
    try:
        accessor = lib.velr_error_details_json
    except (AttributeError, OSError):
        return {}
    try:
        payload = accessor(error_ptr)
        if payload == ffi.NULL:
            return {}
        try:
            details = json.loads(ffi.string(payload).decode("utf-8"))
        finally:
            lib.velr_string_free(payload)
    except (OSError, UnicodeError, ValueError, TypeError):
        # The original failure remains authoritative if metadata is unavailable
        # or comes from an incompatible runtime. Never classify diagnostic prose.
        return {}
    if not isinstance(details, dict) or type(details.get("version")) is not int:
        return {}
    return details if details["version"] == 1 else {}


_ERROR_TYPES = {
    "SyntaxError": CypherSyntaxError,
    "SemanticError": CypherSemanticError,
    "TypeError": CypherTypeError,
    "ArgumentError": ParameterError,
    "ParameterMissing": ParameterError,
    "ProcedureNotFound": ProcedureNotFoundError,
    "ProcedureError": QueryError,
    "EntityNotFound": EntityNotFoundError,
    "ConstraintVerificationFailed": ConstraintError,
    "ArithmeticError": CypherArithmeticError,
    "DatabaseBusy": DatabaseBusyError,
    "ReadOnly": ReadOnlyError,
    "ReadOnlyError": ReadOnlyError,
    "MigrationRequired": MigrationRequiredError,
    "MigrationRequiredError": MigrationRequiredError,
    "HandleState": HandleStateError,
    "HandleStateError": HandleStateError,
    "DatabaseBusyError": DatabaseBusyError,
    "QueryCancelled": QueryCancelled,
    "DeadlineExceeded": DeadlineExceeded,
}


def _make_error(diagnostic: str, rc: int, details: dict[str, Any]) -> VelrError:
    """Construct an owned exception without interpreting rendered diagnostics."""
    def text(name):
        value = details.get(name)
        return value if isinstance(value, str) and value else None

    category = text("category")
    detail = text("detail")
    error_type = _ERROR_TYPES.get(category, VelrError)
    if category == "ArgumentError" and details.get("kind") is not None and details.get("kind") != "query":
        error_type = VelrError
    if category == "DatabaseStateError" and detail == "MigrationRequired":
        error_type = MigrationRequiredError
    if category == "ProcedureError" and detail == "ProcedureNotFound":
        error_type = ProcedureNotFoundError
    if int(rc) == int(lib.VELR_ECANCELLED):
        error_type = QueryCancelled
    elif int(rc) == int(lib.VELR_EDEADLINE):
        error_type = DeadlineExceeded
    elif error_type is VelrError and int(rc) == int(lib.VELR_ESTATE):
        error_type = HandleStateError

    phase = text("phase")
    if phase not in {"compile", "runtime"}:
        phase = None
    position = details.get("position")
    if not isinstance(position, dict):
        position = {"line": details.get("line"), "column": details.get("column")}
    if (
        isinstance(position, dict)
        and type(position.get("line")) is int
        and type(position.get("column")) is int
        and position["line"] > 0
        and position["column"] > 0
    ):
        position = (position["line"], position["column"])
    else:
        position = None
    sqlite_errorcode = details.get("sqlite_errorcode")
    if type(sqlite_errorcode) is not int:
        sqlite_errorcode = None
    os_code = details.get("os_code")
    if type(os_code) is not int:
        os_code = None

    return error_type(
        diagnostic,
        category=category,
        detail=detail,
        phase=phase,
        message=text("message"),
        hint=text("hint"),
        position=position,
        retryable=details.get("retryable") is True,
        status_code=int(rc),
        sqlite_errorcode=sqlite_errorcode,
        sqlite_errorname=text("sqlite_errorname"),
        io_kind=text("io_kind"),
        os_code=os_code,
        operation=text("operation"),
        path=text("path"),
    )


def _check(rc: int, errpp) -> None:
    """Raise ``VelrError`` if a standard runtime call failed."""
    if int(rc) == int(lib.VELR_OK):
        _free_unexpected_err(errpp)
        return
    cause = _take_callback_cause(errpp[0])
    try:
        details = _error_details(errpp[0])
    finally:
        diagnostic = _take_err(errpp)
    error = _make_error(diagnostic, int(rc), details)
    if cause is None and details.get("kind") == "io" and error.os_code is not None:
        if sys.platform.startswith("win"):
            cause = OSError(None, error.message, error.path, error.os_code)
        else:
            cause = OSError(error.os_code, error.message, error.path)
    if cause is not None:
        raise error from cause
    raise error


def _check_noerr(rc: int, context: str) -> None:
    """Raise ``VelrError`` for calls that do not return ``out_err``."""
    if int(rc) != int(lib.VELR_OK):
        raise _make_error(f"{context} (code {int(rc)})", int(rc), {})


def _strview_to_bytes(v, what: str) -> bytes:
    """Copy a borrowed string view into Python bytes."""
    n = int(v.len)
    if n == 0:
        return b""
    if v.ptr == ffi.NULL:
        raise VelrError(f"{what} is null with non-zero length")
    return bytes(ffi.buffer(v.ptr, n))


def _strview_to_string(v, what: str) -> str:
    """Copy and decode a borrowed string view as UTF-8."""
    try:
        return _strview_to_bytes(v, what).decode("utf-8", "strict")
    except UnicodeDecodeError as e:
        raise VelrError(f"{what} is not valid UTF-8: {e}") from e


def _opt_strview_to_string(v, what: str) -> str | None:
    """Decode an optional borrowed string view."""
    if v.ptr == ffi.NULL and int(v.len) == 0:
        return None
    return _strview_to_string(v, what)


def _cell_bytes(ptr, length: int, what: str) -> bytes:
    """Copy a runtime-owned byte buffer into Python bytes."""
    n = int(length)
    if n == 0:
        return b""
    if ptr == ffi.NULL:
        raise VelrError(f"{what} is null with non-zero length")
    return bytes(ffi.buffer(ptr, n))


_CELL_NULL = lib.VELR_NULL
_CELL_BOOL = lib.VELR_BOOL
_CELL_INT64 = lib.VELR_INT64
_CELL_DOUBLE = lib.VELR_DOUBLE
_CELL_TEXT = lib.VELR_TEXT
_CELL_JSON = lib.VELR_JSON
_JSON_TEMPLATE_COLUMNS = 32
_JSON_TEMPLATE_BYTES = 16 * 1024
_JSON_TEMPLATE_MISSES = 8
_JSON_TEMPLATE_UNREAD = object()
_JSON_TEMPLATE_DISABLED = object()


def _json_copy_plan(value):
    if type(value) is dict:
        entries = value.items()
    elif type(value) is list:
        entries = enumerate(value)
    else:
        return value

    shallow = value.copy()
    children = []
    for key, member in entries:
        if type(member) in (dict, list):
            shallow[key] = None
            children.append((key, _json_copy_plan(member)))
    return shallow, children


def _copy_planned_json(plan):
    if type(plan) is not tuple:
        return plan
    shallow, children = plan
    value = shallow.copy()
    for key, child in children:
        value[key] = _copy_planned_json(child)
    return value


class _ParsedJsonTemplate:
    __slots__ = ("payload", "plan", "reference", "__weakref__")

    def __init__(self, payload):
        self.payload = payload
        self.plan = _JSON_TEMPLATE_UNREAD
        self.reference = weakref.ref(self)

    def value(self):
        plan = self.plan
        if plan is _JSON_TEMPLATE_UNREAD:
            value = json.loads(self.payload)
            try:
                self.plan = _json_copy_plan(value)
            except RecursionError:
                self.plan = _JSON_TEMPLATE_DISABLED
            return value
        if plan is not _JSON_TEMPLATE_DISABLED:
            try:
                return _copy_planned_json(plan)
            except RecursionError:
                pass
        return json.loads(self.payload)


class _JsonTemplateColumn:
    __slots__ = ("previous", "repeated", "template", "misses")

    def __init__(self):
        self.previous = None
        self.repeated = False
        self.template = None
        self.misses = 0


@dataclass(frozen=True)
class Cell:
    """One cell value in a result row.

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
    """

    __slots__ = ("__dict__", "__weakref__", "_json_template")

    ty: int
    i64: int
    f64: float
    data: bytes

    def __getstate__(self):
        state = super().__getstate__()
        if isinstance(state, tuple):
            fields, slots = state
            slots.pop("_json_template", None)
            return (fields, slots) if slots else fields
        return state

    def as_python(
        self,
        *,
        decode_text: bool = True,
        parse_json: bool = False,
        encoding: str = "utf-8",
        errors: str = "strict",
    ):
        """Convert this copied cell to a Python value.

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
        """
        ty = self.ty
        if ty == _CELL_JSON:
            if parse_json:
                if encoding == "utf-8" and errors == "strict":
                    reference = getattr(self, "_json_template", None)
                    template = reference() if reference is not None else None
                    if template is not None and self.data is template.payload:
                        return template.value()
                    return json.loads(self.data)
                return json.loads(self.data.decode(encoding, errors))
            return self.data.decode(encoding, errors) if decode_text else bytes(self.data)
        if ty == _CELL_NULL:
            return None
        if ty == _CELL_BOOL:
            return bool(self.i64)
        if ty == _CELL_INT64:
            return int(self.i64)
        if ty == _CELL_DOUBLE:
            return float(self.f64)
        if ty == _CELL_TEXT:
            return self.data.decode(encoding, errors) if decode_text else bytes(self.data)
        return bytes(self.data)


@dataclass(frozen=True)
class VectorEmbeddingField:
    """One property value supplied to an embedding callback.

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
    """

    name: str | None
    value: Any
    value_type: str
    value_json: Any
    display: str


@dataclass(frozen=True)
class VectorEmbeddingInput:
    """One input in a vector embedding callback batch.

    ``index_name`` names the index and ``dimensions`` is the required output
    vector length. ``purpose`` is ``"index_entity"`` or ``"query"``.
    ``entity_kind`` is ``"node"``, ``"relationship"``, or ``None``; ``entity_id``
    is an integer identity when present, otherwise ``None``. ``fields`` contains
    :class:`VectorEmbeddingField` objects in ``ON EACH [...]`` order. Query text
    has one unnamed string field. Return one finite numeric vector per input,
    in the same order. Inputs and their fields are copied Python data and can
    be retained after the callback returns. Stored-vector indexes accept query
    vectors directly without invoking an embedding callback.
    """

    index_name: str
    dimensions: int
    purpose: str
    entity_kind: str | None
    entity_id: int | None
    fields: list[VectorEmbeddingField]


_VECTOR_PURPOSE_NAMES = {
    "VELR_VECTOR_EMBEDDING_INDEX_ENTITY": "index_entity",
    "VELR_VECTOR_EMBEDDING_QUERY": "query",
}

_VECTOR_ENTITY_KIND_NAMES = {
    "VELR_VECTOR_ENTITY_NONE": None,
    "VELR_VECTOR_ENTITY_NODE": "node",
    "VELR_VECTOR_ENTITY_RELATIONSHIP": "relationship",
}

_VECTOR_VALUE_TYPE_NAMES = {
    "VELR_PROPERTY_NULL": "null",
    "VELR_PROPERTY_BOOL": "bool",
    "VELR_PROPERTY_INT64": "int64",
    "VELR_PROPERTY_DOUBLE": "double",
    "VELR_PROPERTY_STRING": "string",
    "VELR_PROPERTY_DATE": "date",
    "VELR_PROPERTY_LOCAL_TIME": "local_time",
    "VELR_PROPERTY_ZONED_TIME": "zoned_time",
    "VELR_PROPERTY_LOCAL_DATETIME": "local_datetime",
    "VELR_PROPERTY_ZONED_DATETIME": "zoned_datetime",
    "VELR_PROPERTY_DURATION": "duration",
    "VELR_PROPERTY_POINT": "point",
    "VELR_PROPERTY_GEOMETRY": "geometry",
    "VELR_PROPERTY_GEOGRAPHY": "geography",
    "VELR_PROPERTY_LIST": "list",
    "VELR_PROPERTY_VECTOR": "vector",
    "VELR_PROPERTY_BYTES": "bytes",
}


def _enum_name(mapping: dict[str, Any], enum_value, fallback: str) -> Any:
    value = int(enum_value)
    for name, mapped in mapping.items():
        if int(getattr(lib, name)) == value:
            return mapped
    return fallback


def _vector_field_json(field) -> Any:
    text = _strview_to_string(field.json, "vector field JSON")
    return json.loads(text)


def _vector_field_value(field, value_type: str, value_json: Any) -> Any:
    if value_type == "null":
        return None
    if value_type == "bool":
        return bool(value_json)
    if value_type == "int64":
        return int(field.i64_)
    if value_type == "double":
        return float(field.f64_)
    if value_type == "string":
        return _strview_to_bytes(field.bytes, "vector field text storage").decode(
            "utf-8",
            "strict",
        )
    if value_type == "bytes":
        return _strview_to_bytes(field.bytes, "vector field blob storage")
    return value_json


def _vector_field_from_raw(field) -> VectorEmbeddingField:
    value_type = _enum_name(_VECTOR_VALUE_TYPE_NAMES, field.value_type, "unknown")
    value_json = _vector_field_json(field)
    return VectorEmbeddingField(
        name=_strview_to_string(field.name, "vector field name")
        if int(field.has_name) != 0
        else None,
        value=_vector_field_value(field, value_type, value_json),
        value_type=value_type,
        value_json=value_json,
        display=_strview_to_string(field.display, "vector field display"),
    )


def _vector_input_from_raw(raw) -> VectorEmbeddingInput:
    fields = []
    field_count = int(raw.field_count)
    if field_count:
        if raw.fields == ffi.NULL:
            raise VelrError("vector embedding fields pointer is null with non-zero count")
        fields = [_vector_field_from_raw(raw.fields[i]) for i in range(field_count)]
    return VectorEmbeddingInput(
        index_name=_strview_to_string(raw.index_name, "vector index name"),
        dimensions=int(raw.dimensions),
        purpose=_enum_name(_VECTOR_PURPOSE_NAMES, raw.purpose, "unknown"),
        entity_kind=_enum_name(_VECTOR_ENTITY_KIND_NAMES, raw.entity_kind, "unknown"),
        entity_id=int(raw.entity_id) if int(raw.has_entity_id) != 0 else None,
        fields=fields,
    )


def _write_vector_callback_error(err_buf, err_buf_len: int, message: str) -> None:
    if err_buf == ffi.NULL or int(err_buf_len) <= 0:
        return
    encoded = message.encode("utf-8", errors="replace")
    limit = max(0, int(err_buf_len) - 1)
    chunk = encoded[:limit]
    if chunk:
        ffi.memmove(err_buf, chunk, len(chunk))
    err_buf[len(chunk)] = b"\0"


def _make_vector_callback(
    embedder: Callable[[list[VectorEmbeddingInput]], Sequence[Sequence[float]]],
    connection: "Velr",
):
    connection_ref = weakref.ref(connection)
    @ffi.callback(
        "velr_code(void*, const velr_vector_embedding_input*, size_t, size_t, float*, char*, size_t)"
    )
    def _callback(
        _user_data,
        raw_inputs,
        input_count,
        dimensions,
        out_vectors,
        err_buf,
        err_buf_len,
    ):
        try:
            count = int(input_count)
            dims = int(dimensions)
            if count and raw_inputs == ffi.NULL:
                raise VelrError("vector embedding inputs pointer is null with non-zero count")
            output_len = count * dims
            if output_len and out_vectors == ffi.NULL:
                raise VelrError("vector embedding output pointer is null")
            inputs = [_vector_input_from_raw(raw_inputs[i]) for i in range(count)]
            vectors = embedder(inputs)
            if len(vectors) != count:
                raise VelrError(
                    f"vector embedder returned {len(vectors)} embeddings for {count} inputs"
                )
            for row_idx, vector in enumerate(vectors):
                if len(vector) != dims:
                    raise VelrError(
                        f"vector embedder returned {len(vector)} dimensions for input {row_idx} "
                        f"but the index expects {dims}"
                    )
                for dim_idx, value in enumerate(vector):
                    value = float(value)
                    if not math.isfinite(value):
                        raise VelrError(
                            f"vector embedder returned a non-finite value for input {row_idx} "
                            f"at dimension {dim_idx}: {value}"
                        )
                    if output_len:
                        out_vectors[row_idx * dims + dim_idx] = value
            return lib.VELR_OK
        except Exception as e:
            owner = connection_ref()
            if owner is not None:
                owner._callback_error = e
            _write_vector_callback_error(err_buf, err_buf_len, str(e))
            return lib.VELR_EERR

    return _callback


@dataclass(frozen=True)
class ExplainPlanMeta:
    """Copied metadata for one explain plan.

    ``plan_id`` identifies the plan, ``cypher`` is its query text, and
    ``step_count`` is the number of steps. Instances remain usable after the
    trace is closed.
    """

    plan_id: str
    cypher: str
    step_count: int


@dataclass(frozen=True)
class ExplainStepMeta:
    """Copied metadata for one explain step.

    ``step_no`` is its displayed step number; ``group_id`` and ``op_index``
    identify its group and operation. ``phase``, ``title``, and ``source``
    describe the step; ``note`` is optional. ``statement_count`` gives the number
    of associated statements. Treat diagnostic identifiers and wording as
    informational rather than stable application identifiers.
    """

    step_no: int
    group_id: str
    op_index: str
    phase: str
    title: str
    source: str
    note: str | None
    statement_count: int


@dataclass(frozen=True)
class ExplainStatementMeta:
    """Copied metadata for one explain statement.

    ``stmt_id`` identifies the statement, ``kind`` describes it, ``sql`` contains
    its SQL text, ``note`` is optional, and ``sqlite_plan_count`` is the number of
    available SQLite plan-detail lines. Diagnostic text is version-specific.
    """

    stmt_id: str
    kind: str
    sql: str
    note: str | None
    sqlite_plan_count: int


@dataclass(frozen=True)
class ExplainStatement:
    """An owned explain statement snapshot.

    ``meta`` is :class:`ExplainStatementMeta`; ``sqlite_plan`` is a list of plan
    detail strings. The snapshot remains usable after the trace is closed.
    """

    meta: ExplainStatementMeta
    sqlite_plan: list[str]


@dataclass(frozen=True)
class ExplainStep:
    """An owned explain step snapshot.

    ``meta`` is :class:`ExplainStepMeta` and ``statements`` contains
    :class:`ExplainStatement` snapshots, in trace order.
    """

    meta: ExplainStepMeta
    statements: list[ExplainStatement]


@dataclass(frozen=True)
class ExplainPlan:
    """An owned explain plan snapshot.

    ``meta`` is :class:`ExplainPlanMeta` and ``steps`` contains
    :class:`ExplainStep` snapshots, in trace order.
    """

    meta: ExplainPlanMeta
    steps: list[ExplainStep]

class ExplainTrace(_NativeHandle):
    """An EXPLAIN or EXPLAIN ANALYZE trace.

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
    """

    __slots__ = ("_xp", "_parent", "__weakref__")

    def __init__(self, xp_cdata, parent):
        """@private"""
        if xp_cdata == ffi.NULL:
            raise VelrError("runtime returned null explain trace")
        self._xp = xp_cdata
        self._init_child(parent)

    def _ensure_open(self):
        self._check_thread()
        if self._xp == ffi.NULL:
            raise HandleStateError("explain trace is closed", detail="ClosedExplainTrace")
        
    def close(self) -> None:
        """Close the explain trace handle.

        Repeated calls are harmless. Existing snapshots and rendered data remain usable.
        """
        self._check_thread()
        if self._xp != ffi.NULL:
            try:
                self._call_native(lib.velr_explain_trace_close, self._xp)
            finally:
                self._xp = ffi.NULL
                try:
                    self._parent._unregister_child(self)
                except Exception:
                    pass

    def __enter__(self) -> "ExplainTrace":
        """Return this handle for use in a ``with`` block.
        """
        self._check_thread()
        return self

    def __exit__(self, exc_type, exc, tb):
        """Close this handle on block exit without suppressing the exception.
        """
        self.close()

    def plan_count(self) -> int:
        """Return the nonnegative number of top-level plans.

        Raises ``VelrError`` if this trace or its producing connection/transaction is
        closed. Plan indexes passed to other methods range from zero to this count
        minus one.
        """
        self._ensure_open()
        return int(self._call_native(lib.velr_explain_trace_plan_count, self._xp))

    def plan_meta(self, plan_idx: int) -> ExplainPlanMeta:
        """Return an owned ``ExplainPlanMeta`` for zero-based ``plan_idx``.

        The metadata remains usable after the trace closes. Invalid indexes and
        closed-handle access raise ``VelrError``; incompatible index types or negative
        values may instead raise ``TypeError`` or ``OverflowError``.
        """
        self._ensure_open()
        out = ffi.new("velr_explain_plan_meta *")
        rc = self._call_native(lib.velr_explain_trace_plan_meta, self._xp, plan_idx, out)
        _check_noerr(rc, f"velr_explain_trace_plan_meta failed at plan_idx={plan_idx}")
        return ExplainPlanMeta(
            plan_id=_strview_to_string(out.plan_id, "plan_id"),
            cypher=_strview_to_string(out.cypher, "cypher"),
            step_count=int(out.step_count),
        )

    def step_count(self, plan_idx: int) -> int:
        """Return the number of steps in the zero-based plan ``plan_idx``.

        Use indexes in ``0..plan_count()-1``. Invalid indexes and closed traces raise
        ``VelrError``; incompatible or negative index arguments may raise Python
        ``TypeError`` or ``OverflowError``.
        """
        return self.plan_meta(plan_idx).step_count

    def step_meta(self, plan_idx: int, step_idx: int) -> ExplainStepMeta:
        """Return owned ``ExplainStepMeta`` for zero-based plan and step indexes.

        ``step_idx`` must be below ``step_count(plan_idx)``. The returned metadata
        survives trace closure. Invalid indexes/closed handles raise ``VelrError``;
        negative or incompatible indexes may raise Python range/type errors.
        """
        self._ensure_open()
        out = ffi.new("velr_explain_step_meta *")
        rc = self._call_native(lib.velr_explain_trace_step_meta, self._xp, plan_idx, step_idx, out)
        _check_noerr(
            rc,
            f"velr_explain_trace_step_meta failed at plan_idx={plan_idx}, step_idx={step_idx}",
        )
        return ExplainStepMeta(
            step_no=int(out.step_no),
            group_id=_strview_to_string(out.group_id, "group_id"),
            op_index=_strview_to_string(out.op_index, "op_index"),
            phase=_strview_to_string(out.phase, "phase"),
            title=_strview_to_string(out.title, "title"),
            source=_strview_to_string(out.source, "source"),
            note=_opt_strview_to_string(out.note, "step.note"),
            statement_count=int(out.statement_count),
        )

    def statement_count(self, plan_idx: int, step_idx: int) -> int:
        """Return the statement count for zero-based ``plan_idx`` and ``step_idx``.

        Both indexes must identify existing entries. Invalid indexes and closed handles
        raise ``VelrError``; negative or incompatible indexes may raise Python
        range/type errors.
        """
        return self.step_meta(plan_idx, step_idx).statement_count

    def statement_meta(self, plan_idx: int, step_idx: int, stmt_idx: int) -> ExplainStatementMeta:
        """Return owned ``ExplainStatementMeta`` for zero-based plan, step, and statement indexes.

        ``stmt_idx`` must be below ``statement_count(plan_idx, step_idx)``. The result
        survives trace closure. Invalid indexes and closed handles raise ``VelrError``;
        negative or incompatible indexes may raise Python range/type errors.
        """
        self._ensure_open()
        out = ffi.new("velr_explain_stmt_meta *")
        rc = self._call_native(
            lib.velr_explain_trace_statement_meta, self._xp, plan_idx, step_idx, stmt_idx, out,
        )
        _check_noerr(
            rc,
            "velr_explain_trace_statement_meta failed at "
            f"plan_idx={plan_idx}, step_idx={step_idx}, stmt_idx={stmt_idx}",
        )
        return ExplainStatementMeta(
            stmt_id=_strview_to_string(out.stmt_id, "stmt_id"),
            kind=_strview_to_string(out.kind, "kind"),
            sql=_strview_to_string(out.sql, "sql"),
            note=_opt_strview_to_string(out.note, "statement.note"),
            sqlite_plan_count=int(out.sqlite_plan_count),
        )

    def sqlite_plan_count(self, plan_idx: int, step_idx: int, stmt_idx: int) -> int:
        """Return the number of diagnostic plan-detail lines for the selected statement.

        All three indexes are zero-based and must identify existing entries. Raises
        ``VelrError`` for invalid indexes or a closed trace; negative or incompatible
        indexes may raise Python range/type errors.
        """
        return self.statement_meta(plan_idx, step_idx, stmt_idx).sqlite_plan_count

    def sqlite_plan_detail(
        self,
        plan_idx: int,
        step_idx: int,
        stmt_idx: int,
        detail_idx: int,
    ) -> str:
        """Return one owned diagnostic string for zero-based plan, step, statement, and detail indexes.

        ``detail_idx`` must be below ``sqlite_plan_count(plan_idx, step_idx, stmt_idx)``.
        The string survives trace closure. Invalid indexes and closed handles raise
        ``VelrError``; negative or incompatible indexes may raise Python range/type errors.
        Diagnostic wording is informational and may vary between runtime versions.
        """
        self._ensure_open()
        out = ffi.new("velr_strview *")
        rc = self._call_native(
            lib.velr_explain_trace_sqlite_plan_detail,
            self._xp,
            plan_idx,
            step_idx,
            stmt_idx,
            detail_idx,
            out,
        )
        _check_noerr(
            rc,
            "velr_explain_trace_sqlite_plan_detail failed at "
            f"plan_idx={plan_idx}, step_idx={step_idx}, stmt_idx={stmt_idx}, detail_idx={detail_idx}",
        )
        return _strview_to_string(out[0], "sqlite_plan_detail")

    def sqlite_plan_details(self, plan_idx: int, step_idx: int, stmt_idx: int) -> list[str]:
        """Return all owned diagnostic strings for the selected statement in detail order.

        Indexes are zero-based. The returned list survives trace closure. Invalid
        indexes and closed handles raise ``VelrError``; negative or incompatible index
        arguments may raise Python range/type errors.
        """
        n = self.sqlite_plan_count(plan_idx, step_idx, stmt_idx)
        return [self.sqlite_plan_detail(plan_idx, step_idx, stmt_idx, i) for i in range(n)]

    def snapshot(self) -> list[ExplainPlan]:
        """Return all plans as an owned list of ``ExplainPlan`` snapshots.

        Each snapshot contains its metadata, ordered steps, statements, and diagnostic
        plan details. The list remains usable after the trace closes. Closed-handle
        access or a failure to retrieve a trace entry raises ``VelrError``.
        """
        plans: list[ExplainPlan] = []
        for plan_idx in range(self.plan_count()):
            pmeta = self.plan_meta(plan_idx)
            steps: list[ExplainStep] = []
            for step_idx in range(pmeta.step_count):
                smeta = self.step_meta(plan_idx, step_idx)
                statements: list[ExplainStatement] = []
                for stmt_idx in range(smeta.statement_count):
                    stmeta = self.statement_meta(plan_idx, step_idx, stmt_idx)
                    statements.append(
                        ExplainStatement(
                            meta=stmeta,
                            sqlite_plan=self.sqlite_plan_details(plan_idx, step_idx, stmt_idx),
                        )
                    )
                steps.append(ExplainStep(meta=smeta, statements=statements))
            plans.append(ExplainPlan(meta=pmeta, steps=steps))
        return plans

    def compact_len(self) -> int:
        """Return the compact rendering length in UTF-8 bytes, excluding a terminator.

        This does not render a string for the caller. Raises ``VelrError`` if the trace
        is closed or the rendering length cannot be obtained.
        """
        self._ensure_open()
        out_len = ffi.new("size_t *")
        out_err = ffi.new("char **")
        rc = self._call_native(lib.velr_explain_trace_compact_len, self._xp, out_len, out_err)
        _check(rc, out_err)
        return int(out_len[0])

    def to_compact_bytes(self) -> bytes:
        """Render the complete trace as owned UTF-8 ``bytes``.

        The bytes survive trace closure. Raises ``VelrError`` for a closed trace or
        rendering failure. Use ``write_compact`` for a binary file-like destination.
        """
        self._ensure_open()
        out_ptr = ffi.new("uint8_t **")
        out_len = ffi.new("size_t *")
        out_err = ffi.new("char **")
        rc = self._call_native(
            lib.velr_explain_trace_compact_malloc, self._xp, out_ptr, out_len, out_err,
        )
        _check(rc, out_err)

        ptr = out_ptr[0]
        ln = int(out_len[0])

        if ptr == ffi.NULL:
            if ln == 0:
                return b""
            raise VelrError("velr_explain_trace_compact_malloc returned null with non-zero length")

        try:
            return bytes(ffi.buffer(ptr, ln))
        finally:
            lib.velr_free(ptr, ln)

    def to_compact_string(self) -> str:
        """Render the complete trace as an owned Python ``str``.

        The string survives trace closure. Raises ``VelrError`` for a closed trace or
        rendering failure, including invalid UTF-8.
        """
        try:
            return self.to_compact_bytes().decode("utf-8", "strict")
        except UnicodeDecodeError as e:
            raise VelrError(f"compact explain is not valid UTF-8: {e}") from e

    def write_compact(self, out) -> None:
        """Write the compact rendering to a file-like object.

        The destination must accept bytes and remains open. I/O errors propagate;
        this method returns ``None``.
        """
        out.write(self.to_compact_bytes())

class Velr(_NativeHandle):
    """A Velr database connection.

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
    """

    __slots__ = (
        "_db", "_children", "_vector_embedders", "_state_lock", "_busy", "_native_busy", "_callback_error", "__weakref__",
    )

    def __init__(self, db_cdata):
        """@private"""
        self._owner = _current_handle_thread()
        self._db = db_cdata
        self._children = _ChildHandles()
        self._vector_embedders = {}
        self._state_lock = RLock()
        self._busy = False
        self._native_busy = False
        self._callback_error = None
        self._check_thread()

    def _connection(self) -> Velr:
        return self

    def __del__(self):
        if sys.is_finalizing():
            return
        try:
            self.close()
        except Exception:
            owner = getattr(self, "_owner", None)
            if owner is not None:
                owner.defer_close(partial(self._close_from_owner, owner))

    def _start_operation(self, cleanup_owner=None, *, only_with_children: bool = False) -> bool:
        previous = self._owner
        # Thread-local state can already be cleared during owner-thread cleanup.
        owner = (
            previous if previous.exiting and get_ident() == previous.ident
            else _current_handle_thread()
        )
        with self._state_lock:
            if cleanup_owner is not None and (
                self._owner is not cleanup_owner or owner is not cleanup_owner
                or (only_with_children and not self._children)
            ):
                return False
            if self._busy or self._native_busy:
                raise HandleStateError("Velr connection is already in use", detail="ConnectionInUse")
            if self._children and self._owner is not owner:
                raise HandleStateError("Velr connection has live handles on another thread", detail="WrongThread")
            self._busy = True
            self._owner = owner
        return True

    def _finish_operation(self) -> None:
        with self._state_lock:
            self._busy = False

    def _close_from_owner(self, owner: _HandleThread, *, only_with_children: bool = False) -> None:
        if self._start_operation(owner, only_with_children=only_with_children):
            try:
                self._close()
            finally:
                self._finish_operation()

    def _ensure_open(self) -> None:
        self._check_thread()
        if self._db == ffi.NULL:
            raise HandleStateError("connection already closed", detail="ClosedConnection")

    def _register_child(self, child) -> None:
        with self._state_lock:
            if not self._children:
                self._owner.connections.add(self)
            self._children.add(child)

    def _unregister_child(self, child) -> None:
        with self._state_lock:
            self._children.discard(child)
            if not self._children:
                self._owner.connections.discard(self)

    def _close_children_first(self) -> None:
        self._children.close()

    @classmethod
    def open(
        cls,
        path: Optional[str] = None,
        *,
        extensions: Sequence[Extension] = (),
    ) -> "Velr":
        """Open a writable in-memory or file-backed Velr database.

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
        """
        if not isinstance(extensions, Sequence) or isinstance(extensions, (str, bytes, bytearray)):
            raise TypeError("extensions must be a sequence of Extension descriptors")
        out_db = ffi.new("velr_db **")
        out_err = ffi.new("char **")
        path_arg = path.encode("utf-8") if path is not None else ffi.NULL
        if extensions:
            if path is not None and "\x00" in path:
                raise VelrError("path contains NUL")
            descriptors = tuple(extensions)
            if not all(isinstance(extension, Extension) for extension in descriptors):
                raise TypeError("extensions must contain only Extension descriptors")
            paths = [ffi.new("char[]", str(extension.library_path).encode("utf-8"))
                     for extension in descriptors]
            configs = ffi.new("velr_extension_config[]", [
                (library, extension.memory_limit_bytes)
                for library, extension in zip(paths, descriptors, strict=True)
            ])
            open_with_extensions = _runtime_symbol("velr_open_with_extensions")
            rc = open_with_extensions(path_arg, configs, len(descriptors), out_db, out_err)
        else:
            rc = lib.velr_open(path_arg, out_db, out_err)
        _check(rc, out_err)
        if out_db[0] == ffi.NULL:
            raise VelrError("velr_open returned null db")
        return cls(out_db[0])

    @classmethod
    def create(
        cls,
        path: Optional[str] = None,
        *,
        inline_edge_properties: Sequence[str] = (),
    ) -> "Velr":
        """Create a new database and return its open connection.

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
        """
        if isinstance(inline_edge_properties, (str, bytes, bytearray)):
            raise TypeError("inline_edge_properties must be a sequence of property names")
        if path is not None and "\x00" in path:
            raise VelrError("path contains NUL")
        names = []
        for name in inline_edge_properties:
            if not isinstance(name, str):
                raise TypeError("inline edge property names must be strings")
            if "\x00" in name:
                raise VelrError("inline edge property names cannot contain NUL")
            names.append(ffi.new("char[]", name.encode("utf-8")))
        names_ptr = ffi.new("char *[]", names) if names else ffi.NULL
        out_db = ffi.new("velr_db **")
        out_err = ffi.new("char **")
        rc = _runtime_symbol("velr_create")(
            path.encode("utf-8") if path is not None else ffi.NULL,
            names_ptr,
            len(names),
            out_db,
            out_err,
        )
        _check(rc, out_err)
        if out_db[0] == ffi.NULL:
            raise VelrError("velr_create returned null db")
        return cls(out_db[0])

    @_connection_operation
    def inline_edge_properties(self) -> list[str]:
        """Return the database's inline edge property names in configured order.

        The selection remains available after reopening the database. Returns
        an empty list when no property is selected. The caller owns the returned
        list, which remains usable after the connection closes. Raises ``VelrError``
        if the connection is closed, busy, or the runtime cannot report the selection.
        Requires the inspection API provided by Velr 0.2.62 or newer.
        """
        self._ensure_open()
        out_json = ffi.new("char **")
        out_err = ffi.new("char **")
        rc = self._call_native(
            _runtime_symbol("velr_inline_edge_properties_json"),
            self._db, out_json, out_err
        )
        _check(rc, out_err)
        if out_json[0] == ffi.NULL:
            raise VelrError("velr_inline_edge_properties_json returned null JSON")
        try:
            names = json.loads(ffi.string(out_json[0]))
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise VelrError("inline edge properties did not decode to a string list")
            return names
        except ValueError as exc:
            raise VelrError("inline edge properties returned invalid JSON") from exc
        finally:
            lib.velr_string_free(out_json[0])

    @classmethod
    def open_readonly(cls, path: str) -> "Velr":
        """Open an existing file-backed database without modifying it.

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
        """
        out_db = ffi.new("velr_db **")
        out_err = ffi.new("char **")
        rc = lib.velr_open_existing_readonly(
            path.encode("utf-8"),
            out_db,
            out_err,
        )
        _check(rc, out_err)
        if out_db[0] == ffi.NULL:
            raise VelrError("velr_open_existing_readonly returned null db")
        return cls(out_db[0])

    @_connection_operation
    def schema_version(self) -> int:
        """Return this connection's database schema version as an integer.

        Compare with ``current_schema_version()`` or use ``needs_migration()`` before
        explicit maintenance. Raises ``VelrError`` for a closed connection or a
        runtime that cannot report the version.
        """
        self._ensure_open()
        fn = _runtime_symbol("velr_schema_version")
        out_version = ffi.new("int32_t *")
        out_err = ffi.new("char **")
        rc = self._call_native(fn, self._db, out_version, out_err)
        _check(rc, out_err)
        return int(out_version[0])

    @_connection_operation
    def current_schema_version(self) -> int:
        """Return the current schema version supported by this runtime.

        Raises ``VelrError`` if the connection is closed or its runtime cannot report
        the current version.
        """
        self._ensure_open()
        fn = _runtime_symbol("velr_current_schema_version")
        out_version = ffi.new("int32_t *")
        out_err = ffi.new("char **")
        rc = self._call_native(fn, self._db, out_version, out_err)
        _check(rc, out_err)
        return int(out_version[0])

    @_connection_operation
    def needs_migration(self) -> bool:
        """Return ``True`` when this database is on an older supported schema version.

        With the current runtime, supported older formats require explicit
        migration before graph queries or transactions.
        Raises ``VelrError`` for a closed connection or a runtime that cannot
        report migration state.
        """
        self._ensure_open()
        fn = _runtime_symbol("velr_needs_migration")
        out_needs = ffi.new("int *")
        out_err = ffi.new("char **")
        rc = self._call_native(fn, self._db, out_needs, out_err)
        _check(rc, out_err)
        return bool(int(out_needs[0]))

    @_connection_operation
    def migrate(self) -> MigrationReport:
        """Explicitly migrate this database to the current schema version.

        Opening a supported older database does not migrate it. Call this method,
        or run ``MIGRATE DATABASE``, when maintenance code intentionally wants to
        apply the pending schema migration.

        Migration requires a writable connection outside any active transaction.
        Opening never applies migrations. Reopen existing read-only connections
        after the file is migrated through another connection.

        Returns :class:`MigrationReport`, including ``already_current`` when no change
        is needed. Raises ``VelrError`` for read-only connections, active transactions,
        unsupported migration, a closed connection, or migration failure.
        """
        self._ensure_open()
        fn = _runtime_symbol("velr_migrate")
        report = ffi.new("velr_migration_report *")
        out_err = ffi.new("char **")
        rc = self._call_native(fn, self._db, report, out_err)
        try:
            _check(rc, out_err)
            steps: List[str] = []
            if report.steps != ffi.NULL and int(report.step_count) > 0:
                detail = ffi.string(report.steps).decode("utf-8", errors="replace")
                steps = [step for step in detail.split(",") if step]
            status = (
                "migrated"
                if int(report.status) == int(ffi.cast("velr_migration_status", 1))
                else "already_current"
            )
            return MigrationReport(
                from_version=int(report.from_version),
                to_version=int(report.to_version),
                status=status,
                steps=steps,
            )
        finally:
            if report.steps != ffi.NULL:
                try:
                    lib.velr_migration_report_clear(report)
                except AttributeError:
                    lib.velr_string_free(report.steps)
                    report.steps = ffi.NULL

    @_connection_operation
    def vacuum(self) -> None:
        """Compact a file-backed database.

        This is an explicit maintenance operation for file-backed databases
        after large deletes or prune operations. It must run on a writable
        current-schema connection outside any open transaction.
        Returns ``None``. A closed connection, unmet requirements, or compaction
        failure raises ``VelrError``.
        """
        self._ensure_open()
        out_err = ffi.new("char **")
        rc = self._call_native(_runtime_symbol("velr_vacuum"), self._db, out_err)
        _check(rc, out_err)

    @_connection_operation
    def close(self) -> None:
        """Close the connection and its open child handles.

        Uncommitted transactions roll back. Streams, tables, row iterators, and
        explain traces associated with the connection close. Copied cells, records,
        Arrow/dataframe exports, and explain snapshots remain usable. Close active
        children on their creating thread; cross-thread use or overlapping operations
        raise ``VelrError``.
        Repeated calls are harmless. Prefer ``with Velr.open(...) as db`` for
        deterministic cleanup.
        """
        self._close()

    def _close(self) -> None:
        self._check_thread()
        self._close_children_first()
        if self._db != ffi.NULL:
            self._call_native(lib.velr_close, self._db)
            self._db = ffi.NULL
        self._vector_embedders.clear()

    @_connection_operation
    def __enter__(self) -> "Velr":
        """Return this handle for use in a ``with`` block.
        """
        self._check_thread()
        return self

    def __exit__(self, exc_type, exc, tb):
        """Close this handle on block exit without suppressing the exception.
        """
        self.close()

    @_connection_operation
    def register_vector_embedder(
        self,
        name: str,
        embedder: Callable[[list[VectorEmbeddingInput]], Sequence[Sequence[float]]],
    ) -> None:
        """Register a named embedding callback for vector indexes.

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
        """
        self._ensure_open()
        if not isinstance(name, str):
            raise TypeError("vector embedder name must be a string")
        if not name.strip():
            raise ValueError("vector embedder name cannot be empty")
        if not callable(embedder):
            raise TypeError("vector embedder must be callable")

        callback = _make_vector_callback(embedder, self)
        name_view, name_buf = _strview(name.encode("utf-8"))
        out_err = ffi.new("char **")
        rc = self._call_native(
            _runtime_symbol("velr_register_vector_embedder"),
            self._db,
            name_view,
            callback,
            ffi.NULL,
            ffi.NULL,
            out_err,
        )
        _ = name_buf
        self._vector_embedders[name] = callback
        _check(rc, out_err)

    @_connection_operation
    def run(
        self,
        cypher: str,
        *,
        max_result_rows: int | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        """Execute a query or script and discard all results.

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
        """
        self._ensure_open()
        _call_query(
            self._call_native,
            "velr_run", self._db, cypher, max_result_rows, params, timeout, cancellation
        )

    def execute(
        self,
        cypher: str,
        *,
        max_result_rows: int | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        """Alias for ``run`` with identical parameters, return value, and errors.

        Executes the script, discards all result tables, and returns ``None``. See
        ``Velr.run`` for ``params``, per-table row caps, seconds-based ``timeout``,
        ``cancellation``, argument validation, and query-error behavior.
        """
        return self.run(
            cypher, max_result_rows=max_result_rows, params=params, timeout=timeout, cancellation=cancellation
        )

    @_connection_operation
    def exec(
        self,
        cypher: str,
        *,
        max_result_rows: int | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> "Stream":
        """Execute a query and return a stream of result tables.

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
        """
        self._ensure_open()
        out_st = ffi.new("velr_stream **")
        control = _call_query(
            self._call_native,
            "velr_exec_start", self._db, cypher, max_result_rows, params, timeout, cancellation, out_st
        )
        if out_st[0] == ffi.NULL:
            raise VelrError("velr_exec_start returned null stream")

        st = Stream(out_st[0], parent=self, control=control)
        self._register_child(st)
        return st

    @_connection_operation
    def _exec_one_native(
        self,
        cypher: str,
        *,
        max_result_rows: int | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> "Table":
        """Execute a query using the native one-table path."""
        self._ensure_open()
        out_tbl = ffi.new("velr_table **")
        control = _call_query(
            self._call_native,
            "velr_exec_one", self._db, cypher, max_result_rows, params, timeout, cancellation, out_tbl
        )
        if out_tbl[0] == ffi.NULL:
            raise VelrError("velr_exec_one returned null table")

        tbl = Table(out_tbl[0], parent=self, control=control)
        self._register_child(tbl)
        return tbl

    def exec_one(
        self,
        cypher: str,
        *,
        max_result_rows: int | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> "Table":
        """Execute a query expected to return exactly one table.

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
        """
        return self._exec_one_native(
            cypher, max_result_rows=max_result_rows, params=params, timeout=timeout, cancellation=cancellation
        )

    @_connection_operation
    def explain(self, cypher: str) -> ExplainTrace:
        """Explain query text without executing it.

        Accepts NUL-free Unicode ``str`` query text only,
        without parameters, row caps, timeouts, or cancellation arguments. Keep the
        trace and its parent connection open while reading; close it explicitly or
        use a context manager. Closing the parent closes the trace. Copied snapshots,
        metadata, and compact strings/bytes remain usable afterward.

        Query/runtime failures and closed, busy, or wrong-thread handles raise
        ``VelrError``. Unsupported text objects or encoding failures can raise
        standard Python exceptions such as ``AttributeError`` or ``UnicodeEncodeError``.
        """
        self._ensure_open()

        out_xp = ffi.new("velr_explain_trace **")
        out_err = ffi.new("char **")
        rc = self._call_native(lib.velr_explain, self._db, cypher.encode("utf-8"), out_xp, out_err)
        _check(rc, out_err)
        if out_xp[0] == ffi.NULL:
            raise VelrError("velr_explain returned null explain trace")

        xp = ExplainTrace(out_xp[0], parent=self)
        self._register_child(xp)
        return xp

    @_connection_operation
    def explain_analyze(self, cypher: str) -> ExplainTrace:
        """Execute and profile query text, including its writes.

        Use ``VelrTx.explain_analyze()`` and roll back its transaction when analyzed
        writes should be discarded. Accepts NUL-free Unicode ``str`` query text only,
        without parameters, row caps, timeouts, or cancellation arguments. Keep the
        trace and its parent connection open while reading; close it explicitly or
        use a context manager. Closing the parent closes the trace. Copied snapshots,
        metadata, and compact strings/bytes remain usable afterward.

        Query/runtime failures and closed, busy, or wrong-thread handles raise
        ``VelrError``. Unsupported text objects or encoding failures can raise
        standard Python exceptions such as ``AttributeError`` or ``UnicodeEncodeError``.
        """
        self._ensure_open()

        out_xp = ffi.new("velr_explain_trace **")
        out_err = ffi.new("char **")
        rc = self._call_native(
            lib.velr_explain_analyze, self._db, cypher.encode("utf-8"), out_xp, out_err,
        )
        _check(rc, out_err)
        if out_xp[0] == ffi.NULL:
            raise VelrError("velr_explain_analyze returned null explain trace")

        xp = ExplainTrace(out_xp[0], parent=self)
        self._register_child(xp)
        return xp

    def to_pyarrow(self, cypher: str):
        """Execute a query and return the last result table as ``pyarrow.Table``.

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
        """
        import pyarrow as pa  # type: ignore[import-not-found]

        with self.exec(cypher) as st:
            last: Table | None = None
            try:
                while True:
                    t = st.next_table()
                    if t is None:
                        break

                    # Re-parent the candidate table away from the stream so an EOF-driven
                    # stream close does not implicitly close it before we export it.
                    if getattr(t, "_parent", None) is st:
                        try:
                            st._unregister_child(t)
                        except Exception:
                            pass
                        t._parent = self
                        self._register_child(t)

                    if last is not None:
                        last.close()

                    last = t

                if last is None:
                    return pa.table({})

                try:
                    return last.to_pyarrow()
                finally:
                    last.close()

            except Exception:
                if last is not None:
                    try:
                        last.close()
                    except Exception:
                        pass
                raise

    def to_rows(self, cypher: str, columns=None):
        """Execute a query and return the last table as ``list[list]``.

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
        """
        with self.exec(cypher) as st:
            last: Table | None = None
            try:
                while True:
                    t = st.next_table()
                    if t is None:
                        break

                    # Re-parent the candidate table away from the stream so an EOF-driven
                    # stream close does not implicitly close it before we export it.
                    if getattr(t, "_parent", None) is st:
                        try:
                            st._unregister_child(t)
                        except Exception:
                            pass
                        t._parent = self
                        self._register_child(t)

                    if last is not None:
                        last.close()

                    last = t

                if last is None:
                    return []

                try:
                    return last.to_rows(columns=columns)
                finally:
                    last.close()

            except Exception:
                if last is not None:
                    try:
                        last.close()
                    except Exception:
                        pass
                raise

    def to_records(self, cypher: str, columns=None):
        """Execute a query and return the last table as ``list[dict]``.

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
        """
        with self.exec(cypher) as st:
            last: Table | None = None
            try:
                while True:
                    t = st.next_table()
                    if t is None:
                        break

                    # Re-parent the candidate table away from the stream so an EOF-driven
                    # stream close does not implicitly close it before we export it.
                    if getattr(t, "_parent", None) is st:
                        try:
                            st._unregister_child(t)
                        except Exception:
                            pass
                        t._parent = self
                        self._register_child(t)

                    if last is not None:
                        last.close()

                    last = t

                if last is None:
                    return []

                try:
                    return last.to_records(columns=columns)
                finally:
                    last.close()

            except Exception:
                if last is not None:
                    try:
                        last.close()
                    except Exception:
                        pass
                raise

    def to_pandas(self, cypher: str, **kwargs):
        """Execute a query and return its last table as a pandas DataFrame.

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
        """
        pa_tbl = self.to_pyarrow(cypher)
        df = pa_tbl.to_pandas()

        backend = kwargs.pop("dtype_backend", "numpy_nullable")
        if backend is not None:
            try:
                df = df.convert_dtypes(dtype_backend=backend)
            except TypeError:
                df = df.convert_dtypes()

        if kwargs:
            raise VelrError(f"Unsupported to_pandas() kwargs: {list(kwargs.keys())}")

        return df

    def to_polars(self, cypher: str):
        """Execute a query and return its last table as a Polars DataFrame.

        Requires Polars and PyArrow. No result table produces an empty DataFrame.
        The result remains usable after the connection closes. Query failures raise
        ``VelrError`` and missing optional packages raise ``ImportError``.

        Accepts query text only, without query parameters or execution controls.
        Use ``exec_one(...).to_polars()`` for parameterized or controlled queries.
        Conversion follows ``Table.to_pyarrow``; structured values remain JSON strings.
        """
        import polars as pl  # type: ignore[import-not-found]

        return pl.from_arrow(self.to_pyarrow(cypher))

    @_connection_operation
    def bind_arrow(self, logical: str, columns) -> None:
        """Bind Arrow columns as a named input table for ``BIND('name')``.

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
        """
        self._ensure_open()
        self._bind_arrow(logical, columns)

    def _bind_arrow(self, logical: str, columns) -> None:
        pairs = _normalize_arrow_columns(columns)
        (
            cols_c,
            name_views,
            _name_backing,
            _keep_schemas_nested,
            _keep_arrays_nested,
            _schema_pp_per_col,
            _array_pp_per_col,
        ) = _export_arrow_columns_for_bind_chunked(pairs)

        out_err = ffi.new("char **")
        logical_c = ffi.new("char[]", logical.encode("utf-8"))
        rc = self._call_native(
            lib.velr_bind_arrow_chunks,
            self._db,
            logical_c,
            cols_c,
            name_views,
            len(pairs),
            out_err,
        )
        _check(rc, out_err)

    @_connection_operation
    def bind_arrow_ipc(self, logical: str, ipc_bytes) -> None:
        """Bind Arrow IPC file/Feather v2 bytes as a named Cypher input table.

        ``ipc_bytes`` accepts a nonempty contiguous buffer such as ``bytes``,
        ``bytearray``, or ``memoryview``. It need only remain valid during this call.
        Read the data with ``UNWIND BIND('name') AS row ...``; binding alone does not
        write graph data. Use a NUL-free ``str`` logical name. IPC streams are not
        accepted. PyArrow is not required. Returns ``None``. Empty/noncontiguous buffers raise
        ``ValueError``; incompatible buffers raise ``TypeError``; invalid IPC or a
        closed connection raises ``VelrError``.
        """
        self._ensure_open()
        cbuf, nbytes, view = _ipc_buffer(ipc_bytes)
        out_err = ffi.new("char **")
        logical_c = ffi.new("char[]", logical.encode("utf-8"))
        rc = self._call_native(
            _runtime_symbol("velr_bind_arrow_ipc"),
            self._db,
            logical_c,
            cbuf,
            nbytes,
            out_err,
        )
        _ = view
        _check(rc, out_err)

    def bind_arrow_chunks(self, logical: str, columns) -> None:
        """Alias for ``bind_arrow`` accepting the same single- or multi-chunk columns.

        The logical name, supported Arrow inputs, required PyArrow installation,
        input immutability, return value, and errors are identical to ``Velr.bind_arrow``.
        """
        self.bind_arrow(logical, columns)

    def bind_pandas(
        self,
        logical: str,
        df: "pd.DataFrame",
        *,
        index: bool = False,
        jsonify_objects: bool = True,
        schema: "Optional['pa.Schema']" = None,
    ) -> None:
        """Bind a pandas DataFrame as a named Cypher input table.

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
        """
        tbl = _pandas_to_arrow_table(
            df,
            index=index,
            jsonify_objects=jsonify_objects,
            schema=schema,
        )
        self.bind_arrow(logical, tbl)

    def bind_polars(
        self,
        logical: str,
        df: "pl.DataFrame",
        *,
        rechunk: bool = False,
    ) -> None:
        """Bind a Polars DataFrame as a named Cypher input table.

        Requires Polars and PyArrow. ``rechunk=True`` combines dataframe chunks
        before conversion; the default preserves existing chunks. Returns ``None``.
        Conversion errors propagate; database binding errors raise ``VelrError``.
        See :meth:`bind_arrow` for ``BIND(...)`` usage.
        """
        tbl = _polars_to_arrow_table(df, rechunk=rechunk)
        self.bind_arrow(logical, tbl)

    def bind_numpy(
        self,
        logical: str,
        data,
        *,
        names: list[str] | None = None,
        types: dict | None = None,
    ) -> None:
        """Bind NumPy columns as a named Cypher input table.

        ``data`` is a mapping from column names to arrays, or a two-dimensional
        ``numpy.ndarray`` with one name per column in ``names``. ``types`` optionally
        maps column names to PyArrow types. Requires NumPy and PyArrow. Returns
        ``None``. Unsupported shapes or missing/mismatched names raise ``VelrError``;
        array conversion errors and missing-package ``ImportError`` propagate.
        The ``types`` mapping casts named columns and leaves other columns inferred.
        Input lifetime/immutability follows ``bind_arrow``. See that method for query usage.
        """
        tbl = _numpy_to_arrow_table(data, names=names, types=types)
        self.bind_arrow(logical, tbl)

    @_connection_operation
    def bind_records(
        self,
        logical: str,
        rows: list[dict],
        *,
        types: dict | None = None,
    ) -> None:
        """Bind a list of mapping records as a named Cypher input table.

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
        """
        self._ensure_open()
        if types is None and self._call_native(_bind_records_scalar, self._db, logical, rows, tx=False):
            return
        tbl = _records_to_arrow_table(rows, types=types)
        self._bind_arrow(logical, tbl)

    @_connection_operation
    def begin_tx(self) -> "VelrTx":
        """Begin this connection's single active explicit transaction.

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
        """
        self._ensure_open()

        out_tx = ffi.new("velr_tx **")
        out_err = ffi.new("char **")
        rc = self._call_native(lib.velr_tx_begin, self._db, out_tx, out_err)
        _check(rc, out_err)
        if out_tx[0] == ffi.NULL:
            raise VelrError("velr_tx_begin returned null tx")

        tx = VelrTx(out_tx[0], parent=self)
        self._register_child(tx)
        return tx

class Stream(_NativeHandle):
    """Stream of result tables produced by ``Velr.exec()``.

    Use ``next_table()`` to pull tables one by one, or ``iter_tables()`` to
    iterate over the remaining tables.

    Tables yielded by this stream are valid while it remains open. Closing or
    exhausting the stream closes its still-open tables and row iterators.

    Use ``close()`` when done, or use the stream as a context manager.

    Do not construct streams directly; obtain them from ``exec()``. Use a context
    manager to ensure cleanup, especially when stopping before exhaustion.
    """

    __slots__ = ("_st", "_children", "_parent", "_control", "__weakref__")

    def __init__(self, st_cdata, parent: "Velr", *, control=None):
        """@private"""
        self._st = st_cdata
        self._children = _ChildHandles()
        self._init_child(parent)
        self._control = control

    def _register_child(self, child) -> None:
        self._children.add(child)

    def _unregister_child(self, child) -> None:
        try:
            self._children.discard(child)
        except Exception:
            pass

    def _close_children_first(self) -> None:
        self._children.close()

    def next_table(self) -> Optional["Table"]:
        """Return the next :class:`Table`, or ``None`` after stream exhaustion/closure.

        The table is owned by this stream and must be consumed or exported while the
        stream is open. Reaching end-of-stream closes it and any remaining child
        tables. Query errors, wrong-thread access, or a closed parent raise
        ``VelrError``. A closed stream returns ``None`` only while its parent remains open.
        """
        self._check_thread()
        self._parent._ensure_open()
        if self._st == ffi.NULL:
            return None

        out_tbl = ffi.new("velr_table **")
        out_has = ffi.new("int *")
        out_err = ffi.new("char **")
        rc = self._call_native(lib.velr_stream_next_table, self._st, out_tbl, out_has, out_err)
        _check(rc, out_err)

        if out_has[0] == 0:
            self.close()
            return None

        if out_tbl[0] == ffi.NULL:
            raise VelrError("velr_stream_next_table returned has=1 but null table")

        tbl = Table(out_tbl[0], parent=self, control=self._control)
        self._register_child(tbl)
        return tbl

    def iter_tables(self):
        """Iterate over remaining result tables.

        Each yielded table is closed automatically after the iteration step
        completes.

        Consume each table inside the loop body: advancing or closing the generator
        closes the previous table. Use a stream context manager when stopping early.
        Execution errors propagate as ``VelrError``.
        """
        while True:
            t = self.next_table()
            if t is None:
                return
            try:
                yield t
            finally:
                t.close()

    def close(self) -> None:
        """Close this stream and any still-open tables and row iterators it produced.

        Repeated calls are harmless. The parent connection/transaction remains open.
        After stream closure ``next_table()`` returns ``None`` while its parent
        remains open; a closed parent instead raises ``VelrError``. Call on the
        creating thread; cross-thread access raises ``VelrError``.
        """
        self._check_thread()
        self._close_children_first()
        if self._st != ffi.NULL:
            try:
                self._call_native(lib.velr_exec_close, self._st)
            finally:
                self._st = ffi.NULL
                self._control = None
                try:
                    self._parent._unregister_child(self)
                except Exception:
                    pass

    def __enter__(self) -> "Stream":
        """Return this handle for use in a ``with`` block.
        """
        self._check_thread()
        return self

    def __exit__(self, exc_type, exc, tb):
        """Close this handle on block exit without suppressing the exception.
        """
        self.close()


class StreamTx(_NativeHandle):
    """Stream of result tables produced by ``VelrTx.exec()``.

    This is the transaction-scoped counterpart to ``Stream``.

    Tables yielded by this stream are valid while it remains open. Closing or
    exhausting the stream closes its still-open tables and row iterators.

    The stream keeps its parent transaction alive for as long as the stream
    itself is open.

    Do not construct streams directly; obtain them from ``exec()``. Use a context
    manager to ensure cleanup, especially when stopping before exhaustion.
    """

    __slots__ = ("_st", "_children", "_parent", "_control", "__weakref__")

    def __init__(self, st_cdata, parent: "VelrTx", *, control=None):
        """@private"""
        self._st = st_cdata
        self._children = _ChildHandles()
        self._init_child(parent)
        self._control = control

    def _register_child(self, child) -> None:
        self._children.add(child)

    def _unregister_child(self, child) -> None:
        try:
            self._children.discard(child)
        except Exception:
            pass

    def _close_children_first(self) -> None:
        self._children.close()

    def next_table(self) -> Optional["Table"]:
        """Return the next :class:`Table`, or ``None`` after stream exhaustion/closure.

        The table is owned by this stream and must be consumed or exported while the
        stream is open. Reaching end-of-stream closes it and any remaining child
        tables. Query errors, wrong-thread access, or a closed parent raise
        ``VelrError``. A closed stream returns ``None`` only while its parent remains open.
        """
        self._check_thread()
        self._parent._ensure_open()
        if self._st == ffi.NULL:
            return None

        out_tbl = ffi.new("velr_table **")
        out_has = ffi.new("int *")
        out_err = ffi.new("char **")
        rc = self._call_native(lib.velr_stream_tx_next_table, self._st, out_tbl, out_has, out_err)
        _check(rc, out_err)

        if out_has[0] == 0:
            self.close()
            return None

        if out_tbl[0] == ffi.NULL:
            raise VelrError("velr_stream_tx_next_table returned has=1 but null table")

        tbl = Table(out_tbl[0], parent=self, control=self._control)
        self._register_child(tbl)
        return tbl

    def iter_tables(self):
        """Iterate over remaining transaction-scoped result tables.

        Each yielded table is closed automatically after the iteration step
        completes.

        Consume each table inside the loop body: advancing or closing the generator
        closes the previous table. Use a stream context manager when stopping early.
        Execution errors propagate as ``VelrError``.
        """
        while True:
            t = self.next_table()
            if t is None:
                return
            try:
                yield t
            finally:
                t.close()

    def close(self) -> None:
        """Close this stream and any still-open tables and row iterators it produced.

        Repeated calls are harmless. The parent connection/transaction remains open.
        After stream closure ``next_table()`` returns ``None`` while its parent
        remains open; a closed parent instead raises ``VelrError``. Call on the
        creating thread; cross-thread access raises ``VelrError``.
        """
        self._check_thread()
        self._close_children_first()
        if self._st != ffi.NULL:
            try:
                self._call_native(lib.velr_exec_tx_close, self._st)
            finally:
                self._st = ffi.NULL
                self._control = None
                try:
                    self._parent._unregister_child(self)
                except Exception:
                    pass

    def __enter__(self) -> "StreamTx":
        """Return this handle for use in a ``with`` block.
        """
        self._check_thread()
        return self

    def __exit__(self, exc_type, exc, tb):
        """Close this handle on block exit without suppressing the exception.
        """
        self.close()


class Table(_NativeHandle):
    """One result table.

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
    """

    __slots__ = ("_tbl", "_children", "_parent", "_control", "__weakref__")

    def __init__(self, tbl_cdata, parent, *, control=None):
        """@private"""
        self._tbl = tbl_cdata
        self._children = _ChildHandles()
        self._init_child(parent)
        self._control = control

    def _ensure_open(self) -> None:
        self._check_thread()
        if self._tbl == ffi.NULL:
            raise HandleStateError("table already closed", detail="ClosedTable")

    def _register_child(self, child) -> None:
        self._children.add(child)

    def _unregister_child(self, child) -> None:
        try:
            self._children.discard(child)
        except Exception:
            pass

    def _close_children_first(self) -> None:
        self._children.close()

    def _to_ipc_foreign(self) -> _ForeignIPC:
        """Export this table as a runtime-owned Arrow IPC buffer."""
        self._ensure_open()

        out_ptr = ffi.new("uint8_t **")
        out_len = ffi.new("size_t *")
        out_err = ffi.new("char **")
        rc = self._call_native(lib.velr_table_ipc_file_malloc, self._tbl, out_ptr, out_len, out_err)
        _check(rc, out_err)

        ptr = out_ptr[0]
        ln = int(out_len[0])
        return _ForeignIPC(ptr, ln)

    def _normalize_json_text_columns(
        self,
        json_text_columns,
        *,
        arg_name: str = "json_text_columns",
    ) -> tuple[int, ...]:
        if not json_text_columns:
            return ()
        if isinstance(json_text_columns, (str, int)) and not isinstance(
            json_text_columns, bool
        ):
            json_text_columns = (json_text_columns,)
        out = set()
        names = None
        count = None
        for column in json_text_columns:
            if isinstance(column, bool):
                raise ValueError(
                    f"{arg_name} must contain non-negative column indexes or column names"
                )
            if isinstance(column, str):
                if names is None:
                    names = self.column_names()
                matches = [idx for idx, name in enumerate(names) if name == column]
                if not matches:
                    raise ValueError(
                        f"{arg_name} references unknown column name: {column!r}"
                    )
                if len(matches) > 1:
                    raise ValueError(
                        f"{arg_name} references ambiguous column name: {column!r}; "
                        "use a column index"
                    )
                index = matches[0]
            elif isinstance(column, int):
                index = column
            else:
                raise ValueError(
                    f"{arg_name} must contain non-negative column indexes or column names"
                )
            if index < 0:
                raise ValueError(
                    f"{arg_name} must contain non-negative column indexes or column names"
                )
            if count is None:
                count = self.column_count()
            if index >= count:
                raise ValueError(
                    f"{arg_name} references column index {index}, but table has {count} columns"
                )
            out.add(index)
        return tuple(sorted(out))

    def _record_fields(self) -> list[str]:
        names = self.column_names()
        seen = set()
        duplicates = []
        for field in names:
            if field in seen and field not in duplicates:
                duplicates.append(field)
            seen.add(field)
        if duplicates:
            duplicate = duplicates[0]
            raise ValueError(
                "to_records() requires unique result column names; "
                f"duplicate column name: {duplicate!r}"
            )
        return names

    def _copy_json_rows(
        self,
        *,
        parse_json: bool = True,
        json_text_columns=(),
        allow_nonfinite: bool = False,
        include_paths: bool = False,
    ) -> bytes:
        """Copy materialized JSON rows into Python-owned bytes."""
        self._ensure_open()

        json_text_columns = self._normalize_json_text_columns(json_text_columns)
        out_ptr = ffi.new("uint8_t **")
        out_len = ffi.new("size_t *")
        out_err = ffi.new("char **")
        if include_paths:
            columns = ffi.new("size_t[]", json_text_columns) if json_text_columns else ffi.NULL
            rc = self._call_native(
                _runtime_symbol("velr_table_python_rows_with_paths_malloc"),
                self._tbl,
                columns,
                len(json_text_columns),
                out_ptr,
                out_len,
                out_err,
            )
        elif allow_nonfinite:
            columns = ffi.new("size_t[]", json_text_columns) if json_text_columns else ffi.NULL
            rc = self._call_native(
                _runtime_symbol("velr_table_python_rows_malloc"),
                self._tbl,
                int(parse_json),
                columns,
                len(json_text_columns),
                out_ptr,
                out_len,
                out_err,
            )
        elif json_text_columns:
            columns = ffi.new("size_t[]", json_text_columns)
            rc = self._call_native(
                _runtime_symbol("velr_table_json_rows_with_options_malloc"),
                self._tbl,
                1 if parse_json else 0,
                columns,
                len(json_text_columns),
                out_ptr,
                out_len,
                out_err,
            )
        else:
            rc = self._call_native(
                _runtime_symbol("velr_table_json_rows_malloc"),
                self._tbl,
                1 if parse_json else 0,
                out_ptr,
                out_len,
                out_err,
            )
        _check(rc, out_err)

        ptr = out_ptr[0]
        length = out_len[0]
        if ptr == ffi.NULL or length == 0:
            return b""
        try:
            return ffi.buffer(ptr, length)[:]
        finally:
            lib.velr_free(ptr, length)

    def to_pyarrow_zero_copy(self):
        """Return a ``pyarrow.Buffer`` containing Arrow IPC file bytes.

        This is an encoded IPC buffer, not a ``pyarrow.Table``. PyArrow must be
        installed. The buffer retains its memory independently of the table handle;
        keep the returned buffer alive while using its data. The method avoids a
        Python bytes copy but still exports the result. Raises ``VelrError`` for an
        unavailable export, closed table, or missing PyArrow.
        """
        return self._to_ipc_foreign().to_buffer()

    def to_pyarrow(self):
        """Export this result as a ``pyarrow.Table``.

        Requires PyArrow. The returned table owns references to its exported data and
        remains usable after the Velr table closes. Raises ``VelrError`` for a closed
        table, unavailable export, or missing PyArrow; Arrow decoding errors propagate.
        Types follow the inferred Arrow schema described on :class:`Table`;
        structured Cypher values are JSON strings, with null values preserved.
        Export materializes data and may use memory proportional to the result size.
        """
        return self._to_ipc_foreign().to_pyarrow()

    def to_pandas(self, **kwargs):
        """Export this result as a pandas DataFrame.

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
        """
        return self._to_ipc_foreign().to_pandas(**kwargs)

    def to_polars(self):
        """Export this result as a Polars DataFrame.

        Requires Polars and PyArrow. The result remains usable after this table
        closes. Export/dependency failures raise ``VelrError``; conversion errors
        from Polars or PyArrow propagate. Arrow column conversion follows
        :meth:`to_pyarrow`; structured Cypher values remain JSON strings.
        """
        return self._to_ipc_foreign().to_polars()

    def to_ipc_memoryview(self) -> memoryview:
        """Export Arrow IPC file bytes as a ``memoryview``.

        The view retains the exported memory independently of the table handle; keep
        the view alive while consuming it. No PyArrow installation is required.
        Raises ``VelrError`` for a closed table or unavailable export.
        """
        return self._to_ipc_foreign().raw_memoryview()

    def to_ipc_bytes(self) -> bytes:
        """Return an owned ``bytes`` copy of this result's Arrow IPC file.

        The bytes remain valid after the table closes and can be saved or passed to
        ``bind_arrow_ipc()``. No PyArrow installation is required. Export failures
        and closed-table access raise ``VelrError``. The file uses the inferred
        Arrow column types described on :class:`Table`.
        """
        return self._to_ipc_foreign().to_bytes()

    def _to_json_rows_bytes(self, *, parse_json: bool = True, json_text_columns=()) -> bytes:
        """Return this table as JSON row-array bytes.

        The JSON payload is an array of row arrays. When ``parse_json`` is true,
        Cypher map/list/JSON cells are embedded as JSON values; otherwise they
        are returned as JSON strings containing their serialized text.

        ``json_text_columns`` can name zero-based TEXT column indexes or column
        names that should be embedded as serialized JSON values. This is useful
        for application schemas that intentionally store JSON documents in text
        properties.
        """
        try:
            return self._copy_json_rows(
                parse_json=parse_json,
                json_text_columns=json_text_columns,
            )
        except VelrError as exc:
            if "does not expose velr_table_json_rows" not in str(exc):
                raise
            rows = self._to_rows_fallback(
                parse_json=parse_json,
                json_text_columns=json_text_columns,
            )
            return json.dumps(rows, separators=(",", ":")).encode("utf-8")

    def _to_rows_fallback(self, *, parse_json: bool = True, json_text_columns=()):
        json_text_columns = self._normalize_json_text_columns(json_text_columns)
        rows = [list(row) for row in self._collect_python(parse_json=parse_json)]
        if json_text_columns:
            for row in rows:
                for index in json_text_columns:
                    if index >= len(row):
                        continue
                    value = row[index]
                    if isinstance(value, str):
                        row[index] = json.loads(value)
        return rows

    def to_rows(self, columns=None):
        """Return the table as plain Python row lists.

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
        """
        columns = self._normalize_json_text_columns(columns, arg_name="columns")
        try:
            encoded = self._copy_json_rows(
                json_text_columns=columns,
                include_paths=True,
            )
        except VelrError as exc:
            if "does not expose velr_table_python_rows_with_paths_malloc" not in str(exc):
                raise
        else:
            rows = []
            for row, paths in json.loads(encoded):
                for location in paths:
                    parent = row
                    for key in location[:-1]:
                        parent = parent[key]
                    key = location[-1]
                    parent[key] = Path(tuple(parent[key]))
                rows.append(row)
            return rows

        try:
            return json.loads(
                self._copy_json_rows(
                    json_text_columns=columns,
                    allow_nonfinite=True,
                )
            )
        except VelrError as exc:
            if "does not expose velr_table_python_rows_malloc" not in str(exc):
                raise
            return self._to_rows_fallback(json_text_columns=columns)

    def to_records(self, columns=None):
        """Return the table as plain Python dict records.

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
        """
        fields = self._record_fields()
        return [dict(zip(fields, row)) for row in self.to_rows(columns=columns)]

    def write_ipc_to(self, fileobj) -> int:
        """Write Arrow IPC file bytes to an open binary file-like object.

        Return the value returned by ``fileobj.write`` (normally the byte count).
        The destination stays open. Export failures raise ``VelrError``; destination
        I/O exceptions propagate. No PyArrow installation is required.
        """
        return self._to_ipc_foreign().write_to(fileobj)

    def save_ipc(self, path: str) -> None:
        """Write Arrow IPC file bytes to ``path`` and return ``None``.

        Open the file in binary write mode, replacing existing contents, and close
        it before return. Export failures raise ``VelrError``; filesystem errors
        propagate. No PyArrow installation is required.
        """
        self._to_ipc_foreign().save(path)

    def column_count(self) -> int:
        """Return the number of projected columns as an integer.

        A zero-row result still exposes its projected columns. The table must
        remain open; closed-handle access raises ``VelrError``.
        """
        self._ensure_open()
        return int(self._call_native(lib.velr_table_column_count, self._tbl))

    def column_names(self) -> List[str]:
        """Return a new list of projected UTF-8 column names, in result order.

        The returned list survives table closure, but this method requires an open
        table and raises ``VelrError`` after closure. Give projected expressions
        unique aliases in ``RETURN``; duplicate aliases fail query validation
        before a table is created. Invalid UTF-8 raises ``UnicodeDecodeError``.
        """
        self._ensure_open()

        n = self.column_count()
        out_ptr = ffi.new("const uint8_t *[1]")
        out_len = ffi.new("size_t[1]")
        names: List[str] = []

        for i in range(n):
            rc = self._call_native(lib.velr_table_column_name, self._tbl, i, out_ptr, out_len)
            _check_noerr(rc, f"velr_table_column_name failed at idx={i}")
            b = ffi.string(out_ptr[0], out_len[0])
            names.append(b.decode("utf-8", errors="strict"))

        return names

    def iter_rows(self) -> "Rows":
        """Open a cursor-style row iterator for this table.

        Iteration yields ``Cell`` objects one row at a time. This avoids
        materializing all result rows as Python values; it does not cap the
        query's execution memory or time. Each returned row is a tuple of copied
        ``Cell`` values.

        Returns a :class:`Rows` handle. Use ``with table.iter_rows() as rows`` when
        stopping early; exhausting it closes the iterator automatically. Closing the
        table closes its row iterators. Fetch failures raise ``VelrError``.
        Iteration observes the query's original deadline and cancellation token.
        """
        self._ensure_open()

        out_rows = ffi.new("velr_rows **")
        out_err = ffi.new("char **")
        rc = self._call_native(lib.velr_table_rows_open, self._tbl, out_rows, out_err)
        _check(rc, out_err)
        if out_rows[0] == ffi.NULL:
            raise VelrError("velr_table_rows_open returned null rows")

        rows = Rows(out_rows[0], self.column_count(), parent=self)
        self._register_child(rows)
        return rows

    def rows(self) -> "Rows":
        """Deprecated alias for ``iter_rows``; prefer ``iter_rows`` in new code.

        Returns the same owned row cursor and has the same close, error, and incremental
        consumption contract as ``Table.iter_rows``.
        """
        warnings.warn(
            "Table.rows() is deprecated; use Table.iter_rows()",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.iter_rows()

    def for_each_row(self, fn) -> None:
        """Call ``fn(row)`` for every row and return ``None``.

        Each row is a tuple of copied :class:`Cell` objects. The temporary row
        iterator is closed even when ``fn`` raises; callback exceptions propagate.
        The table itself remains open. ``fn`` runs synchronously; coroutine or
        awaitable return values are not awaited. Fetch failures raise ``VelrError``.
        Retained cells survive cursor/table/connection closure.
        """
        with self.iter_rows() as rows:
            for row in rows:
                fn(row)

    def collect(self, map_fn=None):
        """Collect rows into a list.

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
        """
        out = []
        with self.iter_rows() as rows:
            if map_fn is None:
                for row in rows:
                    out.append(row)
            else:
                for row in rows:
                    out.append(map_fn(row))
        return out

    def _iter_python(
        self,
        *,
        decode_text: bool = True,
        parse_json: bool = False,
        encoding: str = "utf-8",
        errors: str = "strict",
    ):
        """Yield rows converted to plain Python values.

        This is equivalent to iterating over ``iter_rows()`` and calling
        ``cell.as_python(...)`` for every cell, but avoids allocating
        intermediate ``Cell`` objects.
        """
        with self.iter_rows() as rows:
            yield from rows._iter_python(
                decode_text=decode_text,
                parse_json=parse_json,
                encoding=encoding,
                errors=errors,
            )

    def _collect_python(
        self,
        *,
        decode_text: bool = True,
        parse_json: bool = False,
        encoding: str = "utf-8",
        errors: str = "strict",
    ):
        """Collect rows converted to plain Python values."""
        return list(
            self._iter_python(
                decode_text=decode_text,
                parse_json=parse_json,
                encoding=encoding,
                errors=errors,
            )
        )

    def close(self) -> None:
        """Close this table and any open row iterators it produced.

        Repeated calls are harmless. Previously copied ``Cell`` values, Python
        rows/records, and exported data remain usable; further table operations raise
        ``VelrError``. Prefer a context manager for deterministic cleanup.
        """
        self._check_thread()
        self._close_children_first()
        if self._tbl != ffi.NULL:
            try:
                self._call_native(lib.velr_table_close, self._tbl)
            finally:
                self._tbl = ffi.NULL
                self._control = None
                try:
                    self._parent._unregister_child(self)
                except Exception:
                    pass

    def __enter__(self) -> "Table":
        """Return this handle for use in a ``with`` block.
        """
        self._check_thread()
        return self

    def __exit__(self, exc_type, exc, tb):
        """Close this handle on block exit without suppressing the exception.
        """
        self.close()


class Rows(_NativeHandle):
    """Iterator over rows in a table.

    Iteration yields ``tuple[Cell, ...]``.

    Use ``close()`` when done, or use the iterator as a context manager.

    Obtain this handle from ``Table.iter_rows()``, not by direct construction.
    Exhaustion closes it automatically. Closing its table also closes it. Fetch
    errors raise ``VelrError``; ordinary exhaustion raises ``StopIteration``.
    """

    __slots__ = (
        "_rows", "_ncols", "_buf", "_written", "_parent", "_json_columns", "__weakref__",
    )

    def __init__(self, rows_cdata, ncols: int, parent: "Table"):
        """@private"""
        self._rows = rows_cdata
        self._ncols = ncols
        self._buf = ffi.new("velr_cell[]", ncols)
        self._written = ffi.new("size_t *")
        self._json_columns = None
        self._init_child(parent)

    def __iter__(self):
        """Return the iterator itself."""
        self._check_thread()
        return self

    def __next__(self):
        """Return the next row as a tuple of copied :class:`Cell` objects.

        Raise ``StopIteration`` and close the iterator at end-of-results or when
        already closed. Query/fetch errors raise ``VelrError``. Returned cells remain
        usable after further iteration and after parent handles close.
        """
        n = self._next_row_cell_count()

        row = []
        columns = self._json_columns
        for i in range(n):
            c = self._buf[i]
            ty = c.ty
            data = b""
            template = None
            if ty == _CELL_JSON:
                data = _cell_bytes(c.ptr, c.len, "VELR_JSON cell")
                if columns is None:
                    columns = self._json_columns = [
                        _JsonTemplateColumn()
                        for _ in range(min(n, _JSON_TEMPLATE_COLUMNS))
                    ]
                column = columns[i] if i < len(columns) else None
                if column is not None:
                    if column.previous == data:
                        template = column.template
                        if template is None:
                            if column.repeated:
                                # A pair alone would pay to build an unused plan.
                                template = column.template = _ParsedJsonTemplate(column.previous)
                            else:
                                column.repeated = True
                                column.misses = 0
                        data = column.previous
                    else:
                        # A column of unique values only pays for a few comparisons.
                        column.misses += 1
                        if column.misses < _JSON_TEMPLATE_MISSES and len(data) <= _JSON_TEMPLATE_BYTES:
                            column.previous = data
                            column.repeated = False
                            column.template = None
                        else:
                            columns[i] = None
            else:
                if ty == _CELL_TEXT:
                    data = _cell_bytes(c.ptr, c.len, "VELR_TEXT cell")
                if columns is not None and i < len(columns):
                    column = columns[i]
                    if column is not None and column.previous is not None:
                        column.previous = None
                        column.repeated = False
                        column.template = None
            cell = Cell(ty, int(c.i64_), float(c.f64_), data)
            if template is not None:
                # Retained cells own their bytes. The cursor owns the template.
                object.__setattr__(cell, "_json_template", template.reference)
            row.append(cell)
        return tuple(row)

    def _next_row_cell_count(self) -> int:
        self._check_thread()
        if self._rows == ffi.NULL:
            raise StopIteration

        out_err = ffi.new("char **")
        rc = self._call_native(
            lib.velr_rows_next, self._rows, self._buf, self._ncols, self._written, out_err,
        )

        if rc < 0:
            try:
                self.close()
            finally:
                _check(rc, out_err)

        if rc == 0:
            _free_unexpected_err(out_err)
            self.close()
            raise StopIteration

        _free_unexpected_err(out_err)

        n = int(self._written[0])
        if n > self._ncols:
            self.close()
            raise VelrError(
                f"velr_rows_next reported {n} cells, but the buffer holds {self._ncols}"
            )
        return n

    def _next_python(
        self,
        *,
        decode_text: bool = True,
        parse_json: bool = False,
        encoding: str = "utf-8",
        errors: str = "strict",
    ):
        """Fetch the next row as plain Python values."""
        n = self._next_row_cell_count()
        row = [None] * n
        velr_null = lib.VELR_NULL
        velr_bool = lib.VELR_BOOL
        velr_int64 = lib.VELR_INT64
        velr_double = lib.VELR_DOUBLE
        velr_text = lib.VELR_TEXT
        velr_json = lib.VELR_JSON
        parse_json_bytes = parse_json and encoding == "utf-8" and errors == "strict"
        for i in range(n):
            cell = self._buf[i]
            ty = cell.ty
            if ty == velr_null:
                value = None
            elif ty == velr_bool:
                value = bool(cell.i64_)
            elif ty == velr_int64:
                value = int(cell.i64_)
            elif ty == velr_double:
                value = float(cell.f64_)
            elif ty == velr_text:
                data = _cell_bytes(cell.ptr, cell.len, "VELR_TEXT cell")
                value = data.decode(encoding, errors) if decode_text else data
            elif ty == velr_json:
                data = _cell_bytes(cell.ptr, cell.len, "VELR_JSON cell")
                if parse_json_bytes:
                    value = json.loads(data)
                elif parse_json:
                    value = json.loads(data.decode(encoding, errors))
                else:
                    value = data.decode(encoding, errors) if decode_text else data
            else:
                value = _cell_bytes(cell.ptr, cell.len, "VELR cell")
            row[i] = value
        return tuple(row)

    def _iter_python(
        self,
        *,
        decode_text: bool = True,
        parse_json: bool = False,
        encoding: str = "utf-8",
        errors: str = "strict",
    ):
        """Yield rows as plain Python values.

        This has the same conversion behavior as ``Cell.as_python(...)`` and
        closes the iterator when exhausted.
        """
        self._check_thread()
        try:
            rows_next_batch = lib.velr_rows_next_batch
        except AttributeError:
            rows_next_batch = None

        if rows_next_batch is not None:
            yield from self._iter_python_batched(
                rows_next_batch,
                decode_text=decode_text,
                parse_json=parse_json,
                encoding=encoding,
                errors=errors,
            )
            return

        while True:
            try:
                yield self._next_python(
                    decode_text=decode_text,
                    parse_json=parse_json,
                    encoding=encoding,
                    errors=errors,
                )
            except StopIteration:
                return

    def _iter_python_batched(
        self,
        rows_next_batch,
        *,
        decode_text: bool,
        parse_json: bool,
        encoding: str,
        errors: str,
    ):
        batch_rows = 1024
        buf_len = self._ncols * batch_rows
        batch_buf = ffi.new("velr_cell[]", max(1, buf_len))
        out_rows = ffi.new("size_t *")
        out_cells = ffi.new("size_t *")

        velr_null = lib.VELR_NULL
        velr_bool = lib.VELR_BOOL
        velr_int64 = lib.VELR_INT64
        velr_double = lib.VELR_DOUBLE
        velr_text = lib.VELR_TEXT
        velr_json = lib.VELR_JSON
        parse_json_bytes = parse_json and encoding == "utf-8" and errors == "strict"

        while True:
            self._check_thread()
            if self._rows == ffi.NULL:
                return

            out_err = ffi.new("char **")
            rc = self._call_native(
                rows_next_batch,
                self._rows,
                batch_buf,
                buf_len,
                batch_rows,
                out_rows,
                out_cells,
                out_err,
            )

            if rc < 0:
                try:
                    self.close()
                finally:
                    _check(rc, out_err)

            if rc == 0:
                _free_unexpected_err(out_err)
                self.close()
                return

            _free_unexpected_err(out_err)
            row_count = int(out_rows[0])
            cell_count = int(out_cells[0])
            expected_cells = row_count * self._ncols
            if cell_count != expected_cells:
                self.close()
                raise VelrError(
                    "velr_rows_next_batch reported "
                    f"{cell_count} cells for {row_count} rows and {self._ncols} columns"
                )

            for row_index in range(row_count):
                offset = row_index * self._ncols
                row = [None] * self._ncols
                for col_index in range(self._ncols):
                    cell = batch_buf[offset + col_index]
                    ty = cell.ty
                    if ty == velr_null:
                        value = None
                    elif ty == velr_bool:
                        value = bool(cell.i64_)
                    elif ty == velr_int64:
                        value = int(cell.i64_)
                    elif ty == velr_double:
                        value = float(cell.f64_)
                    elif ty == velr_text:
                        data = _cell_bytes(cell.ptr, cell.len, "VELR_TEXT cell")
                        value = data.decode(encoding, errors) if decode_text else data
                    elif ty == velr_json:
                        data = _cell_bytes(cell.ptr, cell.len, "VELR_JSON cell")
                        if parse_json_bytes:
                            value = json.loads(data)
                        elif parse_json:
                            value = json.loads(data.decode(encoding, errors))
                        else:
                            value = data.decode(encoding, errors) if decode_text else data
                    else:
                        value = _cell_bytes(cell.ptr, cell.len, "VELR cell")
                    row[col_index] = value
                yield tuple(row)

    def close(self) -> None:
        """Close this row iterator without closing its table.

        Repeated calls are harmless. Future iteration stops; previously returned
        ``Cell`` tuples remain usable. Use a context manager when stopping early.
        """
        self._check_thread()
        if self._rows != ffi.NULL:
            try:
                self._call_native(lib.velr_rows_close, self._rows)
            finally:
                self._rows = ffi.NULL
                self._json_columns = None
                try:
                    self._parent._unregister_child(self)
                except Exception:
                    pass

    def __enter__(self) -> "Rows":
        """Return this handle for use in a ``with`` block.
        """
        self._check_thread()
        return self

    def __exit__(self, exc_type, exc, tb):
        """Close this handle on block exit without suppressing the exception.
        """
        self.close()


def _cell_cdata_as_python(
    cell,
    *,
    decode_text: bool = True,
    parse_json: bool = False,
    encoding: str = "utf-8",
    errors: str = "strict",
):
    """Convert a borrowed runtime cell to a Python value."""
    if cell.ty == lib.VELR_NULL:
        return None
    if cell.ty == lib.VELR_BOOL:
        return bool(cell.i64_)
    if cell.ty == lib.VELR_INT64:
        return int(cell.i64_)
    if cell.ty == lib.VELR_DOUBLE:
        return float(cell.f64_)
    if cell.ty == lib.VELR_TEXT:
        data = _cell_bytes(cell.ptr, cell.len, "VELR_TEXT cell")
        return data.decode(encoding, errors) if decode_text else data
    if cell.ty == lib.VELR_JSON:
        data = _cell_bytes(cell.ptr, cell.len, "VELR_JSON cell")
        if parse_json:
            if encoding == "utf-8" and errors == "strict":
                return json.loads(data)
            return json.loads(data.decode(encoding, errors))
        return data.decode(encoding, errors) if decode_text else data
    return _cell_bytes(cell.ptr, cell.len, "VELR cell")


@dataclass
class _NamedSavepointEntry:
    """Transaction-owned named savepoint state."""
    name: str
    sp: object
    active: bool = True



class Savepoint(_NativeHandle):
    """A savepoint within a transaction.

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
    """

    __slots__ = ("_sp", "_closed", "_parent", "_named_entry", "__weakref__")

    def __init__(
        self,
        sp_cdata,
        parent: "VelrTx",
        *,
        named_entry: _NamedSavepointEntry | None = None,
    ):
        """@private"""
        self._sp = sp_cdata if sp_cdata is not None else ffi.NULL
        self._closed = False
        self._init_child(parent)
        self._named_entry = named_entry

    def release(self) -> None:
        """Release this savepoint while retaining its changes in the transaction.

        This does not commit the outer transaction. Repeated calls on a closed
        handle are harmless. Named savepoints must be released in reverse creation
        order; invalid or wrong-thread operations raise ``VelrError``. Returns
        ``None``. A named marker consumed through its transaction can make further
        handle operations fail even when the Python handle still exists.
        """
        self._check_thread()
        if self._closed:
            return

        # Named savepoint: transaction-owned marker.
        if self._named_entry is not None:
            try:
                self._parent._release_named_entry(self._named_entry)
            finally:
                if not self._named_entry.active:
                    self._closed = True
                    self._sp = ffi.NULL
            return

        # Scoped savepoint: handle-owned.
        if self._sp == ffi.NULL:
            self._closed = True
            return

        sp = self._sp
        self._sp = ffi.NULL
        self._closed = True

        try:
            out_err = ffi.new("char **")
            rc = self._call_native(lib.velr_sp_release, sp, out_err)
            _check(rc, out_err)
        finally:
            try:
                self._parent._unregister_child(self)
            except Exception:
                pass

    def rollback(self) -> None:
        """Undo changes after this savepoint, then release it.

        The outer transaction remains active. This differs from
        ``VelrTx.rollback_to(name)``, which keeps the target named savepoint active.
        Calling this on a closed or already-consumed handle raises ``VelrError``;
        ``release()`` and ``close()`` have different repeated-call behavior. Runtime
        failures also raise ``VelrError``. Returns ``None`` on success.
        """
        self._check_thread()
        if self._closed or (self._named_entry is None and self._sp == ffi.NULL):
            raise HandleStateError("savepoint is closed", detail="ClosedSavepoint")

        # Named savepoint: rollback-to + release target.
        if self._named_entry is not None:
            try:
                self._parent._rollback_named_entry(self._named_entry, release_target=True)
            finally:
                if not self._named_entry.active:
                    self._closed = True
                    self._sp = ffi.NULL
            return

        sp = self._sp
        self._sp = ffi.NULL
        self._closed = True

        try:
            out_err = ffi.new("char **")
            rc = self._call_native(lib.velr_sp_rollback, sp, out_err)
            _check(rc, out_err)
        finally:
            try:
                self._parent._unregister_child(self)
            except Exception:
                pass

    def close(self) -> None:
        """Close this handle without committing the outer transaction.

        An active unnamed scoped savepoint is rolled back and released. Closing a
        named handle leaves its transaction-owned savepoint active; use
        ``release()``, ``rollback()``, or the transaction's named-savepoint methods
        to change it. Repeated calls are harmless. Prefer a ``with`` block when you
        want release on success and rollback on an exception.
        """
        self._check_thread()
        if self._closed:
            return

        # Named savepoint handles do not own the runtime marker.
        if self._named_entry is not None:
            self._closed = True
            self._sp = ffi.NULL
            return

        if self._sp != ffi.NULL:
            try:
                self._call_native(lib.velr_sp_close, self._sp)
            finally:
                self._sp = ffi.NULL
                self._closed = True
                try:
                    self._parent._unregister_child(self)
                except Exception:
                    pass
        else:
            self._closed = True

    def __enter__(self) -> "Savepoint":
        """Return this handle for use in a ``with`` block.
        """
        self._check_thread()
        return self

    def __exit__(self, exc_type, exc, tb):
        """Release on normal exit, or roll back and release on exceptional exit.

        Does not commit the outer transaction or suppress the block exception.
        A savepoint cleanup error can replace an exception raised by the block.
        """
        self._check_thread()
        if self._closed:
            return
        if exc_type is not None:
            self.rollback()
        else:
            self.release()

class VelrTx(_NativeHandle):
    """A transaction handle.

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
    """

    __slots__ = (
        "_tx",
        "_closed",
        "_children",
        "_parent",
        "_named_savepoints",
        "__weakref__",
    )

    def __init__(self, tx_cdata, parent: "Velr"):
        """@private"""
        self._tx = tx_cdata
        self._closed = False
        self._children = _ChildHandles()
        self._init_child(parent)
        self._named_savepoints: list[_NamedSavepointEntry] = []

    def _ensure_open(self):
        self._check_thread()
        if self._tx == ffi.NULL or self._closed:
            raise HandleStateError("tx already closed", detail="ClosedTransaction")

    def _register_child(self, child) -> None:
        self._children.add(child)

    def _unregister_child(self, child) -> None:
        try:
            self._children.discard(child)
        except Exception:
            pass

    def _close_children_first(self) -> None:
        self._children.close()

    def _find_named_index(self, name: str) -> int | None:
        for i in range(len(self._named_savepoints) - 1, -1, -1):
            entry = self._named_savepoints[i]
            if entry.active and entry.name == name:
                return i
        return None

    def _find_named_entry_index(self, wanted: _NamedSavepointEntry) -> int | None:
        for i in range(len(self._named_savepoints) - 1, -1, -1):
            if self._named_savepoints[i] is wanted and wanted.active:
                return i
        return None

    def _invalidate_named_savepoints(self) -> None:
        for entry in self._named_savepoints:
            entry.active = False
            entry.sp = ffi.NULL
        self._named_savepoints.clear()

    def _create_named_runtime_savepoint(self, name: str):
        """Create one runtime named savepoint and return the raw handle."""
        out_sp = ffi.new("velr_sp **")
        out_err = ffi.new("char **")
        rc = self._call_native(
            lib.velr_tx_savepoint_named,
            self._tx,
            name.encode("utf-8"),
            out_sp,
            out_err,
        )
        _check(rc, out_err)
        if out_sp[0] == ffi.NULL:
            raise VelrError("velr_tx_savepoint_named returned null savepoint")
        return out_sp[0]

    def _prune_named_after(self, idx: int) -> None:
        """Discard all active named savepoints newer than idx."""
        for newer in self._named_savepoints[idx + 1 :]:
            newer.active = False
            newer.sp = ffi.NULL
        del self._named_savepoints[idx + 1 :]

    def _retain_named_target_after_rollback(self, idx: int) -> None:
        """Recreate the rolled-back-to named savepoint so it remains active.

        This emulates retained-target semantics even if the underlying runtime
        consumes the target during rollback_to(name).
        """
        entry = self._named_savepoints[idx]

        try:
            new_sp = self._create_named_runtime_savepoint(entry.name)
        except Exception as e:
            # The rollback already happened in the runtime. Reflect the actual
            # post-rollback state honestly: target is gone, and newer savepoints
            # are also gone.
            entry.active = False
            entry.sp = ffi.NULL
            for newer in self._named_savepoints[idx + 1 :]:
                newer.active = False
                newer.sp = ffi.NULL
            del self._named_savepoints[idx:]
            raise VelrError(
                f"rollback_to({entry.name!r}) succeeded, but failed to recreate "
                f"the named savepoint to preserve retained-target semantics: {e}"
            ) from e

        entry.sp = new_sp
        entry.active = True
        self._prune_named_after(idx)

    def _release_named_entry(self, entry: _NamedSavepointEntry) -> None:
        self._ensure_open()

        if not entry.active or entry.sp == ffi.NULL:
            raise HandleStateError(f"no such savepoint '{entry.name}'", detail="UnknownSavepoint")

        idx = self._find_named_entry_index(entry)
        if idx is None:
            raise HandleStateError(f"no such savepoint '{entry.name}'", detail="UnknownSavepoint")

        if idx != len(self._named_savepoints) - 1:
            raise HandleStateError("named savepoint can only be released from the top of the stack", detail="SavepointOrder")

        sp = entry.sp
        entry.sp = ffi.NULL
        entry.active = False
        self._named_savepoints.pop()

        out_err = ffi.new("char **")
        rc = self._call_native(lib.velr_sp_release, sp, out_err)
        _check(rc, out_err)

    def _rollback_named_entry(
        self,
        entry: _NamedSavepointEntry,
        *,
        release_target: bool,
    ) -> None:
        self._ensure_open()

        if not entry.active:
            raise HandleStateError(f"no such savepoint '{entry.name}'", detail="UnknownSavepoint")

        idx = self._find_named_entry_index(entry)
        if idx is None:
            raise HandleStateError(f"no such savepoint '{entry.name}'", detail="UnknownSavepoint")

        if release_target:
            # Handle semantics: rollback to this exact savepoint and consume it.
            sp = entry.sp
            for doomed in self._named_savepoints[idx:]:
                doomed.active = False
                doomed.sp = ffi.NULL
            del self._named_savepoints[idx:]

            out_err = ffi.new("char **")
            rc = self._call_native(lib.velr_sp_rollback, sp, out_err)
            _check(rc, out_err)
            return

        # Name-based transaction semantics: rollback to target, discard newer,
        # and keep the target logically active.
        out_err = ffi.new("char **")
        rc = self._call_native(lib.velr_tx_rollback_to, self._tx, entry.name.encode("utf-8"), out_err)
        _check(rc, out_err)

        self._retain_named_target_after_rollback(idx)

    def run(
        self,
        cypher: str,
        *,
        max_result_rows: int | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        """Execute a query or script inside the transaction and discard results.

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
        """
        self._ensure_open()
        _call_query(
            self._call_native,
            "velr_tx_run", self._tx, cypher, max_result_rows, params, timeout, cancellation
        )

    execute = run

    def exec(
        self,
        cypher: str,
        *,
        max_result_rows: int | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> "StreamTx":
        """Execute a query inside the transaction and return a stream of tables.

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
        """
        self._ensure_open()
        out_st = ffi.new("velr_stream_tx **")
        control = _call_query(
            self._call_native,
            "velr_tx_exec_start", self._tx, cypher, max_result_rows, params, timeout, cancellation, out_st
        )
        if out_st[0] == ffi.NULL:
            raise VelrError("velr_tx_exec_start returned null stream")

        st = StreamTx(out_st[0], parent=self, control=control)
        self._register_child(st)
        return st

    def exec_one(
        self,
        cypher: str,
        *,
        max_result_rows: int | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> "Table":
        """Execute a query inside the transaction expecting exactly one table.

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
        """
        self._ensure_open()

        st = self.exec(
            cypher, max_result_rows=max_result_rows, params=params, timeout=timeout, cancellation=cancellation
        )
        t: Table | None = None
        try:
            t = st.next_table()
            if t is None:
                raise VelrError("query produced no result tables")

            if getattr(t, "_parent", None) is st:
                try:
                    st._unregister_child(t)
                except Exception:
                    pass
                t._parent = self
                self._register_child(t)

            t2 = st.next_table()
            if t2 is not None:
                try:
                    t2.close()
                finally:
                    t.close()
                raise VelrError("query produced multiple tables; use exec() to stream them")

            st.close()
            return t

        except Exception:
            try:
                st.close()
            except Exception:
                pass

            if t is not None and getattr(t, "_parent", None) is self:
                try:
                    t.close()
                except Exception:
                    pass

            raise

    def explain(self, cypher: str) -> ExplainTrace:
        """Explain query text without executing it.

        Accepts NUL-free Unicode ``str`` query text only,
        without parameters, row caps, timeouts, or cancellation arguments. Keep the
        trace and its parent transaction open while reading; close it explicitly or
        use a context manager. Closing the parent closes the trace. Copied snapshots,
        metadata, and compact strings/bytes remain usable afterward.

        Query/runtime failures and closed, busy, or wrong-thread handles raise
        ``VelrError``. Unsupported text objects or encoding failures can raise
        standard Python exceptions such as ``AttributeError`` or ``UnicodeEncodeError``.
        """
        self._ensure_open()
        out_xp = ffi.new("velr_explain_trace **")
        out_err = ffi.new("char **")
        rc = self._call_native(lib.velr_tx_explain, self._tx, cypher.encode("utf-8"), out_xp, out_err)
        _check(rc, out_err)
        if out_xp[0] == ffi.NULL:
            raise VelrError("velr_tx_explain returned null explain trace")

        xp = ExplainTrace(out_xp[0], parent=self)
        self._register_child(xp)
        return xp

    def explain_analyze(self, cypher: str) -> ExplainTrace:
        """Execute and profile query text, including its writes.

        Changes remain part of this transaction and follow its commit/rollback
        outcome. Accepts NUL-free Unicode ``str`` query text only,
        without parameters, row caps, timeouts, or cancellation arguments. Keep the
        trace and its parent transaction open while reading; close it explicitly or
        use a context manager. Closing the parent closes the trace. Copied snapshots,
        metadata, and compact strings/bytes remain usable afterward.

        Query/runtime failures and closed, busy, or wrong-thread handles raise
        ``VelrError``. Unsupported text objects or encoding failures can raise
        standard Python exceptions such as ``AttributeError`` or ``UnicodeEncodeError``.
        """
        self._ensure_open()
        out_xp = ffi.new("velr_explain_trace **")
        out_err = ffi.new("char **")
        rc = self._call_native(
            lib.velr_tx_explain_analyze, self._tx, cypher.encode("utf-8"), out_xp, out_err,
        )
        _check(rc, out_err)
        if out_xp[0] == ffi.NULL:
            raise VelrError("velr_tx_explain_analyze returned null explain trace")

        xp = ExplainTrace(out_xp[0], parent=self)
        self._register_child(xp)
        return xp

    def savepoint(self) -> Savepoint:
        """Return an unnamed :class:`Savepoint` scoped to this transaction.

        Use it as a context manager to release on success or undo its work on an
        exception. Closing an active scoped savepoint rolls back and releases it.
        A finished transaction or a savepoint-creation failure raises ``VelrError``.
        """
        self._ensure_open()
        out_sp = ffi.new("velr_sp **")
        out_err = ffi.new("char **")
        rc = self._call_native(lib.velr_tx_savepoint, self._tx, out_sp, out_err)
        _check(rc, out_err)
        if out_sp[0] == ffi.NULL:
            raise VelrError("velr_tx_savepoint returned null savepoint")

        sp = Savepoint(out_sp[0], parent=self)
        self._register_child(sp)
        return sp

    def savepoint_named(self, name: str) -> Savepoint:
        """Create a transaction-owned named savepoint.

        The named savepoint remains active in the transaction even if the returned
        Python handle is dropped. Use ``rollback_to(name)`` or
        ``release_savepoint(name)`` to act on it later by name.

        The returned handle supports the same success/exception context-manager
        behavior as scoped savepoints. Savepoint failures or a finished transaction
        raise ``VelrError``. Names must be unique, nonempty NUL-free strings
        while active; invalid names or duplicate markers raise runtime errors.
        Unsupported Python name objects can raise ``AttributeError`` on encoding.
        Release named savepoints in reverse creation order.
        """
        self._ensure_open()

        sp_cdata = self._create_named_runtime_savepoint(name)
        entry = _NamedSavepointEntry(name=name, sp=sp_cdata, active=True)
        self._named_savepoints.append(entry)

        # Returned handle is a controller/view over the transaction-owned marker.
        return Savepoint(None, parent=self, named_entry=entry)

    def rollback_to(self, name: str) -> None:
        """Roll back to a named savepoint.

        On success, changes made after the target named savepoint are undone, newer
        named savepoints are discarded, and the target named savepoint remains
        active and may be used again.

        An unknown name, a finished transaction, or rollback failure raises ``VelrError``.
        This returns ``None`` and does not commit the outer transaction.
        """
        self._ensure_open()

        idx = self._find_named_index(name)
        if idx is None:
            raise HandleStateError(f"no such savepoint '{name}'", detail="UnknownSavepoint")

        self._rollback_named_entry(self._named_savepoints[idx], release_target=False)

    def release_savepoint(self, name: str) -> None:
        """Release a named savepoint.

        The named savepoint must be the most recent active named savepoint.

        Returns ``None``. An unknown name, out-of-order release, or a finished
        transaction raises ``VelrError``; changes remain part of the outer transaction.
        """
        self._ensure_open()

        idx = self._find_named_index(name)
        if idx is None:
            raise HandleStateError(f"no such savepoint '{name}'", detail="UnknownSavepoint")

        entry = self._named_savepoints[idx]
        self._release_named_entry(entry)

    def _release_all_named_savepoints_for_commit(self) -> None:
        """Release all active named savepoints in LIFO order.

        This preserves all changes made inside retained/recreated named savepoints
        before the outer transaction commit.
        """
        self._ensure_open()

        while self._named_savepoints:
            entry = self._named_savepoints[-1]
            if not entry.active or entry.sp == ffi.NULL:
                self._named_savepoints.pop()
                continue
            self._release_named_entry(entry)

    def commit(self) -> None:
        """Commit the transaction.

        Any still-active named savepoints are released first so that surviving
        changes inside the transaction are preserved.

        After the commit operation consumes the transaction, it can no longer
        be used, including if that operation reports a failure. A failure while
        closing children or releasing savepoints can occur before consumption.

        Closes child result handles. Commit failures raise ``VelrError``; the
        transaction cannot be reused after the commit operation consumes it.
        """
        self._ensure_open()
        self._close_children_first()

        # Important: release any active named savepoints first so that changes
        # made after a retained/recreated rollback target are preserved.
        self._release_all_named_savepoints_for_commit()

        tx = self._tx
        self._tx = ffi.NULL
        self._closed = True

        try:
            out_err = ffi.new("char **")
            rc = self._call_native(lib.velr_tx_commit, tx, out_err)
            _check(rc, out_err)
        finally:
            self._invalidate_named_savepoints()
            try:
                self._parent._unregister_child(self)
            except Exception:
                pass

    def rollback(self) -> None:
        """Roll back the transaction.

        After this call, the transaction can no longer be used.

        Closes child result handles. Runtime failures or a finished transaction
        raise ``VelrError``. Returns ``None``.
        """
        self._ensure_open()
        self._close_children_first()

        tx = self._tx
        self._tx = ffi.NULL
        self._closed = True

        try:
            out_err = ffi.new("char **")
            rc = self._call_native(lib.velr_tx_rollback, tx, out_err)
            _check(rc, out_err)
        finally:
            self._invalidate_named_savepoints()
            try:
                self._parent._unregister_child(self)
            except Exception:
                pass

    def close(self) -> None:
        """Close this transaction, rolling back any uncommitted changes.

        Also close child streams, result tables, row iterators, and explain traces.
        Calling ``close()`` after commit, rollback, or a previous close is harmless.
        It returns ``None``; use ``commit()`` to retain changes.
        """
        self._check_thread()
        self._close_children_first()
        if self._tx != ffi.NULL and not self._closed:
            try:
                self._call_native(lib.velr_tx_close, self._tx)
            finally:
                self._tx = ffi.NULL
                self._closed = True
                self._invalidate_named_savepoints()
                try:
                    self._parent._unregister_child(self)
                except Exception:
                    pass

    def bind_arrow(self, logical: str, columns) -> None:
        """Bind Arrow columns for queries within this transaction.

        Accepts the same ``logical`` name and ``columns`` shapes as
        :meth:`Velr.bind_arrow`, including Tables, RecordBatches, arrays and chunked
        arrays. Requires PyArrow and returns ``None``. Binding alone does not create
        graph data. Conversion errors propagate; a finished transaction or binding
        failure raises ``VelrError``. Input lifetime/immutability and required
        column lengths/chunks follow ``Velr.bind_arrow``. Transaction-local bindings
        are available only until the transaction ends.
        """
        self._ensure_open()
        pairs = _normalize_arrow_columns(columns)
        (
            cols_c,
            name_views,
            _name_backing,
            _keep_schemas_nested,
            _keep_arrays_nested,
            _schema_pp_per_col,
            _array_pp_per_col,
        ) = _export_arrow_columns_for_bind_chunked(pairs)

        out_err = ffi.new("char **")
        logical_c = ffi.new("char[]", logical.encode("utf-8"))
        rc = self._call_native(
            lib.velr_tx_bind_arrow_chunks,
            self._tx,
            logical_c,
            cols_c,
            name_views,
            len(pairs),
            out_err,
        )
        _check(rc, out_err)

    def bind_arrow_ipc(self, logical: str, ipc_bytes) -> None:
        """Bind Arrow IPC file/Feather v2 bytes for this transaction's queries.

        Accepts the nonempty contiguous buffers described by
        :meth:`Velr.bind_arrow_ipc`; the buffer need only survive this call. Returns
        ``None``. Invalid buffers raise ``TypeError`` or ``ValueError``; invalid IPC,
        a finished transaction, or a binding failure raises ``VelrError``. PyArrow
        is not required. IPC streams are unsupported, and the logical name must be a
        NUL-free string. Transaction-local bindings end with the transaction.
        """
        self._ensure_open()
        cbuf, nbytes, view = _ipc_buffer(ipc_bytes)
        out_err = ffi.new("char **")
        logical_c = ffi.new("char[]", logical.encode("utf-8"))
        rc = self._call_native(
            _runtime_symbol("velr_tx_bind_arrow_ipc"),
            self._tx,
            logical_c,
            cbuf,
            nbytes,
            out_err,
        )
        _ = view
        _check(rc, out_err)

    def bind_arrow_chunks(self, logical: str, columns) -> None:
        """Alias for this transaction's ``bind_arrow``.

        Supports the same Arrow inputs and chunk layouts, requires PyArrow, returns
        ``None``, and has the same ownership/immutability and validation rules. The
        transaction must remain active; closed transactions raise ``VelrError``.
        """
        self.bind_arrow(logical, columns)

    def bind_pandas(
        self,
        logical: str,
        df: "pd.DataFrame",
        *,
        index: bool = False,
        jsonify_objects: bool = True,
        schema: "Optional['pa.Schema']" = None,
    ) -> None:
        """Bind a pandas DataFrame for queries within this transaction.

        ``index``, ``jsonify_objects``, and optional PyArrow ``schema`` have the same
        meaning as :meth:`Velr.bind_pandas`. Requires pandas/PyArrow, returns ``None``,
        and propagates conversion errors; binding failures or a finished transaction
        raise ``VelrError``.
        """
        tbl = _pandas_to_arrow_table(
            df,
            index=index,
            jsonify_objects=jsonify_objects,
            schema=schema,
        )
        self.bind_arrow(logical, tbl)

    def bind_polars(
        self,
        logical: str,
        df: "pl.DataFrame",
        *,
        rechunk: bool = False,
    ) -> None:
        """Bind a Polars DataFrame for queries within this transaction.

        ``rechunk`` follows :meth:`Velr.bind_polars`. Requires Polars/PyArrow and
        returns ``None``. Conversion errors propagate; binding failures or a finished
        transaction raise ``VelrError``.
        """
        tbl = _polars_to_arrow_table(df, rechunk=rechunk)
        self.bind_arrow(logical, tbl)

    def bind_numpy(
        self,
        logical: str,
        data,
        *,
        names: list[str] | None = None,
        types: dict | None = None,
    ) -> None:
        """Bind NumPy columns for queries within this transaction.

        Input shapes, ``names``, and optional PyArrow ``types`` follow
        :meth:`Velr.bind_numpy`. Requires NumPy/PyArrow and returns ``None``. Shape
        validation and binding failures raise ``VelrError``; conversion errors
        propagate.
        """
        tbl = _numpy_to_arrow_table(data, names=names, types=types)
        self.bind_arrow(logical, tbl)

    def bind_records(
        self,
        logical: str,
        rows: list[dict],
        *,
        types: dict | None = None,
    ) -> None:
        """Bind a list of dict records within the transaction.

        Use this for JSON-compatible Python records. Use ``bind_arrow()`` when
        the caller intentionally wants Arrow-backed columnar binding. Passing
        ``types=...`` builds an Arrow table from the records for explicit type
        control.

        Input and optional-dependency rules follow :meth:`Velr.bind_records`. Returns
        ``None``. Invalid records raise Python validation exceptions; database errors
        or a finished transaction raise ``VelrError``.
        """
        self._ensure_open()
        if types is None and self._call_native(_bind_records_scalar, self._tx, logical, rows, tx=True):
            return
        tbl = _records_to_arrow_table(rows, types=types)
        self.bind_arrow(logical, tbl)

    def __enter__(self) -> "VelrTx":
        """Return this handle for use in a ``with`` block.
        """
        self._check_thread()
        return self

    def __exit__(self, exc_type, exc, tb):
        """Roll back an open transaction on context-manager exit, then close it.

        This never commits automatically. It does not suppress an exception from
        the block; rollback/close failures can replace that exception.
        """
        try:
            if self._tx != ffi.NULL and not self._closed:
                self.rollback()
        finally:
            self.close()
