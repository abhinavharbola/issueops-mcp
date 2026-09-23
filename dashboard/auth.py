import secrets


def token_matches(entered: str | None, expected: str | None) -> bool:
    if not entered or not expected:
        return False
    return secrets.compare_digest(entered.encode("utf-8"), expected.encode("utf-8"))


