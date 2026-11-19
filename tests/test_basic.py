import json

import pytest

import velr.driver as driver_module
from velr.driver import Velr, VelrError

MOVIES_CREATE = r"""
CREATE
  (keanu:Person:Actor {name:'Keanu Reeves', born:1964}),
  (nolan:Person:Director {name:'Christopher Nolan'}),
  (matrix:Movie {title:'The Matrix', released:1999, genres:['Sci-Fi','Action']}),
  (inception:Movie {title:'Inception', released:2010, genres:['Sci-Fi','Heist']}),
  (keanu)-[:ACTED_IN {roles:['Neo']}]->(matrix),
  (nolan)-[:DIRECTED]->(inception);
"""


def _missing_options_symbol(exc: VelrError) -> bool:
    msg = str(exc)
    return "does not expose" in msg and (
        "_with_options" in msg or "query_param" in msg or "velr_run" in msg
    )


def _missing_vector_symbol(exc: VelrError) -> bool:
    msg = str(exc)
    return "does not expose" in msg and "velr_register_vector_embedder" in msg


def _vector_feature_unavailable(exc: VelrError) -> bool:
    return "requires the vector-usearch feature" in str(exc)


def _stored_vector_query_unavailable(exc: VelrError) -> bool:
    return "query must be a string literal or bound string parameter" in str(exc)


def _fulltext_feature_unavailable(exc: VelrError) -> bool:
    return "requires the fulltext-tantivy feature" in str(exc)


def _vector_text(input):
    return "\n".join(
        field.value
        for field in input.fields
        if field.value_type == "string" and isinstance(field.value, str)
    )


def _toy_vector(text):
    lower = text.lower()
    if "alpha" in lower:
        return [1.0, 0.0, 0.0]
    if "beta" in lower:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


def _vector_field_snapshot(field):
    return (field.name, field.value_type, field.value, field.value_json)


def _title_from_node_cell(cell):
    node = cell.as_python(parse_json=True)
    if isinstance(node, str):
        node = json.loads(node)
    return node["properties"]["title"]


def test_exec_one_roundtrip():
    with Velr.open(None) as db:
        # seed
        with db.exec(MOVIES_CREATE) as st:
            while True:
                t = st.next_table()
                if not t: break
                t.close()

        # query
        t = db.exec_one("MATCH (m:Movie {title:'Inception'}) "
                        "RETURN m.title AS title, m.released AS y, m.genres AS g")
        cols = t.column_names()
        assert cols == ["title", "y", "g"]

        with t.iter_rows() as rows:
            got = list(rows)
        t.close()

        assert len(got) == 1
        (c0, c1, c2) = got[0]
        assert c0.data == b"Inception"
        assert c1.i64 == 2010
        assert c2.data == b'["Sci-Fi","Heist"]'


def test_rows_deprecated_alias_for_iter_rows():
    with Velr.open(None) as db:
        with db.exec_one("RETURN 1 AS value") as table:
            with pytest.warns(DeprecationWarning, match="Table.iter_rows"):
                with table.rows() as rows:
                    got = list(rows)

    assert got[0][0].as_python() == 1


def test_private_rows_iter_python_matches_cell_as_python():
    with Velr.open(None) as db:
        db.run(
            """
            CREATE (:Person {name:'Alice', age:42, tags:['graph','driver']})
            """
        )

        with db.exec_one(
            """
            MATCH (p:Person)
            RETURN p.name AS name, p.age AS age, p.tags AS tags, p AS node
            """
        ) as table:
            with table.iter_rows() as rows:
                from_cells = [
                    tuple(cell.as_python(parse_json=True) for cell in row)
                    for row in rows
                ]

        with db.exec_one(
            """
            MATCH (p:Person)
            RETURN p.name AS name, p.age AS age, p.tags AS tags, p AS node
            """
        ) as table:
            with table.iter_rows() as rows:
                direct = list(rows._iter_python(parse_json=True))

        assert direct == from_cells
        assert len(direct) == 1
        name, age, tags, node = direct[0]
        assert (name, age, tags) == ("Alice", 42, ["graph", "driver"])
        assert isinstance(node["identity"], int)
        assert node["elementId"] == str(node["identity"])
        assert node["labels"] == ["Person"]
        assert node["properties"] == {
            "age": 42,
            "name": "Alice",
            "tags": ["graph", "driver"],
        }


