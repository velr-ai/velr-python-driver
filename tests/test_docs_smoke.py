from velr.driver import Velr

MOVIES_CREATE = r"""
CREATE
  (keanu:Person:Actor {name:'Keanu Reeves', born:1964}),
  (nolan:Person:Director {name:'Christopher Nolan'}),
  (matrix:Movie {title:'The Matrix', released:1999, genres:['Sci-Fi','Action']}),
  (inception:Movie {title:'Inception', released:2010, genres:['Sci-Fi','Heist']}),
  (keanu)-[:ACTED_IN {roles:['Neo']}]->(matrix),
  (nolan)-[:DIRECTED]->(inception);
"""


def test_doc_example_exec_one():
    with Velr.open(None) as db:
        db.run(MOVIES_CREATE)

        with db.exec_one(
            "MATCH (m:Movie {title:'Inception'}) "
            "RETURN m.title AS title, m.released AS y, m.genres AS g"
        ) as table:
            assert table.column_names() == ["title", "y", "g"]

            with table.iter_rows() as rows:
                row = next(rows)

        title, year, genres = row
        assert title.as_python() == "Inception"
        assert year.as_python() == 2010
        assert genres.as_python() == '["Sci-Fi","Heist"]'


def test_doc_example_to_pandas():
    pytest = __import__("pytest")
    pytest.importorskip("pyarrow")
    pytest.importorskip("pandas")

    with Velr.open(None) as db:
        db.run(MOVIES_CREATE)

        df = db.to_pandas(
            "MATCH (m:Movie) "
            "RETURN m.title AS title, m.released AS released "
            "ORDER BY released"
        )

        assert list(df.columns) == ["title", "released"]
        assert df.shape == (2, 2)
        assert df["title"].tolist() == ["The Matrix", "Inception"]
        assert df["released"].tolist() == [1999, 2010]


def test_doc_example_transaction():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:Movie {title:'Interstellar', released:2014})")
            tx.commit()

        with db.exec_one(
            "MATCH (m:Movie {title:'Interstellar'}) "
            "RETURN m.title AS title, m.released AS released"
        ) as table:
            assert table.column_names() == ["title", "released"]

            with table.iter_rows() as rows:
                row = next(rows)

        title, released = row
        assert title.as_python() == "Interstellar"
        assert released.as_python() == 2014