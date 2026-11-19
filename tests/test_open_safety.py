from contextlib import closing
import sqlite3

import pytest

from velr.driver import Velr, VelrError


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_open_rejects_foreign_sqlite_without_modifying_it(tmp_path, journal_mode):
    path = tmp_path / "application.db"
    with closing(sqlite3.connect(path)) as sqlite:
        assert sqlite.execute(f"PRAGMA journal_mode={journal_mode}").fetchone() == (
            journal_mode,
        )
        sqlite.executescript(
            "CREATE TABLE documents(id INTEGER PRIMARY KEY, body TEXT);"
            "INSERT INTO documents VALUES(1, 'application data');"
            "CREATE INDEX documents_body ON documents(body);"
            "CREATE VIEW document_bodies AS SELECT body FROM documents;"
        )
        assert sqlite.execute("PRAGMA user_version").fetchone() == (0,)

    before_bytes = path.read_bytes()
    before_files = set(tmp_path.iterdir())

    with pytest.raises(VelrError):
        with Velr.open(str(path)):
            pytest.fail("an unrelated SQLite database must be rejected")

    assert path.read_bytes() == before_bytes
    assert set(tmp_path.iterdir()) == before_files
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as sqlite:
        assert sqlite.execute("PRAGMA journal_mode").fetchone() == (journal_mode,)
        assert sqlite.execute("PRAGMA user_version").fetchone() == (0,)
        assert sqlite.execute("SELECT * FROM documents").fetchall() == [
            (1, "application data")
        ]
        assert sqlite.execute("SELECT * FROM document_bodies").fetchall() == [
            ("application data",)
        ]


@pytest.mark.parametrize("existing_empty_file", [False, True])
def test_open_initializes_new_or_empty_file_and_reopens(tmp_path, existing_empty_file):
    path = tmp_path / "graph.db"
    if existing_empty_file:
        path.touch()

    with Velr.open(str(path)) as db:
        db.run("CREATE (:Document {body: 'graph data'})")

    with Velr.open(str(path)) as db:
        with db.exec_one("MATCH (n:Document) RETURN n.body") as table:
            assert table.to_rows() == [["graph data"]]

    with Velr.open_readonly(str(path)) as db:
        with db.exec_one("MATCH (n:Document) RETURN n.body") as table:
            assert table.to_rows() == [["graph data"]]
