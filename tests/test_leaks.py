import gc
from velr.driver import Velr

def test_handle_churn_no_crash():
    # churn lots of handles across FFI boundaries
    for _ in range(200):
        with Velr.open(None) as db:
            with db.exec("MATCH (t) RETURN t") as st:
                while True:
                    t = st.next_table()
                    if t is None: break
                    t.close()
            with db.begin_tx() as tx:
                tx.savepoint()  # leaked on purpose; RAII must close it before tx closes
        gc.collect()

def test_tx_sp_create_drop_cycles():
    # try to trigger odd GC orderings
    with Velr.open(None) as db:
        for _ in range(100):
            tx = db.begin_tx()
            sp = tx.savepoint()
            del tx   # SP holds strong parent -> TX must not be dropped first
            gc.collect()
            del sp
            gc.collect()
