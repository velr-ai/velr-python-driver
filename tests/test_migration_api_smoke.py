import pytest

from velr.driver import Velr, VelrError


def test_migration_api_reports_current_in_memory_database():
    with Velr.open(None) as db:
        try:
            schema_version = db.schema_version()
        except VelrError as e:
            if "does not expose velr_schema_version" in str(e):
                pytest.skip(str(e))
            raise

        current_schema_version = db.current_schema_version()
        assert schema_version == current_schema_version
        assert db.needs_migration() is False

        report = db.migrate()
        assert report.from_version == schema_version
        assert report.to_version == current_schema_version
        assert report.status == "already_current"
        assert report.steps == []


def test_vacuum_api_compacts_file_backed_database(tmp_path):
    with Velr.open(str(tmp_path / "vacuum.sqlite")) as db:
        db.run(
            """
            CREATE (:Tmp {id: 1, payload: 'alpha'});
            CREATE (:Tmp {id: 2, payload: 'beta'});
            MATCH (n:Tmp {id: 1}) DETACH DELETE n;
            """
        )
        try:
            db.vacuum()
        except VelrError as e:
            if "does not expose velr_vacuum" in str(e):
                pytest.skip(str(e))
            raise

        with db.exec_one("MATCH (n:Tmp) RETURN count(n) AS n") as table:
            assert table.to_rows() == [[1]]
