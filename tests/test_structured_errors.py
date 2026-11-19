"""Typed error contracts, native metadata ownership, and deferred failures."""

from concurrent.futures import ThreadPoolExecutor
import json
import errno
import pickle
import sqlite3
import subprocess
import sys

import pytest

import velr.driver as driver
from velr.driver import (
    ConstraintError,
    CypherArithmeticError,
    CypherSemanticError,
    CypherSyntaxError,
    CypherTypeError,
    DatabaseBusyError,
    DeadlineExceeded,
    EntityNotFoundError,
    HandleStateError,
    MigrationRequiredError,
    ParameterError,
    ProcedureNotFoundError,
    QueryCancelled,
    QueryError,
    ReadOnlyError,
    Velr,
    VelrError,
)


@pytest.mark.parametrize("args", [(), ("diagnostic",), ("message", 7)])
def test_base_error_preserves_runtime_error_args_and_metadata_through_pickle(args):
    error = VelrError(*args, category="FutureCategory", detail="FutureDetail")
    assert error.args == args
    assert str(error) == str(RuntimeError(*args))
    assert error.message == str(error)
    assert error.code is None
    assert error.status_code is None
    assert error.phase is None
    assert error.position is None
    assert error.hint is None
    assert error.retryable is False
    assert error.sqlite_errorcode is None
    assert error.sqlite_errorname is None
    assert error.io_kind is None
    assert error.os_code is None
    assert error.operation is None
    assert error.path is None
    restored = pickle.loads(pickle.dumps(error))
    assert restored.args == error.args
    assert restored.__dict__ == error.__dict__


class _ErrorLibrary:
    def __init__(self, native_lib, details):
        self.native_lib = native_lib
        self.freed = []
        self.payload = driver.ffi.new("char[]", details)
        self.reads = 0

    def __getattr__(self, name):
        return getattr(self.native_lib, name)

    def velr_error_details_json(self, pointer):
        assert pointer not in self.freed, "metadata was read after its error was freed"
        self.reads += 1
        return self.payload

    def velr_string_free(self, pointer):
        self.freed.append(pointer)
        self.payload[0] = b"!"


def _check_payload(monkeypatch, details, *, status=-4, diagnostic="original diagnostic"):
    payload = json.dumps(details).encode() if isinstance(details, dict) else details
    library = _ErrorLibrary(driver.lib, payload)
    error_string = driver.ffi.new("char[]", diagnostic.encode())
    error_pointer = driver.ffi.new("char **", error_string)
    with monkeypatch.context() as patch:
        patch.setattr(driver, "lib", library)
        with pytest.raises(VelrError) as raised:
            driver._check(status, error_pointer)
    assert library.reads == 1
    assert library.freed == [library.payload, error_string]
    assert error_pointer[0] == driver.ffi.NULL
    assert str(raised.value) == diagnostic
    assert raised.value.args == (diagnostic,)
    assert raised.value.status_code == status
    return raised.value


