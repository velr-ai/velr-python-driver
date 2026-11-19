# tests/test_python_driver_site_semantics.py

from __future__ import annotations

from velr.driver import Velr


def _collect_first_col(query: str) -> list:
    with Velr.open(None) as db:
        db.run(query)
        with db.exec_one("MATCH (n:T) RETURN n.k AS k ORDER BY k") as table:
            with table.iter_rows() as rows:
                return [row[0].as_python() for row in rows]


def test_exec_table_is_valid_while_stream_is_open():
    with Velr.open(None) as db:
        db.run("CREATE (:T {k:'a'})")

        with db.exec("MATCH (n:T) RETURN n.k AS k ORDER BY k") as stream:
            table = stream.next_table()
            assert table is not None

            with table.iter_rows() as rows:
                row = next(rows)

            assert row[0].as_python() == "a"


def test_exec_table_is_closed_when_stream_closes():
    with Velr.open(None) as db:
        db.run("CREATE (:T {k:'a'})")

        with db.exec("MATCH (n:T) RETURN n.k AS k ORDER BY k") as stream:
            table = stream.next_table()
            assert table is not None

        # Table came from exec(), so it should be closed with the stream.
        try:
            table.column_names()
        except Exception:
            pass
        else:
            raise AssertionError("expected table from exec() to be closed when stream closes")


def test_exec_one_table_survives_after_return():
    with Velr.open(None) as db:
        db.run("CREATE (:T {k:'a'})")

        table = db.exec_one("MATCH (n:T) RETURN n.k AS k ORDER BY k")
        try:
            # exec_one() should return a still-usable table.
            assert table.column_names() == ["k"]

            with table.iter_rows() as rows:
                row = next(rows)

            assert row[0].as_python() == "a"
        finally:
            table.close()


def test_transaction_without_commit_rolls_back():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:T {k:'rolled_back'})")
            # no commit

        with db.exec_one("MATCH (n:T) RETURN count(n) AS c") as table:
            with table.iter_rows() as rows:
                row = next(rows)

        assert row[0].as_python() == 0


def test_transaction_commit_persists():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:T {k:'committed'})")
            tx.commit()

        with db.exec_one("MATCH (n:T) RETURN n.k AS k") as table:
            with table.iter_rows() as rows:
                row = next(rows)

        assert row[0].as_python() == "committed"


def test_scoped_savepoint_rollback_semantics():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:T {k:'outer'})")

            with tx.savepoint() as sp:
                tx.run("CREATE (:T {k:'inner'})")
                sp.rollback()

            tx.commit()

        with db.exec_one("MATCH (n:T) RETURN n.k AS k ORDER BY k") as table:
            with table.iter_rows() as rows:
                values = [row[0].as_python() for row in rows]

        assert values == ["outer"]


def test_named_savepoint_rollback_semantics():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:T {k:'outer'})")

            tx.savepoint_named("sp1")
            tx.run("CREATE (:T {k:'inner'})")

            tx.rollback_to("sp1")
            tx.commit()

        with db.exec_one("MATCH (n:T) RETURN n.k AS k ORDER BY k") as table:
            with table.iter_rows() as rows:
                values = [row[0].as_python() for row in rows]

        assert values == ["outer"]


def test_named_savepoint_context_manager_release_on_success():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            with tx.savepoint_named("sp1"):
                tx.run("CREATE (:T {k:'kept'})")

            tx.commit()

        with db.exec_one("MATCH (n:T) RETURN n.k AS k ORDER BY k") as table:
            with table.iter_rows() as rows:
                values = [row[0].as_python() for row in rows]

        assert values == ["kept"]


def test_named_savepoint_context_manager_rollback_on_exception():
    with Velr.open(None) as db:
        try:
            with db.begin_tx() as tx:
                tx.run("CREATE (:T {k:'outer'})")

                with tx.savepoint_named("sp1"):
                    tx.run("CREATE (:T {k:'inner'})")
                    raise RuntimeError("boom")

                tx.commit()
        except RuntimeError:
            pass

        # The whole transaction context should roll back because commit() was never reached.
        with db.exec_one("MATCH (n:T) RETURN count(n) AS c") as table:
            with table.iter_rows() as rows:
                row = next(rows)

        assert row[0].as_python() == 0


def test_explain_snapshot_is_materializable():
    with Velr.open(None) as db:
        db.run("CREATE (:T {k:'a'})")

        with db.explain("MATCH (n:T) RETURN n.k AS k") as xp:
            snap = xp.snapshot()

        assert len(snap) >= 1


def test_explain_analyze_snapshot_is_materializable():
    with Velr.open(None) as db:
        db.run("CREATE (:T {k:'a'})")

        with db.explain_analyze("MATCH (n:T) RETURN n.k AS k") as xp:
            snap = xp.snapshot()

        assert len(snap) >= 1