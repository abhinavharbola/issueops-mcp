import os

TITLE_CHARS = 300
BODY_CHARS = 8000
COMMENT_CHARS = 1500
MAX_COMMENTS = 10
COMMENTS_TOTAL_CHARS = 6000

COMMENT_BODY_MAX_CHARS_DEFAULT = 65536
COMMENT_BODY_MAX_CHARS_CEILING = 65536
MAX_PENDING_PER_ISSUE_DEFAULT = 10
MAX_PENDING_PER_INITIATOR_DEFAULT = 500


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}[truncated {len(text) - limit} chars]"


def positive_int_from_env(name: str, default: int, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got: {raw!r}")
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer, got: {value}")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} must be at most {maximum}, got: {value}")
    return value