@pytest.mark.parametrize(
    "category,detail,expected",
    [
        ("SyntaxError", "UnknownFunction", CypherSyntaxError),
        ("SemanticError", "InvalidAggregation", CypherSemanticError),
        ("TypeError", "InvalidArgumentType", CypherTypeError),
        ("ArgumentError", "InvalidArgumentValue", ParameterError),
        ("ParameterMissing", "MissingParameter", ParameterError),
        ("ProcedureNotFound", "UnknownProcedure", ProcedureNotFoundError),
        ("ProcedureError", "ProcedureNotFound", ProcedureNotFoundError),
        ("ProcedureError", "FutureProcedureReason", QueryError),
        ("EntityNotFound", "DeletedEntity", EntityNotFoundError),
        ("ConstraintVerificationFailed", "DeleteConnectedNode", ConstraintError),
        ("ArithmeticError", "DivisionByZero", CypherArithmeticError),
        ("DatabaseBusy", "Busy", DatabaseBusyError),
        ("ReadOnly", "ReadOnlyDatabase", ReadOnlyError),
        ("ReadOnlyError", "ReadOnlyConnection", ReadOnlyError),
        ("MigrationRequired", "DatabaseMigrationRequired", MigrationRequiredError),
        ("DatabaseStateError", "MigrationRequired", MigrationRequiredError),
        ("HandleState", "ClosedHandle", HandleStateError),
    ],
)
def test_native_categories_select_types_without_changing_diagnostics(
    monkeypatch, category, detail, expected
):
    error = _check_payload(
        monkeypatch,
        {
            "version": 1,
            "category": category,
            "detail": detail,
            "phase": "runtime",
            "message": "A short explanation.",
            "hint": "A remediation.",
            "position": {"line": 2, "column": 13},
            "retryable": False,
        },
        diagnostic="Unrelated prose that cannot identify an error category.",
    )
    assert type(error) is expected
    assert error.category == category
    assert error.detail == detail
    assert error.phase == "runtime"
    assert error.message == "A short explanation."
    assert error.hint == "A remediation."
    assert error.position == (2, 13)
    assert error.code is None


def test_metadata_unknown_fields_and_categories_remain_forward_compatible(monkeypatch):
    error = _check_payload(
        monkeypatch,
        {
            "version": 1,
            "category": "FutureCategory",
            "detail": "FutureDetail",
            "extra": {"new": "field"},
            "sqlite_errorcode": 517,
            "sqlite_errorname": "SQLITE_BUSY_SNAPSHOT",
            "retryable": True,
        },
    )
    assert type(error) is VelrError
    assert error.category == "FutureCategory"
    assert error.detail == "FutureDetail"
    assert error.sqlite_errorcode == 517
    assert error.sqlite_errorname == "SQLITE_BUSY_SNAPSHOT"
    assert error.retryable is True
    assert error.message == str(error)


def test_upstream_source_coordinates_and_nonquery_argument_errors(monkeypatch):
    error = _check_payload(
        monkeypatch,
        {
            "version": 1,
            "kind": "argument",
            "category": "ArgumentError",
            "detail": "InvalidCreationOptions",
            "line": 3,
            "column": 7,
        },
    )
    assert type(error) is VelrError
    assert error.category == "ArgumentError"
    assert error.position == (3, 7)


def test_local_filesystem_identity_is_preserved_in_metadata_and_cause(monkeypatch):
    error = _check_payload(
        monkeypatch,
        {
            "version": 1,
            "kind": "io",
            "category": "IOError",
            "detail": "DatabaseOpenFailed",
            "message": "cannot open database",
            "io_kind": "not_found",
            "os_code": errno.ENOENT,
            "operation": "open",
            "path": "/missing/database.db",
        },
    )
    assert error.io_kind == "not_found"
    assert error.os_code == errno.ENOENT
    assert error.operation == "open"
    assert error.path == "/missing/database.db"
    assert isinstance(error.__cause__, FileNotFoundError)
    assert error.__cause__.filename == error.path


@pytest.mark.parametrize(
    "payload",
    [b"not JSON", b"\xff", b"[]", b'{"version":2}', b'{"version":true}', b'{"category":"SyntaxError"}'],
)
def test_unavailable_or_incompatible_metadata_never_guesses_from_text(monkeypatch, payload):
    error = _check_payload(
        monkeypatch,
        payload,
        diagnostic="SyntaxError at compile time: UnknownFunction\nnot metadata",
    )
    assert type(error) is VelrError
    assert error.category is None
    assert error.detail is None
    assert error.retryable is False


def test_invalid_optional_metadata_does_not_fabricate_a_position(monkeypatch):
    error = _check_payload(
        monkeypatch,
        {
            "version": 1,
            "position": {"line": True, "column": 0},
            "phase": "future phase",
            "message": [],
            "hint": {},
            "sqlite_errorcode": True,
            "retryable": "true",
        },
    )
    assert error.position is None
    assert error.phase is None
    assert error.hint is None
    assert error.sqlite_errorcode is None
    assert error.retryable is False


