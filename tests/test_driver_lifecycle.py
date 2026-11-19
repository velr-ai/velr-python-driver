# tests/test_driver_lifecycle.py

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gc
import subprocess
import sys
import textwrap
import threading
import weakref

import pytest

from velr import driver
from velr.driver import Velr, VelrError


ONE_ROW = "RETURN 1 AS x"
ONE_ROW_ALT = "RETURN 2 AS y"


def _read_scalar(table):
    with table.iter_rows() as rows:
        row = next(rows)
    assert len(row) == 1
    return row[0].as_python()


def _assert_table_open_and_scalar(table, expected):
    assert _read_scalar(table) == expected


def _assert_table_closed(table):
    with pytest.raises(VelrError, match="table already closed"):
        table.column_count()


def _assert_trace_closed(trace):
    with pytest.raises(VelrError, match="explain trace is closed"):
        trace.plan_count()


def _guard_native_thread(monkeypatch):
    native_lib = driver.lib
    owner = threading.current_thread()

    class OwnerOnlyLibrary:
        def __getattr__(self, name):
            assert threading.current_thread() is owner, name
            return getattr(native_lib, name)

    monkeypatch.setattr(driver, "lib", OwnerOnlyLibrary())


def _record_connection_closes(monkeypatch):
    native_lib = driver.lib
    closed_by = []

    class ObservedLibrary:
        def velr_close(self, db):
            closed_by.append(threading.current_thread())
            native_lib.velr_close(db)

        def __getattr__(self, name):
            return getattr(native_lib, name)

    monkeypatch.setattr(driver, "lib", ObservedLibrary())
    return closed_by


def test_exec_table_is_stream_scoped_and_closed_with_stream():
    with Velr.open(None) as db:
        st = db.exec(ONE_ROW)
        t = st.next_table()
        assert t is not None

        _assert_table_open_and_scalar(t, 1)

        st.close()
        _assert_table_closed(t)


def test_exec_stream_close_is_idempotent():
    with Velr.open(None) as db:
        st = db.exec(ONE_ROW)
        t = st.next_table()
        assert t is not None

        st.close()
        st.close()

        _assert_table_closed(t)


def test_exec_one_table_survives_method_return():
    with Velr.open(None) as db:
        t = db.exec_one(ONE_ROW)
        try:
            _assert_table_open_and_scalar(t, 1)
        finally:
            t.close()


def test_exec_one_table_is_closed_when_connection_closes():
    db = Velr.open(None)
    t = db.exec_one(ONE_ROW)

    _assert_table_open_and_scalar(t, 1)

    db.close()
    _assert_table_closed(t)


def test_exec_one_table_does_not_stay_alive_just_because_connection_is_alive():
    with Velr.open(None) as db:
        t = db.exec_one(ONE_ROW)
        wr = weakref.ref(t)

        del t
        gc.collect()

        assert wr() is None


def test_exec_stream_table_does_not_stay_alive_just_because_stream_is_alive():
    with Velr.open(None) as db:
        st = db.exec(ONE_ROW)
        t = st.next_table()
        assert t is not None

        wr = weakref.ref(t)
        del t
        gc.collect()

        assert wr() is None

        st.close()


def test_connection_close_closes_open_streams_and_their_tables():
    db = Velr.open(None)
    st = db.exec(ONE_ROW)
    t = st.next_table()
    assert t is not None

    _assert_table_open_and_scalar(t, 1)

    db.close()

    _assert_table_closed(t)
    with pytest.raises(VelrError, match="connection already closed"):
        st.next_table()


def test_exhausted_stream_returns_none_while_connection_is_open():
    with Velr.open(None) as db, db.exec(ONE_ROW) as st:
        assert st.next_table() is not None
        assert st.next_table() is None
        assert st.next_table() is None


def test_table_close_closes_open_rows():
    with Velr.open(None) as db:
        t = db.exec_one(ONE_ROW)
        rows = t.iter_rows()

        row = next(rows)
        assert row[0].as_python() == 1

        t.close()

        with pytest.raises(StopIteration):
            next(rows)


