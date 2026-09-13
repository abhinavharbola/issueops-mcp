HEURISTIC_PHRASES = [
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard previous instructions",
    "you must",
    "system prompt",
    "new instructions:",
]


def is_heuristically_flagged(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in HEURISTIC_PHRASES)
