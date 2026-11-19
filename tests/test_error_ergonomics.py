"""Public error aliases and tuple-compatible source positions."""

import pickle
import subprocess
import sys

import pytest

import velr
import velr.driver as driver
from velr import CypherSyntaxError, ErrorPosition, Velr, VelrError


def test_package_import_and_discovery_do_not_load_the_driver_or_native_runtime():
    code = r'''
import sys

class NoNativeImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"velr.driver", "velr._ffi", "cffi"}:
            raise AssertionError(f"Unexpected driver import: {fullname}")

sys.meta_path.insert(0, NoNativeImports())
import velr
import velr.guide
import velr.query_language
assert "velr.driver" not in sys.modules
assert "velr._ffi" not in sys.modules
assert set(velr.__all__).issubset(dir(velr))
assert not hasattr(velr, "unknown_public_name")
try:
    velr.Velr
except AssertionError as error:
    assert "Unexpected driver import: velr.driver" in str(error)
else:
    raise AssertionError("Accessing a driver name must import the driver")
assert "Velr" not in vars(velr)
'''
    subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True,
        text=True, timeout=30,
    )


def test_package_root_exports_are_identical_to_driver_exports():
    assert velr.__all__ == driver.__all__
    namespace = {}
    exec("from velr import *", namespace)
    for name in driver.__all__:
        assert getattr(velr, name) is getattr(driver, name)
        assert namespace[name] is getattr(driver, name)
    with Velr.open() as db:
        assert db.to_rows("RETURN 42") == [[42]]


@pytest.mark.parametrize("args", [(), ("original report",), ("original report", 7)])
def test_text_properties_preserve_runtime_error_args_and_display(args):
    error = VelrError(*args, message="short explanation", position=(2, 7))
    original = str(RuntimeError(*args))
    assert error.args == args
    assert str(error) == original
    assert error.report == original
    assert error.explanation == error.message == "short explanation"
    error.message = "updated explanation"
    assert error.explanation == "updated explanation"
    assert error.report == original


def test_named_positions_preserve_tuple_behavior_and_pickling():
    position = ErrorPosition(2, 7)
    error = CypherSyntaxError(
        "original report", message="short explanation", position=position,
    )
    assert error.position is position
    assert (position.line, position.column) == (2, 7)
    assert position == (2, 7)
    assert position[0] == 2
    line, column = position
    assert (line, column) == (2, 7)
    with pytest.raises(AttributeError):
        position.line = 3
    restored = pickle.loads(pickle.dumps(error))
    assert restored.args == error.args
    assert restored.report == error.report
    assert restored.explanation == error.explanation
    assert type(restored.position) is ErrorPosition
    assert restored.position == position
    assert type(pickle.loads(pickle.dumps(position))) is ErrorPosition
    assert VelrError("unknown").position is None
    assert type(VelrError("tuple", position=(1, 3)).position) is ErrorPosition


def test_native_error_exposes_named_position_and_both_diagnostic_texts():
    with Velr.open() as db, pytest.raises(CypherSyntaxError) as raised:
        db.run("WITH '界' AS text\nRETURN missing")
    error = raised.value
    assert type(error.position) is ErrorPosition
    assert error.position.line == 2
    assert error.position.column == 8
    assert error.position == (2, 8)
    assert error.explanation == error.message
    assert error.report == str(error) == error.args[0]
    assert error.explanation != error.report
    assert "missing" in error.explanation