def test_rows_error_path_closes_iterator():
    with Velr.open(None) as db:
        t = db.exec_one(ONE_ROW)
        rows = t.iter_rows()
        rows.close()

        with pytest.raises(StopIteration):
            next(rows)

        t.close()


def test_tx_exec_table_is_stream_scoped_and_closed_with_stream():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            st = tx.exec(ONE_ROW)
            t = st.next_table()
            assert t is not None

            _assert_table_open_and_scalar(t, 1)

            st.close()
            _assert_table_closed(t)


def test_tx_exec_one_table_survives_method_return():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            t = tx.exec_one(ONE_ROW)
            try:
                _assert_table_open_and_scalar(t, 1)
            finally:
                t.close()


def test_tx_exec_one_table_is_closed_when_transaction_closes():
    with Velr.open(None) as db:
        tx = db.begin_tx()
        t = tx.exec_one(ONE_ROW)

        _assert_table_open_and_scalar(t, 1)

        tx.close()
        _assert_table_closed(t)


def test_tx_exec_one_table_is_closed_on_commit():
    with Velr.open(None) as db:
        tx = db.begin_tx()
        t = tx.exec_one(ONE_ROW)

        _assert_table_open_and_scalar(t, 1)

        tx.commit()
        _assert_table_closed(t)


def test_tx_exec_one_table_is_closed_on_rollback():
    with Velr.open(None) as db:
        tx = db.begin_tx()
        t = tx.exec_one(ONE_ROW)

        _assert_table_open_and_scalar(t, 1)

        tx.rollback()
        _assert_table_closed(t)


def test_tx_exec_one_table_does_not_stay_alive_just_because_transaction_is_alive():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            t = tx.exec_one(ONE_ROW)
            wr = weakref.ref(t)

            del t
            gc.collect()

            assert wr() is None


def test_tx_close_closes_open_streams_and_their_tables():
    with Velr.open(None) as db:
        tx = db.begin_tx()
        st = tx.exec(ONE_ROW)
        t = st.next_table()
        assert t is not None

        _assert_table_open_and_scalar(t, 1)

        tx.close()

        _assert_table_closed(t)
        with pytest.raises(VelrError, match="tx already closed"):
            st.next_table()


def test_idle_connection_can_move_between_threads_and_close_on_receiver(monkeypatch):
    closed_by = _record_connection_closes(monkeypatch)

    with Velr.open() as db, ThreadPoolExecutor(1) as worker:
        db.run("CREATE (:Handoff {value: 1})")
        assert worker.submit(db.to_rows, "MATCH (n:Handoff) RETURN n.value").result(
            timeout=5
        ) == [[1]]
        db.run("MATCH (n:Handoff) SET n.value = 2")
        assert db.to_rows("MATCH (n:Handoff) RETURN n.value") == [[2]]

        receiver = worker.submit(threading.current_thread).result(timeout=5)
        assert closed_by == []
        worker.submit(db.close).result(timeout=5)
        assert closed_by == [receiver]
        with pytest.raises(VelrError, match="closed"):
            db.to_rows(ONE_ROW)

    assert closed_by == [receiver]


def test_idle_connection_survives_opener_and_receiver_thread_exit(monkeypatch):
    closed_by = _record_connection_closes(monkeypatch)

    with ThreadPoolExecutor(1) as opener:
        db = opener.submit(Velr.open).result(timeout=5)

    assert closed_by == []
    with db:
        with ThreadPoolExecutor(1) as receiver:
            receiver.submit(db.run, "CREATE (:Handoff {value: 3})").result(timeout=5)
        assert closed_by == []
        assert db.to_rows("MATCH (n:Handoff) RETURN n.value") == [[3]]

    assert closed_by == [threading.current_thread()]


