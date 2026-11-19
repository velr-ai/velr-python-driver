import gc
import pytest

from velr.driver import Velr, VelrError

pa = pytest.importorskip("pyarrow")  # all tests here require pyarrow


def _collect_rows_sorted_by_name(tbl):
    """
    Read all rows from `tbl` (expected schema: name,b,i,f) and return
    a list[(name:str, b:bool, i:int, f:float)] sorted by name.
    """
    try:
        rows = tbl.iter_rows()
        got = []
        for r in rows:
            # Cells -> Python values
            name = r[0].as_python()
            b    = r[1].as_python()
            i    = r[2].as_python()
            f    = r[3].as_python()
            got.append((name, b, i, f))
        return sorted(got, key=lambda t: t[0])
    finally:
        try:
            rows.close()
        except Exception:
            pass


def test_unwind_create_roundtrip_arrow_non_tx(db: Velr):
    # Build Arrays
    cols = {
        "name": pa.array(["Alice", "Bob"]),
        "b":    pa.array([True, False], type=pa.bool_()),
        "i":    pa.array([123, -7],     type=pa.int64()),
        "f":    pa.array([3.75, 2.1],   type=pa.float64()),
    }

    # Bind columns to logical name
    db.bind_arrow("_people", cols)

    # Create from bound data
    db.run(
        "UNWIND BIND('_people') AS r "
        "CREATE (:Tmp { name: r.name, b: r.b, i: r.i, f: r.f });"
    )

    # Verify round-trip
    t = db._exec_one_native(
        "MATCH (r:Tmp) "
        "RETURN r.name AS name, r.b AS b, r.i AS i, r.f AS f "
        "ORDER BY name"
    )
    try:
        got = _collect_rows_sorted_by_name(t)
        assert got == [("Alice", True, 123, 3.75), ("Bob", False, -7, 2.1)]
    finally:
        t.close()


def test_unwind_create_roundtrip_arrow_tx(db: Velr):
    with db.begin_tx() as tx:
        cols = {
            "name": pa.array(["Alice", "Bob"]),
            "b":    pa.array([True, False], type=pa.bool_()),
            "i":    pa.array([123, -7],     type=pa.int64()),
            "f":    pa.array([3.75, 2.1],   type=pa.float64()),
        }

        tx.bind_arrow("_people_tx", cols)

        tx.run(
            "UNWIND BIND('_people_tx') AS r "
            "CREATE (:Tmp { name: r.name, b: r.b, i: r.i, f: r.f });"
        )

        t = tx.exec_one(
            "MATCH (r:Tmp) "
            "RETURN r.name AS name, r.b AS b, r.i AS i, r.f AS f "
            "ORDER BY name"
        )
        try:
            got = _collect_rows_sorted_by_name(t)
            assert got == [("Alice", True, 123, 3.75), ("Bob", False, -7, 2.1)]
        finally:
            t.close()

        tx.commit()


def test_bind_arrow_ipc_roundtrip(db: Velr):
    source = db._exec_one_native("UNWIND [1,2,3] AS id RETURN id AS id ORDER BY id")
    try:
        ipc = source.to_ipc_bytes()
    finally:
        source.close()

    db.bind_arrow_ipc("_ids_ipc", ipc)

    out = db._exec_one_native(
        "UNWIND BIND('_ids_ipc') AS row RETURN row.id AS id ORDER BY id"
    )
    try:
        ids = [row[0].as_python() for row in out.iter_rows()]
    finally:
        out.close()

    assert ids == [1, 2, 3]


def test_bind_arrow_ipc_tx_roundtrip(db: Velr):
    source = db._exec_one_native("UNWIND [4,5,6] AS id RETURN id AS id ORDER BY id")
    try:
        ipc = source.to_ipc_bytes()
    finally:
        source.close()

    with db.begin_tx() as tx:
        tx.bind_arrow_ipc("_ids_ipc_tx", ipc)
        out = tx.exec_one(
            "UNWIND BIND('_ids_ipc_tx') AS row RETURN row.id AS id ORDER BY id"
        )
        try:
            ids = [row[0].as_python() for row in out.iter_rows()]
        finally:
            out.close()
        tx.commit()

    assert ids == [4, 5, 6]


