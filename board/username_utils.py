"""
Username normalization and similarity checking.

Normalization: lowercase, strip diacritics, keep only [a-z0-9].
Similarity: O(np) edit distance (Wu, Manber, Myers, Miller 1990).
"""

import unicodedata
import re


# Characters NFKD cannot decompose — manual fallback
_MANUAL = str.maketrans("łŁøØæÆ", "lLoOaA")


def normalize(username: str) -> str:
    """Normalize username for comparison: lowercase, no diacritics, alphanumeric only.
    Uses NFKD for most diacritics (handles ą,ć,ę,ń,ó,ś,ź,ż and Nordic Å etc.),
    plus manual map for ł/ø/æ which have no Unicode decomposition.
    """
    s = username.translate(_MANUAL)
    nfkd = unicodedata.normalize("NFKD", s.lower())
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", ascii_only)


def _onp_distance(a: str, b: str) -> int:
    """
    O(np) edit distance — Wu/Manber/Myers/Miller 1990.
    Returns the edit distance (number of insertions + deletions).
    Ported from cli::Distance::compare() in distance-impl.hpp.
    """
    # Ensure a is the shorter string
    if len(a) > len(b):
        a, b = b, a
    M, N = len(a), len(b)
    Delta = N - M

    # fp is indexed from -(M+1) to N+1; use dict for simplicity
    fp = {}

    def get(k):
        return fp.get(k, -1)

    def snake(k, y):
        x = y - k
        while x < M and y < N and a[x] == b[y]:
            x += 1
            y += 1
        return y

    def choose(k):
        v0 = get(k - 1) + 1
        v1 = get(k + 1)
        best = v0 if v0 > v1 else v1
        return snake(k, best)

    p = -1
    while True:
        p += 1
        for k in range(-p, Delta):
            fp[k] = choose(k)
        for k in range(Delta + p, Delta, -1):
            fp[k] = choose(k)
        fp[Delta] = choose(Delta)
        if fp[Delta] == N:
            break

    return Delta + 2 * p


def is_too_similar(proposed: str, existing: str, max_dist: int = 1) -> bool:
    """Return True if normalized proposed name is within max_dist edits of existing."""
    return _onp_distance(normalize(proposed), normalize(existing)) <= max_dist


def find_similar(proposed: str, usernames: list[str], max_dist: int = 1) -> list[str]:
    """Return list of existing usernames too similar to proposed."""
    norm = normalize(proposed)
    result = []
    best = max_dist
    for name in usernames:
        d = _onp_distance(norm, normalize(name))
        if d <= best:
            if d < best:
                result = []
                best = d
            result.append(name)
    return result