def test_idle_connection_last_reference_can_be_dropped_on_another_thread(monkeypatch):
    closed_by = _record_connection_closes(monkeypatch)
    connections = [Velr.open()]
    connections[0].run(ONE_ROW)
    connection = weakref.ref(connections[0])

    with ThreadPoolExecutor(1) as worker:
        receiver = worker.submit(threading.current_thread).result(timeout=5)
        worker.submit(connections.clear).result(timeout=5)
        assert connection() is None
        assert closed_by == [receiver]

    assert closed_by == [receiver]


@pytest.mark.parametrize(
    "factory, child_use",
    [("exec", "next_table"), ("exec_one", "to_rows"), ("explain", "plan_count")],
)
def test_live_direct_result_blocks_connection_handoff(monkeypatch, factory, child_use):
    with Velr.open() as db, ThreadPoolExecutor(1) as worker:
        with getattr(db, factory)(ONE_ROW) as child, monkeypatch.context() as patch:
            _guard_native_thread(patch)
            calls = (
                (db.run, "CREATE (:ForeignWrite)"),
                (db.close,),
                (getattr(child, child_use),),
                (child.close,),
            )
            for fn, *args in calls:
                with pytest.raises(VelrError, match="thread"):
                    worker.submit(fn, *args).result(timeout=5)

            assert getattr(child, child_use)() is not None

        assert worker.submit(
            db.to_rows, "MATCH (n:ForeignWrite) RETURN count(n)"
        ).result(timeout=5) == [[0]]


def test_in_flight_run_rejects_foreign_query_and_close_without_live_children(monkeypatch):
    native_lib = driver.lib
    entered = threading.Event()
    release = threading.Event()

    class BlockingLibrary:
        def velr_run(self, db, cypher, error):
            assert cypher == ONE_ROW.encode("utf-8"), "concurrent query reached native code"
            entered.set()
            assert release.wait(timeout=5), "native run was not released"
            return native_lib.velr_run(db, cypher, error)

        def velr_close(self, db):
            assert release.is_set(), "concurrent close reached native code"
            native_lib.velr_close(db)

        def __getattr__(self, name):
            return getattr(native_lib, name)

    with Velr.open() as db, ThreadPoolExecutor(1) as worker:
        monkeypatch.setattr(driver, "lib", BlockingLibrary())
        running = worker.submit(db.run, ONE_ROW)
        try:
            assert entered.wait(timeout=5), "worker did not reach native run"
            with pytest.raises(VelrError, match="in use|thread"):
                db.run("CREATE (:ForeignWrite)")
            with pytest.raises(VelrError, match="in use|thread"):
                db.close()
        finally:
            release.set()
        running.result(timeout=5)

        assert db.to_rows("MATCH (n:ForeignWrite) RETURN count(n)") == [[0]]


def test_stream_native_callback_cannot_run_query_or_close_connection(monkeypatch):
    native_lib = driver.lib
    inside_callback = False

    with Velr.open() as db:
        with db.exec(ONE_ROW) as stream:
            stream_close = type(stream).close

            def close_outside_callback(handle):
                assert not inside_callback, "callback attempted to close the live stream"
                stream_close(handle)

            class CallbackLibrary:
                def velr_stream_next_table(self, *args):
                    nonlocal inside_callback
                    assert not inside_callback, "nested stream read reached native code"
                    inside_callback = True
                    try:
                        with pytest.raises(VelrError, match="in use"):
                            db.run("CREATE (:Reentered)")
                        with pytest.raises(VelrError, match="in use"):
                            db.close()
                    finally:
                        inside_callback = False
                    return native_lib.velr_stream_next_table(*args)

                def __getattr__(self, name):
                    assert not inside_callback, f"callback reached native call {name}"
                    return getattr(native_lib, name)

            with monkeypatch.context() as patch:
                patch.setattr(driver, "lib", CallbackLibrary())
                # Keep the real stream intact even if an unguarded close walks its children.
                patch.setattr(type(stream), "close", close_outside_callback)
                with stream.next_table() as table:
                    assert table.to_rows() == [[1]]

        assert db.to_rows("MATCH (n:Reentered) RETURN count(n)") == [[0]]


