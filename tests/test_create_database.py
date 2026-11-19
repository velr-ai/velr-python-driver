from contextlib import closing
import sqlite3

import pytest

from velr._ffi import lib
from velr.driver import Velr, VelrError


pytestmark = pytest.mark.skipif(
    not hasattr(lib, "velr_create") or not hasattr(lib, "velr_inline_edge_properties_json"),
    reason="the loaded runtime predates the explicit database creation API",
)


def test_create_reserves_slot_and_preserves_values_on_reopen(tmp_path):
    path = tmp_path / "events.db"
    large_value = "x" * 129
    with Velr.create(str(path), inline_edge_properties=["timestamp"]) as db:
        assert db.inline_edge_properties() == ["timestamp"]
        db.run("CREATE (n:Anchor)-[:EVENT {timestamp: 1700}]->()")
        db.run(
            "MATCH (n:Anchor) CREATE (n)-[:EVENT {timestamp: $value}]->()",
            params={"value": large_value},
        )
        with db.begin_tx() as tx:
            tx.run("MATCH ()-[r:EVENT]->() SET r.timestamp = 99")
            tx.rollback()

    with Velr.open_readonly(str(path)) as db:
        assert db.inline_edge_properties() == ["timestamp"]
        with db.exec_one("MATCH ()-[r:EVENT]->() RETURN r.timestamp ORDER BY id(r)") as table:
            assert table.to_rows() == [[1700], [large_value]]

    with closing(sqlite3.connect(path)) as sqlite:
        assert sqlite.execute("SELECT __vrs_p0 FROM edge ORDER BY id").fetchall() == [
            (1700,),
            (None,),
        ]
        assert sqlite.execute("SELECT value FROM __vrs_edge_property").fetchall() == [
            (large_value,),
        ]


def test_create_preserves_exact_names_and_defaults_to_zero_slots():
    with Velr.create() as db:
        assert db.inline_edge_properties() == []
    with Velr.create(":memory:", inline_edge_properties=["sales,net"]) as db:
        assert db.inline_edge_properties() == ["sales,net"]


def test_create_rejects_existing_file_and_invalid_lists(tmp_path):
    existing = tmp_path / "existing.db"
    existing.touch()
    with pytest.raises(VelrError):
        Velr.create(str(existing), inline_edge_properties=["timestamp"])
    assert existing.read_bytes() == b""

    path = tmp_path / "new.db"
    for names in ([""], ["a\0b"], ["timestamp", "amount"], ["timestamp", "timestamp"]):
        with pytest.raises(VelrError):
            Velr.create(str(path), inline_edge_properties=names)
        assert not path.exists()
    with pytest.raises(TypeError):
        Velr.create(str(path), inline_edge_properties="timestamp")
