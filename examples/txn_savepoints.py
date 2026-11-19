"""Commit a transaction and use scoped and named savepoints explicitly."""

from velr.driver import Velr


def names(db):
    return [row["name"] for row in db.to_records(
        "MATCH (p:Person) RETURN p.name AS name ORDER BY name"
    )]


def main():
    with Velr.open() as db:
        with db.begin_tx() as tx:
            tx.run("CREATE (:Person {name:'Alice'})")
            with tx.savepoint() as discarded:
                tx.run("CREATE (:Person {name:'Draft'})")
                discarded.rollback()  # Discard Draft and release the savepoint.
            with tx.savepoint() as kept:
                tx.run("CREATE (:Person {name:'Grace'})")
                kept.release()
            tx.commit()
        assert names(db) == ["Alice", "Grace"]
        print("after commit:", names(db))

        with db.begin_tx() as tx:
            tx.savepoint_named("before_import").close()
            tx.run("CREATE (:Person {name:'Discarded'})")
            tx.rollback_to("before_import")
            tx.run("CREATE (:Person {name:'Bob'})")
            tx.release_savepoint("before_import")
            tx.commit()
        assert names(db) == ["Alice", "Bob", "Grace"]
        print("after named savepoint:", names(db))

        with db.begin_tx() as tx:
            tx.run("CREATE (:Person {name:'Uncommitted'})")
            # Context exit without commit rolls back the transaction.

        try:
            with db.begin_tx() as tx:
                tx.run("CREATE (:Person {name:'Failed'})")
                raise RuntimeError("discard this transaction")
        except RuntimeError:
            pass
        assert names(db) == ["Alice", "Bob", "Grace"]
        print("after rollbacks:", names(db))


if __name__ == "__main__":
    main()