def test_bind_records_scalar_rows_roundtrip():
    with Velr.open(None) as db:
        db.bind_records(
            "_rows",
            [
                {
                    "id": "a",
                    "count": 1,
                    "score": 1.5,
                    "active": True,
                    "note": None,
                },
                {
                    "id": "b",
                    "count": 2,
                    "score": 2.5,
                    "active": False,
                    "note": "ok",
                },
            ],
        )

        with db.exec_one(
            """
            UNWIND BIND('_rows') AS row
            RETURN row.id AS id,
                   row.count AS count,
                   row.score AS score,
                   row.active AS active,
                   row.note AS note
            ORDER BY id
            """
        ) as table:
            rows = table.to_rows()

    assert rows == [
        ["a", 1, 1.5, True, None],
        ["b", 2, 2.5, False, "ok"],
    ]


def test_bind_records_scalar_rows_work_in_tx():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.bind_records("_rows", [{"id": "a", "name": "Alice"}])
            tx.run(
                """
                UNWIND BIND('_rows') AS row
                CREATE (:Person {id: row.id, name: row.name})
                """
            )
            tx.commit()

        with db.exec_one(
            "MATCH (p:Person) RETURN p.id AS id, p.name AS name"
        ) as table:
            assert table.to_rows() == [["a", "Alice"]]


def test_bind_records_nested_rows_use_native_row_path():
    with Velr.open(None) as db:
        db.bind_records("_rows", [{"id": "a", "tags": ["graph", "driver"]}])

        with db.exec_one(
            """
            UNWIND BIND('_rows') AS row
            RETURN row.id AS id, row.tags AS tags
            """
        ) as table:
            assert table.to_rows() == [["a", ["graph", "driver"]]]


def test_bind_records_row_map_merge_visible_in_same_query():
    with Velr.open(None) as db:
        db.exec_one(
            """
            CREATE
              (:Entity {uuid: 'n1', stale: 'old'}),
              (:Entity {uuid: 'a'}),
              (:Entity {uuid: 'b'})
            RETURN 1 AS ok
            """
        )
        db.exec_one(
            """
            MATCH (a:Entity {uuid: 'a'}), (b:Entity {uuid: 'b'})
            CREATE (a)-[:RELATES_TO {uuid: 'e1', stale: 'old'}]->(b)
            RETURN 1 AS ok
            """
        )

        db.bind_records(
            "_node_rows",
            [
                {
                    "uuid": "n1",
                    "props": {
                        "role": "engineer",
                        "score": 3,
                        "active": True,
                        "stale": None,
                    },
                }
            ],
        )
        with db.exec_one(
            """
            UNWIND BIND('_node_rows') AS row
            MATCH (n:Entity {uuid: row.uuid})
            SET n += row.props
            RETURN n.role AS role, n.score AS score, n.active AS active, n.stale AS stale
            """
        ) as table:
            assert table.to_rows() == [["engineer", 3, True, None]]

        db.bind_records(
            "_edge_rows",
            [
                {
                    "uuid": "e1",
                    "props": {
                        "confidence": 0.7,
                        "kind": "primary",
                        "active": False,
                        "stale": None,
                    },
                }
            ],
        )
        with db.exec_one(
            """
            UNWIND BIND('_edge_rows') AS row
            MATCH ()-[e:RELATES_TO {uuid: row.uuid}]->()
            SET e += row.props
            RETURN e.confidence AS confidence, e.kind AS kind,
                   e.active AS active, e.stale AS stale
            """
        ) as table:
            assert table.to_rows() == [[0.7, "primary", False, None]]


def test_bind_records_numeric_lists_stay_on_record_path(monkeypatch):
    def reject_bind_arrow(self, logical, columns):
        raise AssertionError(f"bind_records should not route {logical!r} through bind_arrow")

    monkeypatch.setattr(driver_module.Velr, "bind_arrow", reject_bind_arrow)
    monkeypatch.setattr(driver_module.VelrTx, "bind_arrow", reject_bind_arrow)

    records = [
        {
            "id": f"chunk-{index}",
            "embedding": [float((index + dim) % 7) / 7.0 for dim in range(64)],
        }
        for index in range(128)
    ]

    with Velr.open(None) as db:
        db.bind_records(
            "_rows",
            records,
        )

        with db.exec_one(
            """
            UNWIND BIND('_rows') AS row
            RETURN count(*) AS count
            """
        ) as table:
            assert table.to_rows() == [[128]]

        with db.begin_tx() as tx:
            tx.bind_records(
                "_tx_rows",
                records,
            )
            tx.run(
                """
                UNWIND BIND('_tx_rows') AS row
                CREATE (:Chunk {id: row.id, embedding: row.embedding})
                """
            )
            tx.commit()

        with db.exec_one("MATCH (c:Chunk) RETURN count(c) AS count") as table:
            assert table.to_rows() == [[128]]


