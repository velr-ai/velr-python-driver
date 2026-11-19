import json
import math
import subprocess
import sys
import textwrap

import pytest

from velr.driver import Velr


def test_plain_python_rows_preserve_top_level_nonfinite_floats():
    query = "RETURN 0.0 / 0.0 AS nan, 1.0 / 0.0 AS positive, -1.0 / 0.0 AS negative, null AS missing"
    with Velr.open() as db:
        [[nan, positive, negative, missing]] = db.to_rows(query)
        [record] = db.to_records(query)
        assert math.isnan(nan)
        assert math.isnan(record["nan"])
        assert positive == record["positive"] == math.inf
        assert negative == record["negative"] == -math.inf
        assert missing is record["missing"] is None


def test_plain_python_rows_and_records_preserve_nested_nonfinite_floats():
    query = """
        WITH 0.0 / 0.0 AS nan, 1.0 / 0.0 AS positive, -1.0 / 0.0 AS negative
        RETURN [nan, {items: [positive, negative, null]}] AS floats,
               {value: nan, text: 'Infinity'} AS mapping
    """
    with Velr.open() as db:
        [[floats, mapping]] = db.to_rows(query)
        [record] = db.to_records(query)
        for result in (floats, record["floats"]):
            assert math.isnan(result[0])
            assert result[1] == {"items": [math.inf, -math.inf, None]}
        for result in (mapping, record["mapping"]):
            assert math.isnan(result["value"])
            assert result["text"] == "Infinity"
        with db.exec_one(query) as table:
            assert json.loads(table._to_json_rows_bytes()) == [
                [[None, {"items": [None, None, None]}], {"value": None, "text": "Infinity"}]
            ]


def test_write_returns_nested_nonfinite_values_and_commits_once():
    with Velr.open() as db:
        with db.exec_one("CREATE (:Measure) RETURN [1.0 / 0.0] AS values") as table:
            assert table.to_records() == [{"values": [math.inf]}]
        assert db.to_rows("MATCH (n:Measure) RETURN count(n)") == [[1]]


def test_deep_parameter_map_raises_a_validation_error():
    value = 1
    for _ in range(1000):
        value = {"key": value}
    with Velr.open() as db:
        with pytest.raises(ValueError, match="nested at most 64 levels"):
            db.exec_one("RETURN $value", params={"value": value})


def test_expression_depth_on_a_small_host_thread_stack():
    script = textwrap.dedent(
        """
        import threading
        from velr.driver import Velr, VelrError

        def run():
            with Velr.open() as db:
                for opening, closing in [('[', ']'), ('CASE WHEN true THEN ', ' END'), ('(', ')')]:
                    db.to_rows('RETURN ' + opening * 40 + '1' + closing * 40)
                    try:
                        db.to_rows('RETURN ' + opening * 140 + '1' + closing * 140)
                    except VelrError as error:
                        assert 'QueryTooComplex' in str(error)
                    else:
                        raise AssertionError('expected depth error')
                query = 'UNWIND $rows AS row RETURN ' + '[' * 40 + 'row.value' + ']' * 40
                with db.exec_one(query, params={'rows': [{'value': 1}]}) as table:
                    [[value]] = table.to_rows()
                    for _ in range(40):
                        [value] = value
                    assert value == 1

        threading.stack_size(512 * 1024)
        failures = []
        threading.excepthook = lambda args: failures.append(args.exc_value)
        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        if failures:
            raise failures[0]
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
