from threading import Timer
from time import sleep

import pytest

from velr.driver import CancellationToken, Velr, VelrError


@pytest.fixture
def db():
    with Velr.open(None) as connection:
        connection.run("UNWIND range(1, 220) AS i CREATE (:StopFixture {i: i})")
        yield connection


SLOW_QUERY = (
    "MATCH (a:StopFixture), (b:StopFixture), (c:StopFixture) "
    "RETURN sum(a.i + b.i + c.i) AS total"
)


def test_query_timeout_during_execution_and_fetching(db):
    with pytest.raises(VelrError) as executing:
        db.run(SLOW_QUERY, timeout=0.02)
    assert executing.value.code == "query.deadline_exceeded"

    with db.exec_one("UNWIND range(1, 3) AS i RETURN i", timeout=0.5) as table:
        with table.iter_rows() as rows:
            assert next(rows)[0].as_python() == 1
            sleep(0.55)
            with pytest.raises(VelrError) as fetching:
                next(rows)
            assert fetching.value.code == "query.deadline_exceeded"

    with db.exec_one("RETURN 1 AS ready") as table:
        assert table.to_records() == [{"ready": 1}]


def test_query_cancellation_during_execution_and_transaction_fetch(db):
    stop = CancellationToken()
    timer = Timer(0.025, stop.cancel)
    timer.start()
    try:
        with pytest.raises(VelrError) as executing:
            db.run(SLOW_QUERY, timeout=3, cancellation=stop)
        assert executing.value.code == "query.cancelled"
    finally:
        timer.cancel()
        timer.join()

    with db.begin_tx() as tx:
        stop_fetch = CancellationToken()
        with tx.exec_one("UNWIND range(1, 3) AS i RETURN i", cancellation=stop_fetch) as table:
            with table.iter_rows() as rows:
                assert next(rows)[0].as_python() == 1
                stop_fetch.cancel()
                with pytest.raises(VelrError) as fetching:
                    next(rows)
                assert fetching.value.code == "query.cancelled"

        with tx.exec_one("RETURN 2 AS ready") as table:
            assert table.to_records() == [{"ready": 2}]


def test_cancellation_between_tables_and_before_a_new_query(db):
    stop = CancellationToken()
    with db.exec("RETURN 1 AS n; RETURN 2 AS n", cancellation=stop) as stream:
        with stream.next_table() as first:
            assert first.to_records() == [{"n": 1}]
        stop.cancel()
        with pytest.raises(VelrError) as fetching:
            stream.next_table()
        assert fetching.value.code == "query.cancelled"

    with pytest.raises(VelrError) as pre_cancelled:
        db.execute("RETURN 3 AS n", cancellation=stop)
    assert pre_cancelled.value.code == "query.cancelled"

    with pytest.raises(VelrError) as expired:
        db.execute("RETURN 3 AS n", timeout=0)
    assert expired.value.code == "query.deadline_exceeded"