def test_bind_records_list_field_can_drive_unwind():
    with Velr.open(None) as db:
        db.bind_records(
            "_rows",
            [
                {"id": "a", "tags": ["graph", "driver"]},
                {"id": "b", "tags": ["vector"]},
                {"id": "c", "tags": []},
            ],
        )

        with db.exec_one(
            """
            UNWIND BIND('_rows') AS row
            UNWIND row.tags AS tag
            RETURN row.id AS id, tag
            ORDER BY id, tag
            """
        ) as table:
            assert table.to_rows() == [
                ["a", "driver"],
                ["a", "graph"],
                ["b", "vector"],
            ]


def test_private_table_collect_python_converts_without_cell_objects():
    with Velr.open(None) as db:
        with db.exec_one(
            """
            UNWIND [1, 2, 3] AS value
            RETURN value, value >= 2 AS ge_two
            ORDER BY value
            """
        ) as table:
            assert table._collect_python() == [
                (1, False),
                (2, True),
                (3, True),
            ]


def test_private_table_collect_python_handles_multiple_batches():
    values = ",".join(str(value) for value in range(1100))
    with Velr.open(None) as db:
        with db.exec_one(
            f"""
            UNWIND [{values}] AS value
            RETURN value
            ORDER BY value
            """
        ) as table:
            rows = table._collect_python()

    assert rows == [(value,) for value in range(1100)]


@pytest.mark.parametrize("conversion", ["_collect_python", "to_rows"])
def test_large_literal_unwind_preserves_mixed_python_types(conversion):
    values = ",".join(["1", "1.5", "'1'", "null", "true"] * 220)
    with Velr.open(None) as db:
        with db.exec_one(
            f"UNWIND [{values}] AS value RETURN value ORDER BY value"
        ) as table:
            actual = [row[0] for row in getattr(table, conversion)()]

    # Captured once from Neo4j 5.26.28 on 2026-10-10; tests use no live oracle.
    expected = ["1"] * 220 + [True] * 220 + [1] * 220 + [1.5] * 220 + [None] * 220
    assert actual == expected
    # Python considers True == 1; check types as well as values.
    assert [type(value) for value in actual] == [type(value) for value in expected]


def test_table_to_rows_matches_python_conversion():
    with Velr.open(None) as db:
        with db.exec_one(
            """
            UNWIND [1] AS _
            RETURN 'Alice' AS name,
                   42 AS age,
                   ['graph', 'driver'] AS tags,
                   {kind: 'Person', active: true} AS props,
                   null AS missing
            """
        ) as table:
            packed = table.to_rows()

        with db.exec_one(
            """
            UNWIND [1] AS _
            RETURN 'Alice' AS name,
                   42 AS age,
                   ['graph', 'driver'] AS tags,
                   {kind: 'Person', active: true} AS props,
                   null AS missing
            """
        ) as table:
            direct = [list(row) for row in table._collect_python(parse_json=True)]

    assert packed == direct
    assert packed == [
        [
            "Alice",
            42,
            ["graph", "driver"],
            {"active": True, "kind": "Person"},
            None,
        ]
    ]


def test_table_json_rows_bytes_can_leave_json_cells_serialized():
    with Velr.open(None) as db:
        with db.exec_one(
            """
            UNWIND [1] AS _
            RETURN ['graph', 'driver'] AS tags,
                   {kind: 'Person', active: true} AS props
            """
        ) as table:
            packed = json.loads(table._to_json_rows_bytes(parse_json=False))

    tags, props = packed[0]
    assert json.loads(tags) == ["graph", "driver"]
    assert json.loads(props) == {"active": True, "kind": "Person"}


def test_table_to_rows_columns_decode_text_by_name():
    with Velr.open(None) as db:
        with db.exec_one(
            """
            UNWIND [1] AS _
            RETURN 'doc-1' AS id,
                   '{"score":7,"tags":["fast"]}' AS metadata,
                   'plain text' AS label
            """
        ) as table:
            packed = table.to_rows(columns=("metadata",))

    assert packed == [
        [
            "doc-1",
            {"score": 7, "tags": ["fast"]},
            "plain text",
        ]
    ]


