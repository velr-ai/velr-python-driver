# tests/test_python_driver_site_examples_exact.py

from __future__ import annotations

from pathlib import Path
import tempfile

import pytest

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


def _tmp_db_path() -> str:
    p = Path(tempfile.gettempdir()) / "velr-python-driver-site-examples.db"
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    return str(p)


def test_open_in_memory_exact():
    from velr.driver import Velr

    with Velr.open(None) as db:
        db.run("CREATE (:Person {name:'Alice'})")


def test_open_file_backed_exact():
    from velr.driver import Velr

    with Velr.open(_tmp_db_path()) as db:
        db.run("CREATE (:Person {name:'Alice'})")


def test_run_exact():
    from velr.driver import Velr

    with Velr.open(None) as db:
        db.run("CREATE (:Movie {title:'Interstellar', released:2014})")


def test_exec_exact():
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

    with Velr.open(None) as db:
        db.run(MOVIES_CREATE)

        with db.exec(
            "MATCH (m:Movie {title:'The Matrix'}) RETURN m.title AS title; "
            "MATCH (m:Movie {title:'Inception'}) RETURN m.released AS released"
        ) as stream:
            for table in stream.iter_tables():
                print(table.column_names())
                print(table.collect(lambda row: [cell.as_python() for cell in row]))


def test_exec_one_exact():
    from velr.driver import Velr

    with Velr.open(None) as db:
        db.run("CREATE (:Person {name:'Alice', age:30})")

        with db.exec_one("MATCH (p:Person) RETURN p.name AS name, p.age AS age") as table:
            print(table.column_names())
            print(table.collect(lambda row: [cell.as_python() for cell in row]))


def test_reading_results_exact():
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

    with Velr.open(None) as db:
        db.run(MOVIES_CREATE)

        with db.exec_one(
            "MATCH (m:Movie {title:'Inception'}) "
            "RETURN m.title AS title, m.released AS year, m.genres AS genres"
        ) as table:
            print(table.column_names())
            print(table.column_count())

            with table.iter_rows() as rows:
                row = next(rows)

        title, year, genres = row
        print(title.as_python())
        print(year.as_python())
        print(genres.as_python())


def test_cell_as_python_exact():
    with Velr.open(None) as db:
        db.run("CREATE (:Person {name:'Alice', age:30})")

        with db.exec_one("MATCH (p:Person) RETURN p.name AS name, p.age AS age") as table:
            with table.iter_rows() as rows:
                for row in rows:
                    print(row[0].as_python(), row[1].as_python())


def test_table_lifetime_exec_exact():
    with Velr.open(None) as db:
        db.run("CREATE (:Node {name:'n1'})")

        with db.exec("MATCH (n) RETURN n") as stream:
            table = stream.next_table()
            assert table is not None
            # table is valid here
            with table:
                print(table.column_names())


def test_table_lifetime_exec_one_exact():
    with Velr.open(None) as db:
        db.run("CREATE (:Node {name:'n1'})")

        table = db.exec_one("MATCH (n) RETURN n")
        with table:
            print(table.column_names())


def test_begin_tx_exact():
    from velr.driver import Velr

    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:Movie {title:'Interstellar', released:2014})")
            tx.commit()


def test_tx_exec_one_exact():
    from velr.driver import Velr

    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:Movie {title:'Interstellar', released:2014})")

            with tx.exec_one("MATCH (m:Movie) RETURN count(m) AS c") as table:
                with table.iter_rows() as rows:
                    for (count_cell,) in rows:
                        print("Count:", count_cell.as_python())

            tx.commit()


def test_commit_rollback_exact():
    with Velr.open(None) as db:
        tx = db.begin_tx()
        tx.rollback()

    with Velr.open(None) as db:
        tx = db.begin_tx()
        tx.commit()


def test_scoped_savepoint_exact():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:Temp {k:'outer'})")

            with tx.savepoint() as sp:
                tx.run("CREATE (:Temp {k:'inner'})")
                sp.rollback()

            tx.commit()


def test_named_savepoint_exact():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            with tx.savepoint_named("sp1") as sp:
                tx.run("CREATE (:Temp {k:'inner'})")
                sp.rollback()  # rollback to savepoint, then release it

            tx.commit()


def test_rollback_to_named_savepoint_exact():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:Temp {k:'outer'})")

            tx.savepoint_named("sp1")
            tx.run("CREATE (:Temp {k:'inner'})")

            tx.rollback_to("sp1")
            tx.commit()


