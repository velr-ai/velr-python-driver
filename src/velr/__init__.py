"""Python package for the embedded Velr property-graph database.

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
"""

from importlib import import_module as _import_module
from typing import Any as _Any, TYPE_CHECKING as _TYPE_CHECKING

__all__ = [
    "Path", "ErrorPosition", "VelrError", "QueryError", "CypherSyntaxError",
    "CypherSemanticError", "CypherTypeError", "ParameterError",
    "ProcedureNotFoundError", "EntityNotFoundError", "ConstraintError",
    "CypherArithmeticError", "DatabaseBusyError", "ReadOnlyError",
    "MigrationRequiredError", "HandleStateError", "QueryCancelled",
    "DeadlineExceeded", "CancellationToken", "MigrationReport", "Cell",
    "VectorEmbeddingField", "VectorEmbeddingInput", "ExplainPlanMeta",
    "ExplainStepMeta", "ExplainStatementMeta", "ExplainStatement", "ExplainStep",
    "ExplainPlan", "ExplainTrace", "Extension", "Velr", "Stream", "StreamTx", "Table", "Rows",
    "Savepoint", "VelrTx",
]

if _TYPE_CHECKING:
    from .driver import (
        CancellationToken as CancellationToken,
        Cell as Cell,
        ConstraintError as ConstraintError,
        CypherArithmeticError as CypherArithmeticError,
        CypherSemanticError as CypherSemanticError,
        CypherSyntaxError as CypherSyntaxError,
        CypherTypeError as CypherTypeError,
        DatabaseBusyError as DatabaseBusyError,
        DeadlineExceeded as DeadlineExceeded,
        EntityNotFoundError as EntityNotFoundError,
        ErrorPosition as ErrorPosition,
        ExplainPlan as ExplainPlan,
        ExplainPlanMeta as ExplainPlanMeta,
        ExplainStatement as ExplainStatement,
        ExplainStatementMeta as ExplainStatementMeta,
        ExplainStep as ExplainStep,
        ExplainStepMeta as ExplainStepMeta,
        ExplainTrace as ExplainTrace,
        HandleStateError as HandleStateError,
        MigrationReport as MigrationReport,
        MigrationRequiredError as MigrationRequiredError,
        ParameterError as ParameterError,
        Path as Path,
        ProcedureNotFoundError as ProcedureNotFoundError,
        QueryCancelled as QueryCancelled,
        QueryError as QueryError,
        ReadOnlyError as ReadOnlyError,
        Rows as Rows,
        Savepoint as Savepoint,
        Stream as Stream,
        StreamTx as StreamTx,
        Table as Table,
        VectorEmbeddingField as VectorEmbeddingField,
        VectorEmbeddingInput as VectorEmbeddingInput,
        Velr as Velr,
        VelrError as VelrError,
        VelrTx as VelrTx,
    )
    from .extensions import Extension as Extension


def __getattr__(name: str) -> _Any:
    """Load a public driver export on first access and cache the same object.

    Unknown names raise ``AttributeError`` without importing the driver. Loading
    a known name retains the driver's native-runtime import requirements.
    """
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = ".extensions" if name == "Extension" else ".driver"
    value = getattr(_import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """List package attributes and public driver names without loading the driver."""
    return sorted(set(globals()) | set(__all__))