def test_table_to_rows_columns_decode_text_by_index():
    with Velr.open(None) as db:
        with db.exec_one(
            """
            UNWIND [1] AS _
            RETURN 'doc-1' AS id,
                   '{"score":7}' AS metadata
            """
        ) as table:
            packed = table.to_rows(columns=(1,))

    assert packed == [
        [
            "doc-1",
            {"score": 7},
        ]
    ]


def test_table_to_rows_columns_can_be_a_single_name():
    with Velr.open(None) as db:
        with db.exec_one(
            """
            UNWIND [1] AS _
            RETURN 'doc-1' AS id,
                   '{"score":7}' AS metadata,
                   'plain text' AS label
            """
        ) as table:
            packed = table.to_rows(columns="metadata")

    assert packed == [
        [
            "doc-1",
            {"score": 7},
            "plain text",
        ]
    ]


def test_table_to_rows_columns_can_be_a_single_index():
    with Velr.open(None) as db:
        with db.exec_one(
            """
            UNWIND [1] AS _
            RETURN 'doc-1' AS id,
                   '{"score":7}' AS metadata
            """
        ) as table:
            packed = table.to_rows(columns=1)

    assert packed == [
        [
            "doc-1",
            {"score": 7},
        ]
    ]


def test_table_to_rows_projection_is_controlled_by_cypher_return():
    with Velr.open(None) as db:
        with db.exec_one(
            """
            UNWIND [1] AS _
            RETURN '{"score":7,"tags":["fast"]}' AS metadata,
                   'doc-1' AS id
            """
        ) as table:
            packed = table.to_rows(columns=("metadata",))

    assert packed == [
        [
            {"score": 7, "tags": ["fast"]},
            "doc-1",
        ]
    ]


def test_table_to_rows_leaves_text_json_strings_alone_by_default():
    with Velr.open(None) as db:
        with db.exec_one("RETURN '{\"score\":7}' AS metadata") as table:
            packed = table.to_rows()

    assert packed == [[r'{"score":7}']]


def test_table_to_rows_rejects_unknown_column_name():
    with Velr.open(None) as db:
        with db.exec_one("RETURN '{}' AS properties") as table:
            with pytest.raises(ValueError, match="unknown column name"):
                table.to_rows(columns=["missing"])


def test_table_to_rows_rejects_out_of_range_column_index():
    with Velr.open(None) as db:
        with db.exec_one("RETURN '{}' AS properties") as table:
            with pytest.raises(ValueError, match="column index 2"):
                table.to_rows(columns=[2])


@pytest.fixture
def duplicate_column_table(monkeypatch):
    with Velr.open(None) as db:
        with db.exec_one("RETURN 1 AS value, 2 AS other") as table:
            # Cypher rejects duplicate aliases before producing a result table.
            monkeypatch.setattr(type(table), "column_names", lambda _: ["value", "value"])
            yield table


def test_table_to_rows_rejects_ambiguous_column_name(duplicate_column_table):
    with pytest.raises(ValueError, match="ambiguous column name"):
        duplicate_column_table.to_rows(columns=["value"])
    assert duplicate_column_table.to_rows(columns=[0]) == [[1, 2]]


def test_table_to_records_returns_named_dicts():
    with Velr.open(None) as db:
        with db.exec_one(
            """
            UNWIND [1] AS _
            RETURN 'doc-1' AS id,
                   {score: 7, tags: ['fast']} AS metadata,
                   'plain text' AS label
            """
        ) as table:
            records = table.to_records()

    assert records == [
        {
            "id": "doc-1",
            "metadata": {"score": 7, "tags": ["fast"]},
            "label": "plain text",
        }
    ]


def test_table_to_records_columns_decode_text_columns():
    with Velr.open(None) as db:
        with db.exec_one(
            """
            UNWIND [1] AS _
            RETURN 'doc-1' AS id,
                   '{"score":7,"tags":["fast"]}' AS metadata,
                   'plain text' AS label
            """
        ) as table:
            records = table.to_records(columns=("metadata",))

    assert records == [
        {
            "id": "doc-1",
            "metadata": {"score": 7, "tags": ["fast"]},
            "label": "plain text",
        }
    ]