@pytest.mark.parametrize(
    "status,expected,code",
    [(-1, VelrError, None), (-2, VelrError, None), (-3, HandleStateError, None),
     (-4, VelrError, None), (-5, QueryCancelled, "query.cancelled"),
     (-6, DeadlineExceeded, "query.deadline_exceeded")],
)
def test_older_runtime_keeps_native_status_and_stop_compatibility(
    monkeypatch, status, expected, code
):
    native_lib = driver.lib
    freed = []

    class OlderLibrary:
        def __getattr__(self, name):
            if name == "velr_error_details_json":
                raise AttributeError(name)
            return getattr(native_lib, name)

        def velr_string_free(self, pointer):
            freed.append(pointer)

    error_string = driver.ffi.new("char[]", b"unchanged native diagnostic")
    error_pointer = driver.ffi.new("char **", error_string)
    monkeypatch.setattr(driver, "lib", OlderLibrary())
    with pytest.raises(expected) as raised:
        driver._check(status, error_pointer)
    error = raised.value
    assert type(error) is expected
    assert error.code == code
    assert error.status_code == status
    assert error.args == ("unchanged native diagnostic",)
    assert freed == [error_string]
    assert error_pointer[0] == driver.ffi.NULL
    if status == -6:
        assert isinstance(error, TimeoutError)


def test_closed_and_wrong_thread_handles_have_explicit_local_state(monkeypatch):
    with Velr.open() as db:
        with db.exec_one("RETURN 1") as table:
            with ThreadPoolExecutor(1) as worker:
                with pytest.raises(HandleStateError) as raised:
                    worker.submit(table.to_rows).result(timeout=5)
            assert raised.value.detail == "WrongThread"
            assert raised.value.status_code is None
        with pytest.raises(HandleStateError, match="table already closed") as raised:
            table.column_count()
        assert raised.value.category == "HandleState"
        assert raised.value.detail == "ClosedTable"
        assert raised.value.status_code is None
        db._native_busy = True
        try:
            with pytest.raises(HandleStateError) as raised:
                db.run("RETURN 1")
            assert raised.value.detail == "ConnectionInUse"
            assert raised.value.retryable is False
        finally:
            db._native_busy = False
    with pytest.raises(HandleStateError) as raised:
        db.run("RETURN 1")
    assert raised.value.detail == "ClosedConnection"


def test_python_parameter_validation_retains_standard_exceptions():
    with Velr.open() as db:
        with pytest.raises(TypeError):
            db.run("RETURN $value", params={"value": object()})
        with pytest.raises(ValueError):
            db.run("RETURN $value", params={"value": float("nan")})


def test_row_callback_exceptions_propagate_unchanged():
    cause = ValueError("application callback failure")

    def fail(_row):
        raise cause

    with Velr.open() as db, db.exec_one("RETURN 1") as table:
        with pytest.raises(ValueError) as raised:
            table.collect(fail)
        assert raised.value is cause
        assert table.column_count() == 1


def test_vector_callback_failure_retains_the_python_cause(tmp_path):
    cause = ValueError("embedding provider failed")

    def fail(_inputs):
        raise cause

    with Velr.open(str(tmp_path / "embedding.db")) as db:
        db.run("CREATE (:Document {text: 'an example'})")
        db.register_vector_embedder("failing", fail)
        with pytest.raises(VelrError) as raised:
            db.run(
                "CREATE VECTOR INDEX documentEmbedding FOR (n:Document) ON EACH [n.text] "
                "OPTIONS {indexConfig: {dimensions: 2, metric: 'cosine', embedder: 'failing'}}"
            )
        assert raised.value.__cause__ is cause
        assert db._callback_error is None
        assert not driver._callback_causes


