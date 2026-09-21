import secrets

MAX_TOKEN_FAILURES = 5
TOKEN_FAILURE_WINDOW_SECONDS = 60
DELAY_PER_RECENT_FAILURE_SECONDS = 0.5
MAX_FAILURE_DELAY_SECONDS = 10.0


def token_matches(entered: str | None, expected: str | None) -> bool:
    if not entered or not expected:
        return False
    return secrets.compare_digest(entered.encode("utf-8"), expected.encode("utf-8"))


def recent(failures: list[float], now: float, window: float = TOKEN_FAILURE_WINDOW_SECONDS) -> list[float]:
    cutoff = now - window
    return [t for t in failures if t > cutoff]


def is_locked(
    failures: list[float], now: float, max_failures: int = MAX_TOKEN_FAILURES,
    window: float = TOKEN_FAILURE_WINDOW_SECONDS,
) -> bool:
    return len(recent(failures, now, window)) >= max_failures


def failure_delay_seconds(recent_failures_across_sessions: int) -> float:
    return min(recent_failures_across_sessions * DELAY_PER_RECENT_FAILURE_SECONDS, MAX_FAILURE_DELAY_SECONDS)