def test_to_pandas_exact():
    pandas = pytest.importorskip("pandas")

    with Velr.open(None) as db:
        db.run(MOVIES_CREATE)

        df = db.to_pandas(
            "MATCH (m:Movie) "
            "RETURN m.title AS title, m.released AS released "
            "ORDER BY released"
        )
        print(df)
        assert isinstance(df, pandas.DataFrame)


def test_to_polars_exact():
    pl = pytest.importorskip("polars")

    with Velr.open(None) as db:
        db.run(MOVIES_CREATE)

        df = db.to_polars(
            "MATCH (m:Movie) "
            "RETURN m.title AS title, m.released AS released "
            "ORDER BY released"
        )
        print(df)
        assert isinstance(df, pl.DataFrame)


def test_to_pyarrow_exact():
    pa = pytest.importorskip("pyarrow")

    with Velr.open(None) as db:
        db.run(MOVIES_CREATE)

        tbl = db.to_pyarrow(
            "MATCH (m:Movie) "
            "RETURN m.title AS title, m.released AS released "
            "ORDER BY released"
        )
        print(tbl)
        assert isinstance(tbl, pa.Table)


def test_export_from_existing_table_exact():
    pa = pytest.importorskip("pyarrow")
    pandas = pytest.importorskip("pandas")
    pl = pytest.importorskip("polars")

    with Velr.open(None) as db:
        db.run(MOVIES_CREATE)

        with db.exec_one("MATCH (m:Movie) RETURN m.title AS title") as table:
            pa_tbl = table.to_pyarrow()
            df = table.to_pandas()
            pl_df = table.to_polars()

        assert isinstance(pa_tbl, pa.Table)
        assert isinstance(df, pandas.DataFrame)
        assert isinstance(pl_df, pl.DataFrame)


def test_bind_pandas_exact():
    pd = pytest.importorskip("pandas")
    from velr.driver import Velr

    df = pd.DataFrame(
        [
            {"name": "Alice", "age": 30},
            {"name": "Bob", "age": 41},
        ]
    )

    with Velr.open(None) as db:
        db.bind_pandas("_people", df)

        db.run("""
        UNWIND BIND('_people') AS r
        CREATE (:Person {name:r.name, age:r.age})
        """)

        out = db.to_pandas("MATCH (p:Person) RETURN p.name AS name, p.age AS age ORDER BY age")
        print(out)


def test_bind_records_exact():
    rows = [
        {"name": "Alice", "age": 30},
        {"name": "Bob", "age": 41},
    ]

    with Velr.open(None) as db:
        db.bind_records("_people", rows)
        db.run("""
        UNWIND BIND('_people') AS r
        CREATE (:Person {name:r.name, age:r.age})
        """)


def test_bind_arrow_exact():
    pa = pytest.importorskip("pyarrow")

    with Velr.open(None) as db:
        tbl = pa.table({
            "name": ["Alice", "Bob"],
            "age": [30, 25],
        })

        db.bind_arrow("_people", tbl)
        db.run("""
        UNWIND BIND('_people') AS r
        CREATE (:Person {name:r.name, age:r.age})
        """)


def test_bind_polars_exact():
    pl = pytest.importorskip("polars")

    with Velr.open(None) as db:
        df = pl.DataFrame(
            {"name": ["Alice", "Bob"], "active": [True, False]}
        )

        db.bind_polars("_users_pl", df)

        db.run("""
        UNWIND BIND('_users_pl') AS r
        CREATE (:User {name: r.name, active: r.active})
        """)


def test_bind_numpy_exact():
    np = pytest.importorskip("numpy")

    with Velr.open(None) as db:
        data = {
            "name": np.array(["Alice", "Bob"], dtype="object"),
            "age": np.array([30, 25], dtype="int64"),
        }

        db.bind_numpy("_np_people", data)

        db.run("""
        UNWIND BIND('_np_people') AS r
        CREATE (:Person {name:r.name, age:r.age})
        """)


def test_explain_exact():
    with Velr.open(None) as db:
        with db.explain("MATCH (p:Person) RETURN p.name AS name") as xp:
            print(xp.to_compact_string())


def test_explain_analyze_exact():
    with Velr.open(None) as db:
        db.run("CREATE (:Person {name:'Alice'})")

        with db.explain_analyze("MATCH (p:Person) RETURN p.name AS name") as xp:
            print(xp.to_compact_string())


def test_tx_explain_exact():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            with tx.explain("MATCH (p:Person) RETURN p.name AS name") as xp:
                print(xp.to_compact_string())
            tx.rollback()


def test_tx_explain_analyze_exact():
    with Velr.open(None) as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:Person {name:'Alice'})")
            with tx.explain_analyze("MATCH (p:Person) RETURN p.name AS name") as xp:
                print(xp.to_compact_string())
            tx.rollback()