def test_table_to_records_columns_can_decode_by_index():
    with Velr.open(None) as db:
        with db.exec_one(
            """
            UNWIND [1] AS _
            RETURN 'doc-1' AS id,
                   '{"score":7,"tags":["fast"]}' AS metadata
            """
        ) as table:
            records = table.to_records(columns=(1,))

    assert records == [
        {
            "id": "doc-1",
            "metadata": {"score": 7, "tags": ["fast"]},
        }
    ]


def test_table_to_records_rejects_duplicate_column_names(duplicate_column_table):
    with pytest.raises(ValueError, match="unique result column names"):
        duplicate_column_table.to_records()


def test_bind_records_accepts_to_records_output():
    with Velr.open(None) as db:
        records = db.to_records(
            """
            UNWIND [1] AS _
            RETURN 'doc-1' AS id,
                   1 AS rank,
                   {score: 7, tags: ['fast', 'graph']} AS metadata,
                   true AS active
            """
        )

        db.bind_records("_roundtrip", records)

        with db.exec_one(
            """
            UNWIND BIND('_roundtrip') AS row
            RETURN row.id AS id,
                   row.rank AS rank,
                   row.metadata AS metadata,
                   row.active AS active
            """
        ) as table:
            roundtripped = table.to_records()

    assert roundtripped == records


def test_db_to_rows_and_records_columns_decode_text_columns():
    with Velr.open(None) as db:
        rows = db.to_rows(
            """
            UNWIND [1] AS _
            RETURN 'doc-1' AS id,
                   '{"score":7}' AS metadata
            """,
            columns=("metadata",),
        )
        records = db.to_records(
            """
            UNWIND [1] AS _
            RETURN 'doc-1' AS id,
                   '{"score":7}' AS metadata
            """,
            columns=("metadata",),
        )

    assert rows == [["doc-1", {"score": 7}]]
    assert records == [{"id": "doc-1", "metadata": {"score": 7}}]


def test_tx_table_to_rows_roundtrip():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            with tx.exec_one(
                """
                UNWIND [1] AS _
                RETURN 'Alice' AS name,
                       {source: 'tx'} AS properties
                """
            ) as table:
                packed = table.to_rows()

    assert packed == [["Alice", {"source": "tx"}]]


def test_table_to_json_rows_bytes_escapes_text_strings():
    with Velr.open(None) as db:
        with db.exec_one(
            """
            UNWIND [1] AS _
            RETURN 'plain' AS plain,
                   'quote " inside' AS quoted,
                   'm\u00f8\u00f8se' AS unicode_text
            """
        ) as table:
            raw = table._to_json_rows_bytes()
            packed = json.loads(raw)

    assert packed == [["plain", 'quote " inside', "m\u00f8\u00f8se"]]
    assert b'quote \\" inside' in raw


def test_table_to_rows_handles_multiple_batches():
    values = ",".join(str(value) for value in range(1100))
    with Velr.open(None) as db:
        with db.exec_one(
            f"""
            UNWIND [{values}] AS value
            RETURN value
            ORDER BY value
            """
        ) as table:
            rows = table.to_rows()

    assert rows == [[value] for value in range(1100)]


def test_db_to_rows_returns_last_result_table():
    with Velr.open(None) as db:
        rows = db.to_rows(
            """
            RETURN 'ignored' AS value;
            RETURN 'kept' AS value, {rank: 1} AS metadata
            """
        )

    assert rows == [["kept", {"rank": 1}]]


def test_db_to_rows_columns_decode_text_columns():
    with Velr.open(None) as db:
        rows = db.to_rows(
            """
            UNWIND [1] AS _
            RETURN 'doc-1' AS id,
                   '{"score":7,"tags":["fast"]}' AS metadata,
                   'plain text' AS label
            """,
            columns=("metadata",),
        )

    assert rows == [["doc-1", {"score": 7, "tags": ["fast"]}, "plain text"]]


def test_db_to_rows_returns_empty_list_for_no_result_tables():
    with Velr.open(None) as db:
        rows = db.to_rows("CREATE (:Person {name: 'Alice'})")

    assert rows == []


def test_db_to_records_returns_last_result_table():
    with Velr.open(None) as db:
        records = db.to_records(
            """
            RETURN 'ignored' AS value;
            RETURN 'kept' AS value, {rank: 1} AS metadata
            """
        )

    assert records == [{"value": "kept", "metadata": {"rank": 1}}]


