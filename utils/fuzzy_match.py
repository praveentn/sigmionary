import re
from rapidfuzz import fuzz

_NOISE = re.compile(r"[^\w\s]")

# Feedback bands (score out of 100)
THRESHOLD_CORRECT = 82
THRESHOLD_HOT     = 70   # very close — one or two letters off
THRESHOLD_WARM    = 50   # in the right ballpark


def _norm(text: str) -> str:
    return _NOISE.sub("", text.strip().lower())


def guess_score(user_input: str, answer: str) -> float:
    """Return the best fuzzy similarity score (0–100) between input and answer."""
    u = _norm(user_input)
    a = _norm(answer)
    if not u:
        return 0.0
    if u == a:
        return 100.0
    scores = [
        fuzz.token_sort_ratio(u, a),
        fuzz.ratio(u, a),
    ]
    if len(a) > 6:
        scores.append(fuzz.partial_ratio(u, a))
    return float(max(scores))


def is_correct_answer(user_input: str, answer: str) -> bool:
    return guess_score(user_input, answer) >= THRESHOLD_CORRECT
