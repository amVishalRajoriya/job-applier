"""Small deterministic text utilities (tokenizing, light stemming, similarity)."""
from __future__ import annotations

import re

STOPWORDS = set("""
a about above across after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each either etc few for from further had
has have having he her here hers him his how i if in into is it its itself just least less like make more
most must my no nor not of off on once only or other our ours out over own per plus same she should so some
such than that the their theirs them then there these they this those through to too under until up upon
us very via was we well were what when where which while who whom why will with within without would you
your yours role team teams company work working ability able strong excellent good great new using use used
including include includes across years year experience experienced skills skill knowledge understanding
familiarity familiar proficiency proficient plus preferred required requirements responsibilities
responsible candidate candidates ideal looking join help ensure various related relevant e.g eg i.e ie
other others day days environment environments solid demonstrated proven track record
""".split())

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#./-]*[a-z0-9+#]|[a-z0-9]")


def stem(word: str) -> str:
    w = word
    for suf, rep in (("ization", "ize"), ("isation", "ize"), ("ational", "ate"), ("iveness", "ive"),
                     ("fulness", "ful"), ("ousness", "ous"), ("ments", "ment"), ("ings", ""), ("ing", ""),
                     ("ies", "y"), ("ied", "y"), ("edly", ""), ("ed", ""), ("ers", "er"), ("es", ""), ("s", "")):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)] + rep
            break
    return w


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in STOPWORDS and not t.isdigit()]


def stems(text: str) -> set[str]:
    return {stem(t) for t in tokens(text) if len(t) > 1}


def overlap(a: str, b: str) -> float:
    """Asymmetric coverage: fraction of a's content stems found in b."""
    sa, sb = stems(a), stems(b)
    if not sa:
        return 0.0
    return len(sa & sb) / len(sa)


NUMBER = re.compile(r"(?<![a-z])(\$?\d[\d,]*(?:\.\d+)?\s*(?:%|percent|x|k|m|mm|bn|million|billion|thousand)?)", re.I)


def numbers_in(text: str) -> set[str]:
    """Normalized numeric claims (metrics, counts, percentages) appearing in text."""
    out = set()
    for m in NUMBER.finditer(text or ""):
        raw = m.group(1).lower().replace(",", "").replace(" ", "").replace("$", "")
        raw = raw.replace("percent", "%")
        out.add(raw)
    return out


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