def test_db_to_records_columns_decode_text_columns():
    with Velr.open(None) as db:
        records = db.to_records(
            """
            UNWIND [1] AS _
            RETURN 'doc-1' AS id,
                   '{"score":7,"tags":["fast"]}' AS metadata,
                   'plain text' AS label
            """,
            columns=("metadata",),
        )

    assert records == [
        {
            "id": "doc-1",
            "metadata": {"score": 7, "tags": ["fast"]},
            "label": "plain text",
        }
    ]


def test_db_to_records_returns_empty_list_for_no_result_tables():
    with Velr.open(None) as db:
        records = db.to_records("CREATE (:Person {name: 'Alice'})")

    assert records == []


def test_max_result_rows_caps_rows():
    with Velr.open(None) as db:
        try:
            t = db.exec_one(
                "UNWIND [1,2,3,4,5,6] AS x RETURN x ORDER BY x LIMIT 10",
                max_result_rows=5,
            )
        except VelrError as exc:
            if _missing_options_symbol(exc):
                pytest.skip("loaded runtime does not expose query options ABI")
            raise

        assert t.column_names() == ["x"]
        with t.iter_rows() as rows:
            got = list(rows)
        t.close()

        assert [row[0].i64 for row in got] == [1, 2, 3, 4, 5]

        t = db.exec_one(
            "UNWIND [1,2,3,4,5,6] AS x RETURN x ORDER BY x LIMIT 3",
            max_result_rows=5,
        )
        assert t.column_names() == ["x"]
        with t.iter_rows() as rows:
            got = list(rows)
        t.close()

        assert [row[0].i64 for row in got] == [1, 2, 3]


def test_max_result_rows_caps_each_stream_table():
    with Velr.open(None) as db:
        try:
            st = db.exec(
                "UNWIND [1,2,3] AS x RETURN x ORDER BY x; "
                "UNWIND [10,20,30] AS y RETURN y ORDER BY y",
                max_result_rows=1,
            )
        except VelrError as exc:
            if _missing_options_symbol(exc):
                pytest.skip("loaded runtime does not expose query options ABI")
            raise

        with st:
            first = st.next_table()
            assert first is not None
            assert first.column_names() == ["x"]
            with first.iter_rows() as rows:
                got = list(rows)
            first.close()
            assert [row[0].i64 for row in got] == [1]

            second = st.next_table()
            assert second is not None
            assert second.column_names() == ["y"]
            with second.iter_rows() as rows:
                got = list(rows)
            second.close()
            assert [row[0].i64 for row in got] == [10]

            assert st.next_table() is None


def test_max_result_rows_zero_preserves_columns_without_rows():
    with Velr.open(None) as db:
        try:
            t = db.exec_one("RETURN 1 AS one, 2 AS two", max_result_rows=0)
        except VelrError as exc:
            if _missing_options_symbol(exc):
                pytest.skip("loaded runtime does not expose query options ABI")
            raise

        assert t.column_names() == ["one", "two"]
        with t.iter_rows() as rows:
            got = list(rows)
        t.close()

        assert got == []


def test_max_result_rows_caps_rows_in_tx():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            try:
                t = tx.exec_one(
                    "UNWIND [10,20,30] AS x RETURN x ORDER BY x",
                    max_result_rows=1,
                )
            except VelrError as exc:
                if _missing_options_symbol(exc):
                    pytest.skip("loaded runtime does not expose query options ABI")
                raise

            assert t.column_names() == ["x"]
            with t.iter_rows() as rows:
                got = list(rows)
            t.close()

            assert [row[0].i64 for row in got] == [10]


def test_query_params_and_max_result_rows():
    with Velr.open(None) as db:
        try:
            t = db.exec_one(
                "UNWIND [1,2,3,4] AS x WITH x WHERE x >= $min RETURN x ORDER BY x",
                max_result_rows=2,
                params={"min": 2},
            )
        except VelrError as exc:
            if _missing_options_symbol(exc):
                pytest.skip("loaded runtime does not expose query params/options ABI")
            raise

        assert t.column_names() == ["x"]
        with t.iter_rows() as rows:
            got = list(rows)
        t.close()

        assert [row[0].i64 for row in got] == [2, 3]