def test_bind_arrow_accepts_recordbatch_and_table(db: Velr):
    # RecordBatch path (strict zero-copy per column)
    rb = pa.record_batch(
        [
            pa.array(["Alice", "Bob"]),
            pa.array([True, False]),
            pa.array([123, -7],     type=pa.int64()),
            pa.array([3.75, 2.1],   type=pa.float64()),
        ],
        names=["name", "b", "i", "f"],
    )
    db.bind_arrow("_people_rb", rb)
    db.run(
        "UNWIND BIND('_people_rb') AS r "
        "CREATE (:TmpRB { name: r.name, b: r.b, i: r.i, f: r.f });"
    )
    t1 = db._exec_one_native(
        "MATCH (r:TmpRB) "
        "RETURN r.name AS name, r.b AS b, r.i AS i, r.f AS f "
        "ORDER BY name"
    )
    try:
        got = _collect_rows_sorted_by_name(t1)
        assert got == [("Alice", True, 123, 3.75), ("Bob", False, -7, 2.1)]
    finally:
        t1.close()

    # Table path (preserves chunking end-to-end).
    # Construct chunked columns to explicitly exercise the chunked bind path.
    name_tbl = pa.chunked_array([pa.array(["Carol"]), pa.array(["Dave"])])
    b_tbl    = pa.chunked_array([pa.array([True]), pa.array([False])])
    i_tbl    = pa.chunked_array([pa.array([7], type=pa.int64()),
                                 pa.array([-100], type=pa.int64())])
    f_tbl    = pa.chunked_array([pa.array([1.25], type=pa.float64()),
                                 pa.array([9.0],  type=pa.float64())])
    tbl = pa.table({"name": name_tbl, "b": b_tbl, "i": i_tbl, "f": f_tbl})

    db.bind_arrow("_people_tbl", tbl)
    db.run(
        "UNWIND BIND('_people_tbl') AS r "
        "CREATE (:TmpT { name: r.name, b: r.b, i: r.i, f: r.f });"
    )
    t2 = db._exec_one_native(
        "MATCH (r:TmpT) "
        "RETURN r.name AS name, r.b AS b, r.i AS i, r.f AS f "
        "ORDER BY name"
    )
    try:
        got = _collect_rows_sorted_by_name(t2)
        assert got == [("Carol", True, 7, 1.25), ("Dave", False, -100, 9.0)]
    finally:
        t2.close()


def test_bind_arrow_struct_column_reads_as_map(db: Velr):
    metadata = pa.array(
        [
            {"score": 7, "tags": ["fast", "graph"]},
            {"score": 8, "tags": ["driver"]},
        ]
    )

    db.bind_arrow("_docs_struct", {"id": pa.array(["doc-1", "doc-2"]), "metadata": metadata})

    with db.exec_one(
        """
        UNWIND BIND('_docs_struct') AS row
        RETURN row.id AS id,
               row.metadata.score AS score,
               row.metadata.tags AS tags,
               row.metadata AS metadata
        ORDER BY id
        """
    ) as table:
        rows = table.to_records()

    assert rows == [
        {
            "id": "doc-1",
            "score": 7,
            "tags": ["fast", "graph"],
            "metadata": {"score": 7, "tags": ["fast", "graph"]},
        },
        {
            "id": "doc-2",
            "score": 8,
            "tags": ["driver"],
            "metadata": {"score": 8, "tags": ["driver"]},
        },
    ]


def test_bind_arrow_preserves_velr_typed_binary_values(db: Velr):
    map_json_tag = b"\x32"
    bool_true_tag = b"\x01"
    metadata_blob = map_json_tag + b'{"score":7,"tags":["fast","graph"]}'

    db.bind_arrow(
        "_docs_velr_typed",
        {
            "id": pa.array(["doc-1"]),
            "metadata": pa.array([metadata_blob], type=pa.binary()),
            "active": pa.array([bool_true_tag], type=pa.binary()),
        },
    )

    with db.exec_one(
        """
        UNWIND BIND('_docs_velr_typed') AS row
        RETURN row.id AS id,
               row.metadata.score AS score,
               row.metadata.tags AS tags,
               row.metadata AS metadata,
               row.active AS active
        """
    ) as table:
        rows = table.to_records()

    assert rows == [
        {
            "id": "doc-1",
            "score": 7,
            "tags": ["fast", "graph"],
            "metadata": {"score": 7, "tags": ["fast", "graph"]},
            "active": True,
        }
    ]


