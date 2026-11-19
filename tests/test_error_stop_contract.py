import pytest

from velr.driver import DeadlineExceeded, QueryCancelled, VelrError


@pytest.mark.parametrize("exception_type", [QueryCancelled, DeadlineExceeded])
def test_explicit_query_stop_cannot_be_made_retryable(exception_type):
    error = exception_type("original diagnostic", retryable=True)
    assert error.retryable is False
    assert str(error) == "original diagnostic"


@pytest.mark.parametrize("status", [-5, -6])
def test_native_stop_status_overrides_optional_retryability(status):
    error = VelrError("original diagnostic", status_code=status, retryable=True)
    assert error.retryable is False
