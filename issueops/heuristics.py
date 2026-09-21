import re
import unicodedata

HEURISTIC_PHRASES = [
    "ignore previous instructions",
    "ignore all previous instructions",
    "ignore the above",
    "disregard previous instructions",
    "disregard the above",
    "new instructions:",
    "system prompt",
    "you are now",
    "act as if",
    "do not tell the user",
    "this is not a drill",
    "override your instructions",
]

_INVISIBLE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text)
    return _WHITESPACE.sub(" ", text).lower()


def flag_matches(text: str) -> list[str]:
    normalized = _normalize(text)
    return [phrase for phrase in HEURISTIC_PHRASES if phrase in normalized]


def is_heuristically_flagged(text: str) -> bool:
    return bool(flag_matches(text))