def test_bind_arrow_lifetime_no_py_refs_needed(db: Velr):
    # Bind using temporary arrays and immediately drop all Python references
    def _make_and_bind():
        cols = {
            "name": pa.array(["Eve", "Frank"]),
            "b":    pa.array([True, False]),
            "i":    pa.array([42, -1], type=pa.int64()),
            "f":    pa.array([0.5,  8.25], type=pa.float64()),
        }
        db.bind_arrow("_tmp_people_gc", cols)

    _make_and_bind()
    # Drop all local references and force GC; Rust side owns the Arrow C release
    gc.collect()

    db.run(
        "UNWIND BIND('_tmp_people_gc') AS r "
        "CREATE (:TmpG { name: r.name, b: r.b, i: r.i, f: r.f });"
    )
    t = db._exec_one_native(
        "MATCH (r:TmpG) "
        "RETURN r.name AS name, r.b AS b, r.i AS i, r.f AS f "
        "ORDER BY name"
    )
    try:
        got = _collect_rows_sorted_by_name(t)
        assert got == [("Eve", True, 42, 0.5), ("Frank", False, -1, 8.25)]
    finally:
        t.close()


def test_bind_arrow_rejects_non_arrays(db: Velr):
    # Wrong type (list of ints) should raise a VelrError from driver normalizer
    with pytest.raises(VelrError):
        db.bind_arrow("_bad", {"x": [1, 2, 3]})


def test_bind_arrow_chunked_columns_non_tx(db: Velr):
    # 2 chunks for name, 2 chunks for others; total rows = 3
    name = pa.chunked_array([pa.array(["A"]), pa.array(["B", "C"])])
    b    = pa.chunked_array([pa.array([True]), pa.array([False, True])])
    i    = pa.chunked_array([pa.array([1], type=pa.int64()),
                             pa.array([2, 3], type=pa.int64())])
    f    = pa.chunked_array([pa.array([1.0], type=pa.float64()),
                             pa.array([2.5, 3.5], type=pa.float64())])

    db.bind_arrow("_people_chunked", {"name": name, "b": b, "i": i, "f": f})
    db.run(
        "UNWIND BIND('_people_chunked') AS r "
        "CREATE (:TmpC { name: r.name, b: r.b, i: r.i, f: r.f });"
    )
    t = db._exec_one_native(
        "MATCH (r:TmpC) "
        "RETURN r.name AS name, r.b AS b, r.i AS i, r.f AS f "
        "ORDER BY name"
    )
    try:
        got = _collect_rows_sorted_by_name(t)
        assert got == [("A", True, 1, 1.0), ("B", False, 2, 2.5), ("C", True, 3, 3.5)]
    finally:
        t.close()


def test_bind_arrow_chunked_columns_tx(db: Velr):
    # Mix different chunk counts per column; totals still match (4 rows).
    name = pa.chunked_array([pa.array(["D"]), pa.array(["E"]), pa.array(["F", "G"])])
    b    = pa.chunked_array([pa.array([True, False]), pa.array([True, False])])
    i    = pa.chunked_array([pa.array([10, 20, 30, 40], type=pa.int64())])  # single chunk
    f    = pa.chunked_array([pa.array([1.1], type=pa.float64()),
                             pa.array([2.2, 3.3], type=pa.float64()),
                             pa.array([4.4], type=pa.float64())])

    with db.begin_tx() as tx:
        tx.bind_arrow("_people_chunked_tx", {"name": name, "b": b, "i": i, "f": f})
        tx.run(
            "UNWIND BIND('_people_chunked_tx') AS r "
            "CREATE (:TmpCTx { name: r.name, b: r.b, i: r.i, f: r.f });"
        )
        t = tx.exec_one(
            "MATCH (r:TmpCTx) "
            "RETURN r.name AS name, r.b AS b, r.i AS i, r.f AS f "
            "ORDER BY name"
        )
        try:
            got = _collect_rows_sorted_by_name(t)
            # Sorted by name
            assert got == [
                ("D", True, 10, 1.1),
                ("E", False, 20, 2.2),
                ("F", True, 30, 3.3),
                ("G", False, 40, 4.4),
            ]
        finally:
            t.close()
        tx.commit()