def test_query_params_preserve_string_values():
    with Velr.open(None) as db:
        try:
            t = db.exec_one(
                "RETURN $name AS name, $literal AS literal",
                params={"name": "Alice", "literal": "MATCH (n) RETURN n"},
            )
        except VelrError as exc:
            if _missing_options_symbol(exc):
                pytest.skip("loaded runtime does not expose query params/options ABI")
            raise

        assert t.column_names() == ["name", "literal"]
        with t.iter_rows() as rows:
            got = list(rows)
        t.close()

        assert len(got) == 1
        assert got[0][0].data == b"Alice"
        assert got[0][1].data == b"MATCH (n) RETURN n"


def test_query_params_projected_list_of_maps_imports_extra_fields():
    with Velr.open(None) as db:
        rows = [
            {"id": "a", "name": "Alpha", "unused": {"nested": True}},
            {"id": "b", "name": "Beta", "unused": [1, 2, 3]},
        ]
        try:
            db.run(
                """
                UNWIND $rows AS row
                WITH row.id AS id, row.name AS name
                CREATE (:Thing {id: id, name: name})
                """,
                params={"rows": rows},
            )
            t = db.exec_one(
                "MATCH (t:Thing) RETURN t.id AS id, t.name AS name ORDER BY id",
            )
        except VelrError as exc:
            if _missing_options_symbol(exc):
                pytest.skip("loaded runtime does not expose query params/options ABI")
            raise

        assert t.column_names() == ["id", "name"]
        with t.iter_rows() as got_rows:
            got = [(row[0].data.decode("utf-8"), row[1].data.decode("utf-8")) for row in got_rows]
        t.close()

        assert got == [("a", "Alpha"), ("b", "Beta")]


def test_query_params_work_in_tx_with_map_properties():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            try:
                tx.run(
                    "CREATE (:Person $props)",
                    params={"props": {"name": "Alice", "score": 7}},
                )
                t = tx.exec_one(
                    "MATCH (p:Person {name: $name}) RETURN p.score AS score",
                    params={"name": "Alice"},
                )
            except VelrError as exc:
                if _missing_options_symbol(exc):
                    pytest.skip("loaded runtime does not expose query params/options ABI")
                raise

            assert t.column_names() == ["score"]
            with t.iter_rows() as rows:
                got = list(rows)
            t.close()

            assert [row[0].i64 for row in got] == [7]


def test_fulltext_public_api_indexes_and_queries_text(tmp_path):
    with Velr.open(str(tmp_path / "fulltext.sqlite")) as db:
        db.run(
            """
            CREATE
              (:Paper {title: 'Vector Search', abstract: 'graph retrieval with embeddings'}),
              (:Paper {title: 'Planner Notes', abstract: 'query planning internals'})
            """
        )
        try:
            db.run(
                """
                CREATE FULLTEXT INDEX paperText IF NOT EXISTS
                FOR (n:Paper) ON EACH [n.title, n.abstract]
                """
            )
        except VelrError as exc:
            if _fulltext_feature_unavailable(exc):
                pytest.skip("loaded runtime does not include fulltext-tantivy")
            raise

        try:
            table = db.exec_one(
                """
                CALL db.index.fulltext.queryNodes('paperText', 'title:vector')
                YIELD node, score
                RETURN node, score
                """
            )
        except VelrError as exc:
            if _fulltext_feature_unavailable(exc):
                pytest.skip("loaded runtime does not include fulltext-tantivy")
            raise

        with table:
            with table.iter_rows() as rows:
                got = list(rows)

    assert len(got) == 1
    assert _title_from_node_cell(got[0][0]) == "Vector Search"
    assert isinstance(got[0][1].as_python(), float)