def _require_structured_runtime():
    if not hasattr(driver.lib, "velr_error_details_json"):
        pytest.skip("the loaded runtime does not expose structured diagnostics")


@pytest.mark.parametrize(
    "query,params,expected,category,detail,phase",
    [
        ("RETURN toUppper('a')", None, CypherSyntaxError, "SyntaxError", "UnknownFunction", "compile"),
        ("RETURN $missing", None, ParameterError, "ParameterMissing", "MissingParameter", "runtime"),
        ("RETURN $value * 2", {"value": "a"}, CypherTypeError, "TypeError", "InvalidArgumentType", "runtime"),
        ("CALL noSuchProcedure()", None, ProcedureNotFoundError, "ProcedureNotFound", "UnknownProcedure", "compile"),
    ],
)
def test_query_failures_carry_typed_native_fields(query, params, expected, category, detail, phase):
    _require_structured_runtime()
    with Velr.open() as db, pytest.raises(expected) as raised:
        db.run(query, params=params)
    error = raised.value
    assert isinstance(error, QueryError)
    assert error.category == category
    assert error.detail == detail
    assert error.phase == phase
    assert error.status_code == -4
    assert error.message
    assert error.retryable is False


def test_connected_node_delete_preserves_constraint_identity():
    _require_structured_runtime()
    with Velr.open() as db:
        db.run("CREATE (:A)-[:R]->(:B)")
        with pytest.raises(ConstraintError) as raised:
            db.run("MATCH (n:A) DELETE n")
        assert raised.value.category == "ConstraintVerificationFailed"
        assert raised.value.detail == "DeleteConnectedNode"
        assert raised.value.phase == "runtime"
        assert db.to_rows("MATCH ()-[r:R]->() RETURN count(r)") == [[1]]


def test_deferred_fetch_error_keeps_metadata_after_cursor_cleanup():
    _require_structured_runtime()
    with Velr.open() as db:
        with db.exec_one("UNWIND [1, 0] AS divisor RETURN 1 / divisor") as table:
            with table.iter_rows() as rows:
                with pytest.raises(CypherArithmeticError) as raised:
                    list(rows)
        error = raised.value
        assert error.detail == "DivisionByZero"
        assert error.phase == "runtime"
        assert error.status_code == -4
        assert error.message
        assert db.to_rows("RETURN 1") == [[1]]


def test_source_positions_use_unicode_characters():
    _require_structured_runtime()
    query = "RETURN 'é', toUppper('a')"
    with Velr.open() as db, pytest.raises(CypherSyntaxError) as raised:
        db.run(query)
    assert raised.value.position == (1, query.index("toUppper") + 1)


def test_database_busy_and_readonly_are_not_query_or_internal_errors(tmp_path):
    _require_structured_runtime()
    path = tmp_path / "graph.db"
    with Velr.open(str(path)) as db:
        writer_program = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("BEGIN IMMEDIATE")
print("locked", flush=True)
sys.stdin.readline()
connection.execute("ROLLBACK")
connection.close()
"""
        with subprocess.Popen(
            [sys.executable, "-u", "-c", writer_program, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as writer:
            assert writer.stdout.readline() == "locked\n"
            try:
                with pytest.raises(DatabaseBusyError) as raised:
                    db.run("CREATE (:Contended)")
            finally:
                _, writer_errors = writer.communicate("\n", timeout=15)
                assert writer.returncode == 0, writer_errors
        error = raised.value
        assert not isinstance(error, QueryError)
        assert error.retryable is True
        assert error.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
        assert error.sqlite_errorname
        assert db.to_rows("MATCH (n:Contended) RETURN count(n)") == [[0]]
    with Velr.open_readonly(str(path)) as db, pytest.raises(ReadOnlyError) as raised:
        db.run("CREATE (:Forbidden)")
    assert raised.value.retryable is False
    assert not isinstance(raised.value, QueryError)
