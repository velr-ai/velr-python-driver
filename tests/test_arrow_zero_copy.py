import io
import gc
import os
import pytest

from velr.driver import Velr, VelrError

pa = pytest.importorskip("pyarrow")  # pyarrow is required for all tests here


def _last_table(db, cypher: str):
    st = db.exec(cypher)
    last = None
    try:
        while True:
            t = st.next_table()
            if t is None:
                break

            if getattr(t, "_parent", None) is st:
                try:
                    st._unregister_child(t)
                except Exception:
                    pass
                t._parent = db
                db._register_child(t)

            if last is not None:
                last.close()

            last = t

        if last is None:
            raise AssertionError("query produced no result tables")

        return last

    except Exception:
        if last is not None:
            try:
                last.close()
            except Exception:
                pass
        raise
    finally:
        try:
            st.close()
        except Exception:
            pass


def test_to_pyarrow_roundtrip_all_scalars(db: Velr, sample_all_scalars):
    tbl = _last_table(
        db,
        """
        MATCH (s:Sample)
        RETURN s.n AS n, s.b AS b, s.i AS i, s.f AS f, s.ta AS ta, s.tb AS tb, s.arr AS arr
        """,
    )
    try:
        pa_tbl = tbl.to_pyarrow()  # zero-copy IPC → pa.Table
        assert isinstance(pa_tbl, pa.Table)
        assert pa_tbl.num_rows == 1
        assert pa_tbl.num_columns == 7

        cols = {f.name: pa_tbl.column(i) for i, f in enumerate(pa_tbl.schema)}
        # Schema types (your serializer: Utf8/Bool/I64/F64/Utf8/Utf8/Utf8)
        assert pa_tbl.schema.field("n").type == pa.large_utf8()
        assert pa_tbl.schema.field("b").type == pa.bool_()
        assert pa_tbl.schema.field("i").type == pa.int64()
        assert pa_tbl.schema.field("f").type == pa.float64()
        assert pa_tbl.schema.field("ta").type == pa.large_utf8()
        assert pa_tbl.schema.field("tb").type == pa.large_utf8()
        assert pa_tbl.schema.field("arr").type == pa.large_utf8()

        # Values
        r = 0
        assert cols["n"][r].as_py() is None
        assert cols["b"][r].as_py() is True
        assert cols["i"][r].as_py() == 123
        assert abs(cols["f"][r].as_py() - 3.75) < 1e-12
        assert cols["ta"][r].as_py() == "hello"
        assert cols["tb"][r].as_py() == "false"
        assert cols["arr"][r].as_py() == '["a","b"]'
    finally:
        tbl.close()


def test_zero_rows_utf8_schema(db: Velr):
    tbl = _last_table(
        db,
        "MATCH (p:Person {name:'Nobody'}) RETURN p.name AS name",
    )
    try:
        pa_tbl = tbl.to_pyarrow()
        assert pa_tbl.num_rows == 0
        assert pa_tbl.num_columns == 1
        assert pa_tbl.schema.field("name").type == pa.large_utf8()
    finally:
        tbl.close()

@pytest.mark.parametrize("want_pandas", [True, False])
def test_to_pandas_polars_optional(db: Velr, sample_all_scalars, want_pandas):
    tbl = _last_table(
        db,
        "MATCH (s:Sample) RETURN s.n AS n, s.b AS b, s.i AS i, s.f AS f, s.ta AS ta, s.tb AS tb, s.arr AS arr",
    )
    try:
        if want_pandas:
            try:
                import pandas as pd  # noqa: F401
            except Exception:
                with pytest.raises(VelrError):
                    tbl.to_pandas()
                return

            df = tbl.to_pandas()
            assert list(df.columns) == ["n", "b", "i", "f", "ta", "tb", "arr"]
            assert len(df) == 1

            # n is still nullable
            assert pd.isna(df.loc[0, "n"])

            # Use truthiness instead of identity: pandas gives numpy.bool_
            assert bool(df.loc[0, "b"]) == True
            assert df.loc[0, "i"] == 123
            assert abs(df.loc[0, "f"] - 3.75) < 1e-12
            assert df.loc[0, "ta"] == "hello"
            assert df.loc[0, "tb"] == "false"
            assert df.loc[0, "arr"] == '["a","b"]'
        else:
            # Polars guard we added for ARM:
            if os.environ.get("VELRPY_SKIP_POLARS_TESTS") == "1":
                pytest.skip("Skipping Polars tests on this build host (VELRPY_SKIP_POLARS_TESTS=1)")

            try:
                import polars as pl  # noqa: F401
            except Exception:
                with pytest.raises(VelrError):
                    tbl.to_polars()
                return

            df = tbl.to_polars()
            assert df.width == 7 and df.height == 1
            r = df.row(0, named=True)
            assert r["n"] is None
            assert bool(r["b"]) is True
            assert r["i"] == 123
            assert abs(r["f"] - 3.75) < 1e-12
            assert r["ta"] == "hello"
            assert r["tb"] == "false"
            assert r["arr"] == '["a","b"]'
    finally:
        tbl.close()



def test_advanced_raw_ipc_open_file(db: Velr, sample_all_scalars):
    """
    Exercise the 'advanced' zero-copy foreign buffer by feeding it directly to
    pyarrow.ipc.open_file (File format, not Stream).
    """
    tbl = _last_table(db, "MATCH (s:Sample) RETURN s.b AS b, s.i AS i")
    try:
        # If your method is named differently, rename here:
        #   buf = tbl.ipc_foreign_buffer()
        buf = tbl.to_pyarrow_zero_copy()  # expected to return a pa.Buffer (or pyarrow.Buffer)
        assert isinstance(buf, pa.Buffer)

        # Wrap in a BufferReader → open_file → read all
        with pa.ipc.open_file(pa.BufferReader(buf)) as reader:
            pa_tbl = reader.read_all()

        assert pa_tbl.num_rows == 1
        assert pa_tbl.num_columns == 2
        assert pa_tbl.schema.field("b").type == pa.bool_()
        assert pa_tbl.schema.field("i").type == pa.int64()
        assert pa_tbl.column(0)[0].as_py() is True
        assert pa_tbl.column(1)[0].as_py() == 123

        # Drop all refs and force GC; the Buffer's exported capsule should call back into velr_free.
        del pa_tbl, reader, buf
        gc.collect()
    finally:
        tbl.close()


def test_run_discards_results(db: Velr):
    db.run("CREATE (:X {a:1})")  # should not raise


def test_tx_run_works(db: Velr):
    with db.begin_tx() as tx:
        tx.run("CREATE (:Y {b:2})")
        tx.commit()