def test_vector_embedder_public_api_indexes_and_queries_text(tmp_path):
    seen = []

    def embedder(inputs):
        for input in inputs:
            seen.append(
                (
                    input.index_name,
                    input.dimensions,
                    input.purpose,
                    input.entity_kind,
                    input.entity_id,
                    [_vector_field_snapshot(field) for field in input.fields],
                    _vector_text(input),
                )
            )
        return [_toy_vector(_vector_text(input)) for input in inputs]

    with Velr.open(str(tmp_path / "vector.sqlite")) as db:
        try:
            db.register_vector_embedder("toy", embedder)
        except VelrError as exc:
            if _missing_vector_symbol(exc):
                pytest.skip("loaded runtime does not expose vector embedder ABI")
            raise

        db.run(
            """
            CREATE
              (:Paper {
                title: 'Alpha Paper',
                abstract: 'alpha graph',
                rank: 7,
                active: true,
                published: date('2024-05-01'),
                tags: ['graph', 'alpha']
              }),
              (:Paper {
                title: 'Beta Paper',
                abstract: 'beta graph',
                rank: 8,
                active: false,
                published: date('2024-05-02'),
                tags: ['graph', 'beta']
              })
            """
        )
        try:
            db.run(
                """
                CREATE VECTOR INDEX paperEmbedding IF NOT EXISTS
                FOR (n:Paper)
                ON EACH [n.title, n.abstract, n.rank, n.active, n.published, n.tags]
                OPTIONS {
                  indexConfig: {
                    dimensions: 3,
                    metric: 'cosine',
                    embedder: 'toy'
                  }
                }
                """
            )
        except VelrError as exc:
            if _vector_feature_unavailable(exc):
                pytest.skip("loaded runtime does not include vector-usearch")
            raise

        try:
            table = db.exec_one(
                """
                CALL db.index.vector.queryNodes('paperEmbedding', 1, 'alpha query')
                YIELD node, score
                RETURN node, score
                """
            )
        except VelrError as exc:
            if _vector_feature_unavailable(exc):
                pytest.skip("loaded runtime does not include vector-usearch")
            raise

        with table:
            with table.iter_rows() as rows:
                got = list(rows)
        assert len(got) == 1
        assert got[0][1].as_python() > 0

    assert any(
        dimensions == 3
        and purpose == "index_entity"
        and entity_kind == "node"
        and field_names
        == [
            ("title", "string", "Alpha Paper", "Alpha Paper"),
            ("abstract", "string", "alpha graph", "alpha graph"),
            ("rank", "int64", 7, 7),
            ("active", "bool", True, True),
            ("published", "date", "2024-05-01", "2024-05-01"),
            ("tags", "list", ["graph", "alpha"], ["graph", "alpha"]),
        ]
        and text == "Alpha Paper\nalpha graph"
        for _, dimensions, purpose, entity_kind, _, field_names, text in seen
    ), seen
    assert any(
        dimensions == 3
        and purpose == "query"
        and entity_kind is None
        and entity_id is None
        and field_names == [(None, "string", "alpha query", "alpha query")]
        and text == "alpha query"
        for _, dimensions, purpose, entity_kind, entity_id, field_names, text in seen
    ), seen


def test_vector_stored_embedding_public_api_uses_bound_lists(tmp_path):
    with Velr.open(str(tmp_path / "stored-vector.sqlite")) as db:
        db.bind_records(
            "_points",
            [
                {"title": "Alpha Chunk", "embedding": [1.0, 0.0, 0.0]},
                {"title": "Beta Chunk", "embedding": [0.0, 1.0, 0.0]},
                {"title": "Gamma Chunk", "embedding": [0.0, 0.0, 1.0]},
            ],
        )
        db.run(
            """
            UNWIND BIND('_points') AS row
            CREATE (:Chunk {title: row.title, embedding: row.embedding})
            """
        )
        try:
            db.run(
                """
                CREATE VECTOR INDEX chunkEmbedding IF NOT EXISTS
                FOR (n:Chunk)
                ON (n.embedding)
                OPTIONS {
                  indexConfig: {
                    `vector.dimensions`: 3,
                    `vector.similarity_function`: 'cosine'
                  }
                }
                """
            )
        except VelrError as exc:
            if _vector_feature_unavailable(exc):
                pytest.skip("loaded runtime does not include vector-usearch")
            raise

        try:
            table = db.exec_one(
                """
                CALL db.index.vector.queryNodes('chunkEmbedding', 1, $embedding)
                YIELD node, score
                RETURN node, score
                """,
                params={"embedding": [0.0, 1.0, 0.0]},
            )
        except VelrError as exc:
            if _missing_options_symbol(exc):
                pytest.skip("loaded runtime does not expose query params/options ABI")
            if _vector_feature_unavailable(exc):
                pytest.skip("loaded runtime does not include vector-usearch")
            if _stored_vector_query_unavailable(exc):
                pytest.skip("loaded runtime does not support direct vector query values")
            raise

        with table:
            with table.iter_rows() as rows:
                got = list(rows)

    assert len(got) == 1
    assert _title_from_node_cell(got[0][0]) == "Beta Chunk"
    assert got[0][1].as_python() > 0