def test_cross_thread_handles_are_rejected_before_entering_native_runtime(monkeypatch):
    with Velr.open(None) as db, db.begin_tx() as tx, ThreadPoolExecutor(1) as worker:
        with tx.exec(ONE_ROW) as stream, tx.explain(ONE_ROW) as trace, tx.savepoint() as sp:
            with stream.next_table() as table, table.iter_rows() as rows:
                # Guard the native boundary so this test is safe against older drivers.
                _guard_native_thread(monkeypatch)
                calls = (
                    (db.run, "CREATE (:Unsafe)"), (db.close,), (tx.run, ONE_ROW),
                    (tx.close,), (stream.next_table,), (stream.close,), (table.column_count,),
                    (table.to_rows,), (table.to_records,), (table.close,), (next, rows),
                    (rows.close,), (trace.plan_count,),
                    (trace.close,), (sp.rollback,), (sp.close,),
                )
                for fn, *args in calls:
                    with pytest.raises(VelrError, match="thread"):
                        worker.submit(fn, *args).result(timeout=5)

                assert next(rows)[0].as_python() == 1
                assert trace.plan_count() >= 1


def test_concurrent_foreign_access_leaves_active_cursor_and_database_untouched():
    script = textwrap.dedent(
        """
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        from velr.driver import Velr, VelrError

        with Velr.open() as db:
            barrier = Barrier(8)
            with db.exec_one('UNWIND range(1, 64) AS i RETURN i ORDER BY i') as table:
                with table.iter_rows() as rows, ThreadPoolExecutor(8) as pool:
                    assert next(rows)[0].as_python() == 1

                    def try_foreign_access():
                        barrier.wait(timeout=5)
                        for call, args in (
                            (db.run, ('CREATE (:ForeignWrite)',)),
                            (db.close, ()),
                            (next, (rows,)),
                        ):
                            try:
                                call(*args)
                            except VelrError as error:
                                assert 'thread' in str(error)
                            else:
                                raise AssertionError(f'{call} accepted a foreign thread')

                    futures = [pool.submit(try_foreign_access) for _ in range(8)]
                    for future in futures:
                        future.result(timeout=5)
                    assert [row[0].as_python() for row in rows] == list(range(2, 65))

            assert db.to_rows('MATCH (n:ForeignWrite) RETURN count(n)') == [[0]]
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_foreign_thread_finalizer_defers_native_close_to_owner(monkeypatch):
    native_lib = driver.lib
    owner = threading.current_thread()
    closed_by = []

    class ObservedLibrary:
        def velr_table_close(self, table):
            current = threading.current_thread()
            closed_by.append(current)
            assert current is owner
            native_lib.velr_table_close(table)

        def __getattr__(self, name):
            return getattr(native_lib, name)

    with Velr.open(None) as db, ThreadPoolExecutor(1) as worker:
        tables = [db.exec_one(ONE_ROW)]
        monkeypatch.setattr(driver, "lib", ObservedLibrary())

        def drop_table():
            tables.pop()

        worker.submit(drop_table).result(timeout=5)
        assert closed_by == []

        db.close()
        assert closed_by == [owner]


def test_owner_thread_finalizer_defers_table_close_during_native_run(monkeypatch):
    native_lib = driver.lib
    inside_run = False
    premature = []
    closed = []

    class ObservedLibrary:
        def velr_run(self, db, cypher, error):
            nonlocal inside_run
            if cypher == ONE_ROW.encode("utf-8"):
                inside_run = True
                try:
                    tables.clear()
                finally:
                    inside_run = False
            return native_lib.velr_run(db, cypher, error)

        def velr_table_close(self, table):
            if inside_run:
                premature.append(table)
                return
            closed.append(table)
            native_lib.velr_table_close(table)

        def __getattr__(self, name):
            return getattr(native_lib, name)

    with Velr.open() as db, monkeypatch.context() as patch:
        tables = [db.exec_one(ONE_ROW)]
        patch.setattr(driver, "lib", ObservedLibrary())
        try:
            db.run(ONE_ROW)
            assert premature == []
            assert closed == []

            db.run(ONE_ROW_ALT)
            assert len(closed) == 1
        finally:
            # An unguarded finalizer must never free the real table inside the mock.
            for table in premature:
                native_lib.velr_table_close(table)


def test_thread_exit_closes_handles_that_outlive_the_owner(monkeypatch):
    native_lib = driver.lib
    closed_by = []
    owners = []

    class ObservedLibrary:
        def velr_close(self, db):
            closed_by.append(threading.get_ident())
            assert closed_by[-1] == owners[0]
            native_lib.velr_close(db)

        def __getattr__(self, name):
            assert threading.get_ident() == owners[0], name
            return getattr(native_lib, name)

    def open_database():
        owners.append(threading.get_ident())
        db = Velr.open(None)
        table = db.exec_one(ONE_ROW)
        return db, table

    monkeypatch.setattr(driver, "lib", ObservedLibrary())
    with ThreadPoolExecutor(1) as worker:
        db, table = worker.submit(open_database).result(timeout=5)
        assert closed_by == []

    assert closed_by == owners
    with pytest.raises(VelrError, match="thread"):
        table.column_count()
    with pytest.raises(VelrError, match="closed"):
        db.to_rows(ONE_ROW)
    db.close()
    assert closed_by == owners


def test_owner_exit_snapshot_cannot_close_connection_after_idle_handoff(monkeypatch):
    closed_by = _record_connection_closes(monkeypatch)
    close_from_owner = Velr._close_from_owner
    handoffs = []
    query = "MATCH (n:Handoff) RETURN n.value"
    db = table = None

    def open_database():
        connection = Velr.open()
        connection.run("CREATE (:Handoff {value: 4})")
        return connection, connection.exec_one(ONE_ROW), threading.get_ident()

    with ThreadPoolExecutor(1) as receiver:
        def close_after_handoff(connection, owner, *, only_with_children=False):
            if connection is not db or not only_with_children:
                return close_from_owner(
                    connection, owner, only_with_children=only_with_children
                )
            table.close()
            result = receiver.submit(connection.to_rows, query).result(timeout=5)
            close_from_owner(connection, owner, only_with_children=only_with_children)
            handoffs.append((threading.get_ident(), result))

        monkeypatch.setattr(Velr, "_close_from_owner", close_after_handoff)
        with ThreadPoolExecutor(1) as opener:
            db, table, opener_ident = opener.submit(open_database).result(timeout=5)

        assert handoffs == [(opener_ident, [[4]])]
        assert closed_by == []

    with db:
        assert db.to_rows(query) == [[4]]

    assert closed_by == [threading.current_thread()]


def test_explain_trace_survives_method_return():
    with Velr.open(None) as db:
        xp = db.explain(ONE_ROW)
        try:
            assert xp.plan_count() >= 1
        finally:
            xp.close()


def test_explain_trace_is_closed_when_connection_closes():
    db = Velr.open(None)
    xp = db.explain(ONE_ROW)

    assert xp.plan_count() >= 1

    db.close()
    _assert_trace_closed(xp)


def test_tx_explain_trace_is_closed_when_transaction_closes():
    with Velr.open(None) as db:
        tx = db.begin_tx()
        xp = tx.explain(ONE_ROW)

        assert xp.plan_count() >= 1

        tx.close()
        _assert_trace_closed(xp)


def test_multiple_close_calls_are_harmless_for_table_and_trace():
    with Velr.open(None) as db:
        t = db.exec_one(ONE_ROW)
        t.close()
        t.close()
        _assert_table_closed(t)

        xp = db.explain(ONE_ROW_ALT)
        xp.close()
        xp.close()
        _assert_trace_closed(xp)
