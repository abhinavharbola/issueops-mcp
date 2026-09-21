import pytest

from dashboard import auth


def test_the_correct_token_matches():
    assert auth.token_matches("s3cret", "s3cret") is True


@pytest.mark.parametrize("entered", ["", None, "wrong", "s3cret ", "S3CRET"])
def test_wrong_or_empty_tokens_do_not_match(entered):
    assert auth.token_matches(entered, "s3cret") is False


def test_nothing_matches_when_no_token_is_configured():
    assert auth.token_matches("anything", None) is False
    assert auth.token_matches("", "") is False


def test_recent_drops_failures_outside_the_window():
    assert auth.recent([1.0, 51.0, 100.0], now=110.0, window=60) == [51.0, 100.0]


def test_a_session_locks_after_the_maximum_number_of_recent_failures():
    now = 1000.0
    failures = [now - i for i in range(auth.MAX_TOKEN_FAILURES)]

    assert auth.is_locked(failures, now) is True


def test_a_session_unlocks_once_the_failures_age_out():
    failures = [100.0] * auth.MAX_TOKEN_FAILURES

    assert auth.is_locked(failures, now=100.0 + auth.TOKEN_FAILURE_WINDOW_SECONDS + 1) is False


def test_failures_below_the_limit_do_not_lock():
    assert auth.is_locked([999.0] * (auth.MAX_TOKEN_FAILURES - 1), now=1000.0) is False


def test_the_delay_grows_with_recent_failures_but_is_capped():
    assert auth.failure_delay_seconds(0) == 0
    assert auth.failure_delay_seconds(4) == 4 * auth.DELAY_PER_RECENT_FAILURE_SECONDS
    assert auth.failure_delay_seconds(10_000) == auth.MAX_FAILURE_DELAY_SECONDS
