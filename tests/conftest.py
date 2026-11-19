import os
import pytest

from velr.driver import Velr, VelrError

@pytest.fixture()
def db():
    # fresh in-memory DB for every test -> no cross-test accumulation
    return Velr.open(None)

@pytest.fixture
def sample_all_scalars(db: Velr):
    """
    One row that exercises all Velr primitive kinds + JSON (stringified).
    """
    seed = """
    CREATE (:Sample {
        n: null,
        b: true,
        i: 123,
        f: 3.75,
        ta: 'hello',
        tb: 'false',
        arr: ['a','b']
    });
    """
    with db.exec(seed) as st:
        # drain planning tables, if any
        while True:
            t = st.next_table()
            if t is None: break
            t.close()
    yield
