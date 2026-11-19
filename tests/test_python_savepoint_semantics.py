import gc

import pytest

from velr.driver import Velr, VelrError


def _count_label(db: Velr, label: str) -> int:
    with db.exec_one(f"MATCH (n:{label}) RETURN count(n) AS c") as table:
        with table.iter_rows() as rows:
            row = next(rows)
    return row[0].as_python()


def _values(db: Velr, label: str, key: str = "k") -> list:
    with db.exec_one(
        f"MATCH (n:{label}) RETURN n.{key} AS v ORDER BY v"
    ) as table:
        return table.collect(lambda row: row[0].as_python())


def test_scoped_savepoint_rollback_discards_inner_changes():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:T {k:'outer'})")

            sp = tx.savepoint()
            tx.run("CREATE (:T {k:'inner'})")
            sp.rollback()

            tx.commit()

        assert _values(db, "T") == ["outer"]


def test_scoped_savepoint_release_keeps_inner_changes():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:T {k:'outer'})")

            sp = tx.savepoint()
            tx.run("CREATE (:T {k:'inner'})")
            sp.release()

            tx.commit()

        assert _values(db, "T") == ["inner", "outer"]


@pytest.mark.parametrize("named", [False, True])
def test_consumed_savepoint_cannot_be_rolled_back(named):
    with Velr.open(None) as db, db.begin_tx() as tx:
        sp = tx.savepoint_named("released") if named else tx.savepoint()
        with sp:
            tx.run("CREATE (:T {k:'retained'})")
            sp.release()
            with pytest.raises(VelrError, match="savepoint is closed"):
                sp.rollback()
        tx.commit()
        assert _values(db, "T") == ["retained"]


def test_scoped_savepoint_context_manager_rolls_back_on_exception():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:T {k:'outer'})")

            with pytest.raises(RuntimeError):
                with tx.savepoint():
                    tx.run("CREATE (:T {k:'inner'})")
                    raise RuntimeError("boom")

            tx.run("CREATE (:T {k:'after'})")
            tx.commit()

        assert _values(db, "T") == ["after", "outer"]


def test_named_savepoint_handle_rollback_discards_inner_changes_and_releases_marker():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:T {k:'outer'})")

            sp = tx.savepoint_named("sp1")
            tx.run("CREATE (:T {k:'inner'})")
            sp.rollback()

            tx.run("CREATE (:T {k:'after'})")
            tx.commit()

        assert _values(db, "T") == ["after", "outer"]


def test_named_savepoint_handle_release_keeps_inner_changes():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:T {k:'outer'})")

            sp = tx.savepoint_named("sp1")
            tx.run("CREATE (:T {k:'inner'})")
            sp.release()

            tx.commit()

        assert _values(db, "T") == ["inner", "outer"]


def test_named_savepoint_rollback_to_keeps_target_active():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            sp1 = tx.savepoint_named("sp1")
            tx.run("CREATE (:T {k:'a'})")

            tx.savepoint_named("sp2")
            tx.run("CREATE (:T {k:'b'})")

            tx.rollback_to("sp1")
            tx.run("CREATE (:T {k:'c'})")

            # sp1 should still be active after rollback_to("sp1")
            tx.rollback_to("sp1")
            tx.run("CREATE (:T {k:'d'})")

            sp1.release()
            tx.commit()

        assert _values(db, "T") == ["d"]


def test_named_savepoint_rollback_to_discards_newer_named_markers():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.savepoint_named("sp1")
            tx.run("CREATE (:T {k:'a'})")

            tx.savepoint_named("sp2")
            tx.run("CREATE (:T {k:'b'})")

            tx.rollback_to("sp1")

            with pytest.raises(VelrError, match="no such savepoint"):
                tx.rollback_to("sp2")

            tx.commit()


def test_named_savepoint_survives_dropped_python_handle():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            sp = tx.savepoint_named("sp1")
            del sp
            gc.collect()

            tx.run("CREATE (:T {k:'inner'})")
            tx.rollback_to("sp1")
            tx.commit()

        assert _count_label(db, "T") == 0


def test_named_savepoint_handle_can_release_after_rollback_to_target():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            sp = tx.savepoint_named("sp1")
            tx.run("CREATE (:T {k:'inner'})")

            tx.rollback_to("sp1")

            # rollback_to keeps the target active, so the original handle
            # should still be able to release it.
            sp.release()

            with pytest.raises(VelrError, match="no such savepoint"):
                tx.rollback_to("sp1")

            tx.commit()

        assert _count_label(db, "T") == 0


def test_named_savepoint_context_manager_release_on_clean_exit():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            with tx.savepoint_named("sp1"):
                tx.run("CREATE (:T {k:'inner'})")

            with pytest.raises(VelrError, match="no such savepoint"):
                tx.rollback_to("sp1")

            tx.commit()

        assert _values(db, "T") == ["inner"]


def test_named_savepoint_context_manager_rollback_on_exception():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:T {k:'outer'})")

            with pytest.raises(RuntimeError):
                with tx.savepoint_named("sp1"):
                    tx.run("CREATE (:T {k:'inner'})")
                    raise RuntimeError("boom")

            tx.run("CREATE (:T {k:'after'})")
            tx.commit()

        assert _values(db, "T") == ["after", "outer"]



def _values(db: Velr, label: str, key: str = "k") -> list:
    with db.exec_one(
        f"MATCH (n:{label}) RETURN n.{key} AS v ORDER BY v"
    ) as table:
        return table.collect(lambda row: row[0].as_python())


def test_named_savepoint_handle_rollback_produces_expected_db_state():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:T {k:'outer'})")

            sp = tx.savepoint_named("sp1")
            tx.run("CREATE (:T {k:'inner'})")
            sp.rollback()

            tx.run("CREATE (:T {k:'after'})")
            tx.commit()

        # "inner" was rolled back; "outer" and "after" remain
        assert _values(db, "T") == ["after", "outer"]


def test_rollback_to_named_savepoint_produces_expected_db_state():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:T {k:'outer'})")

            tx.savepoint_named("sp1")
            tx.run("CREATE (:T {k:'a'})")

            tx.savepoint_named("sp2")
            tx.run("CREATE (:T {k:'b'})")

            tx.rollback_to("sp1")
            tx.run("CREATE (:T {k:'c'})")

            tx.commit()

        # rollback_to("sp1") removed both "a" and "b"
        # "outer" was before sp1, so it remains
        # "c" was created after the rollback, so it remains
        assert _values(db, "T") == ["c", "outer"]


def test_commit_releases_active_named_savepoints_and_persists_all_surviving_writes(tmp_path):
    path = str(tmp_path / "commit-active-named-savepoints.db")

    with Velr.open(path) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:CommitSavepoint {k:'before'})")
            tx.savepoint_named("outer")
            tx.run("CREATE (:CommitSavepoint {k:'middle'})")
            tx.savepoint_named("inner")
            tx.run("CREATE (:CommitSavepoint {k:'after'})")
            tx.commit()

        with db.begin_tx() as tx:
            tx.run("CREATE (:CommitRetained {k:'before'})")
            tx.savepoint_named("retained")
            tx.run("CREATE (:CommitRetained {k:'discarded'})")
            tx.rollback_to("retained")
            tx.run("CREATE (:CommitRetained {k:'surviving'})")
            tx.commit()

    with Velr.open(path) as reopened:
        assert _values(reopened, "CommitSavepoint") == ["after", "before", "middle"]
        assert _values(reopened, "CommitRetained") == ["before", "surviving"]
