import re
from rapidfuzz import fuzz

_NOISE = re.compile(r"[^\w\s]")

def _norm(text: str) -> str:
    return _NOISE.sub("", text.strip().lower())

def is_correct_answer(user_input: str, answer: str, threshold: int = 82) -> bool:
    """Return True if user_input is a close-enough match for answer."""
    u = _norm(user_input)
    a = _norm(answer)

    if not u:
        return False
    if u == a:
        return True

    # Token sort handles word-order differences (e.g. "Beach Kappil" vs "Kappil Beach")
    if fuzz.token_sort_ratio(u, a) >= threshold:
        return True

    # Standard ratio catches simple typos / extra letters (Kaappil → Kappil)
    if fuzz.ratio(u, a) >= threshold:
        return True

    # Partial ratio is useful when the answer is a multi-word phrase
    # and the user types only part of it correctly; raise bar slightly.
    if len(a) > 6 and fuzz.partial_ratio(u, a) >= threshold + 5:
        return True

    return False
