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


def is_heuristically_flagged(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in HEURISTIC_PHRASES)



