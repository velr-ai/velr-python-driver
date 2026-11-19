import pytest
from velr.driver import Velr, VelrError


FAILING_WRITE = "UNWIND [2, 1] AS i CREATE (:Attempt) RETURN 10 / (i - 1) AS quotient"
ATTEMPT_COUNT = "MATCH (n:Attempt) RETURN count(n)"


@pytest.mark.parametrize("method", ["to_records", "exec_one"])
def test_failed_write_return_does_not_persist_through_python_connection(method):
    with Velr.open() as db:
        with pytest.raises(VelrError, match="DivisionByZero"):
            getattr(db, method)(FAILING_WRITE)
        assert db.to_rows(ATTEMPT_COUNT) == [[0]]


def test_failed_write_return_rolls_back_only_its_statement_in_python_transaction():
    with Velr.open() as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:Keep {i: 1})")
            with pytest.raises(VelrError, match="DivisionByZero"):
                tx.exec_one(FAILING_WRITE)
            with tx.exec_one(ATTEMPT_COUNT) as table:
                assert table.to_rows() == [[0]]
            tx.run("CREATE (:Keep {i: 2})")
            tx.commit()

        assert db.to_rows(ATTEMPT_COUNT) == [[0]]
        assert db.to_rows("MATCH (n:Keep) RETURN n.i ORDER BY n.i") == [[1], [2]]


def test_stream_then_tx_then_sp_raii():
    with Velr.open(None) as db:
        # parity with your example: iterate tables then start tx
        with db.exec("MATCH (t) RETURN t") as st:
            while True:
                t = st.next_table()
                if t is None:
                    break
                t.close()

        # tx/raii: do nothing -> driver should rollback by default, no segfaults
        with db.begin_tx() as tx:
            sp = tx.savepoint()  # intentionally not released
            # no explicit commit/rollback; _children logic must close SP before TX

def test_savepoint_cm_release_on_success():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            with tx.savepoint():
                pass  # release

def test_savepoint_cm_rollback_on_error():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            with pytest.raises(RuntimeError):
                with tx.savepoint():
                    raise RuntimeError("boom")

def test_nested_savepoints_mixed_close():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            sp1 = tx.savepoint()
            sp2 = tx.savepoint()
            sp2.release()
            # drop sp1 without release; tx exit should close remaining children
        # default exit = rollback

def test_explicit_commit_overrides_default_rollback():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            with tx.savepoint():
                pass
            # do a write to verify persistence
            with tx.exec("CREATE (:T {k:'v'})") as st:
                # drain
                while True:
                    t = st.next_table()
                    if t is None: break
                    t.close()
            tx.commit()  # should persist

        # verify persisted
        with db.exec_one("MATCH (t:T) RETURN count(t) AS c") as table:
            with table.iter_rows() as rows:
                c = next(rows)[0].as_python()
                assert c == 1
