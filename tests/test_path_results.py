import json

from velr.driver import Path, Velr


def test_paths_keep_their_type_in_materialized_python_results():
    with Velr.open() as db:
        db.run("CREATE (:Start {name:'a'})-[:LINK]->(:End {name:'b'})")
        query = """
            MATCH p=(a:Start)-[r:LINK]->(b:End)
            RETURN p AS path, [a,r,b] AS ordinary,
                   {nested:[p, [a,r,b]], elements:[1]} AS bundle
        """
        [row] = db.to_records(query)
        path = row["path"]
        assert isinstance(path, Path)
        assert isinstance(row["ordinary"], list)
        assert path.elements == tuple(row["ordinary"])
        assert row["bundle"] == {"nested": [path, row["ordinary"]], "elements": [1]}

        with db.exec_one(query) as table:
            [plain] = json.loads(table._to_json_rows_bytes())
            ordinary = row["ordinary"]
            assert plain == [ordinary, ordinary, {"nested": [ordinary, ordinary], "elements": [1]}]

        [[zero, shortest]] = db.to_rows("""
            MATCH (a:Start), (b:End)
            MATCH p=(a)-[:LINK*0..0]->(a)
            RETURN p, shortestPath((a)-[:LINK*]->(b))
        """)
        assert isinstance(zero, Path)
        assert zero.elements == (path.elements[0],)
        assert shortest == path

    assert path.elements[2]["properties"]["name"] == "b"
