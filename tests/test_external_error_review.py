"""Replay the external report's two Python-only parameter cases."""

import pytest

from velr.driver import Velr


@pytest.mark.parametrize(
    ("params", "error", "message"),
    [
        ({"a": {1, 2}}, TypeError, "unsupported query parameter value type: set"),
        ({"a": 99999999999999999999}, ValueError, "integer query parameters must fit Cypher INTEGER"),
    ],
    ids=["par-type", "par-bigint"],
)
def test_invalid_parameter_fails_before_query_execution(params, error, message):
    with Velr.open(None) as db, pytest.raises(error, match=message):
        with db.exec("RETURN $a", params=params):
            pytest.fail("invalid Python parameter reached query execution")
