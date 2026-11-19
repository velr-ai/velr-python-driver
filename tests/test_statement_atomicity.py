import pytest

from velr.driver import Velr, VelrError


@pytest.mark.parametrize("method", ["run", "exec", "exec_one", "to_rows", "to_records"])
@pytest.mark.parametrize(
    "query",
    [
        "CREATE (:Order {id: 1}) RETURN 1 / 0 AS value",
        "CREATE (n:Order {id: 1, quantity: 0}) RETURN 10 / n.quantity AS value",
        "MATCH (a:Acct {name: 'a'}) SET a.balance = 50 RETURN a.balance / 0 AS value",
        "MATCH (a:Acct {name: 'b'}) DETACH DELETE a RETURN a.balance AS value",
        "CREATE (:Order {id: 1}) WITH 1 / 0 AS value RETURN value",
    ],
)
def test_failed_autocommit_statement_preserves_graph(tmp_path, method, query):
    path = str(tmp_path / "orders.velr")
    with Velr.open(path) as db:
        db.run("CREATE (:Acct {name: 'a', balance: 100}), (:Acct {name: 'b', balance: 0})")
        with pytest.raises(VelrError):
            if method == "exec":
                with db.exec(query) as stream:
                    while (table := stream.next_table()) is not None:
                        with table:
                            table.to_rows()
            elif method == "exec_one":
                with db.exec_one(query) as table:
                    table.to_rows()
            else:
                getattr(db, method)(query)

    with Velr.open(path) as db:
        assert db.to_rows("MATCH (n:Order) RETURN count(n)") == [[0]]
        assert db.to_rows("MATCH (a:Acct) RETURN a.name, a.balance ORDER BY a.name") == [
            ["a", 100],
            ["b", 0],
        ]


def test_failed_statement_preserves_earlier_and_later_transaction_writes(tmp_path):
    path = str(tmp_path / "accounts.velr")
    with Velr.open(path) as db:
        db.run("CREATE (:Acct {name: 'a', balance: 100}), (:Acct {name: 'b', balance: 0})")
        tx = db.begin_tx()
        tx.run("CREATE (:Log {id: 1})")
        with pytest.raises(VelrError, match="DivisionByZero"):
            tx.run(
                "MATCH (a:Acct {name: 'a'}) SET a.balance = a.balance - 50 "
                "WITH a MATCH (b:Acct {name: 'b'}) SET b.balance = b.balance + (50 / 0)"
            )
        with tx.exec_one("MATCH (a:Acct) RETURN a.name, a.balance ORDER BY a.name") as table:
            assert table.to_rows() == [["a", 100], ["b", 0]]
        tx.run("CREATE (:Log {id: 2})")
        tx.commit()

    with Velr.open(path) as db:
        assert db.to_rows("MATCH (a:Acct) RETURN a.name, a.balance ORDER BY a.name") == [
            ["a", 100],
            ["b", 0],
        ]
        assert db.to_rows("MATCH (l:Log) RETURN l.id ORDER BY l.id") == [[1], [2]]
