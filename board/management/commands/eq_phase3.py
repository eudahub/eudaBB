#!/usr/bin/env python3
"""Faza 3 — globals/helpers hub for Bible detection."""
import pickle
import re
import unicodedata

from eq_common import *

# ---------------------------------------------------------------------------
# Bible globals and helpers
# ---------------------------------------------------------------------------

_BIBLE_NGRAM_INDEX = None       # załadowany przez --bible-index
_BIBLE_COVERAGE    = 0.40       # minimalny % n-gramów pasujących (--bible-coverage)
_BIBLE_DRY_MIN     = 0.09       # minimalny % do pokazania w dry-run (--bible-dry-min)

_BIBLE_NGRAM_SIZE = 5


def load_bible_index(path):
    global _BIBLE_NGRAM_INDEX
    with open(path, 'rb') as f:
        data = pickle.load(f)
    _BIBLE_NGRAM_INDEX = data['ngrams']


def norm_for_bible(text):
    """Lowercase, strip diacritics, keep only alnum+space."""
    text = unicodedata.normalize('NFKD', text.lower())
    return ''.join(c for c in text
                   if unicodedata.category(c) != 'Mn' and (c.isalnum() or c == ' '))


def _bible_votes(inner):
    """Zwraca (best_ref, best_count, total_grams) dla tekstu wewnętrznego cytatu.

    Nie stosuje żadnego progu — zwraca surowe dane do dalszej oceny.
    Zwraca (None, 0, 0) jeśli brak jakiegokolwiek dopasowania.
    """
    if _BIBLE_NGRAM_INDEX is None:
        return None, 0, 0
    text = _strip_bbcode_tags(inner)
    ws = norm_for_bible(text).split()
    if not ws:
        return None, 0, 0
    n = _BIBLE_NGRAM_SIZE
    if len(ws) < n:
        key = ' '.join(ws)
        ref = _BIBLE_NGRAM_INDEX.get(key)
        if ref:
            return ref, 1, 1
        return None, 0, 0
    total_grams = len(ws) - n + 1
    ref_votes = {}
    for i in range(total_grams):
        key = ' '.join(ws[i:i + n])
        ref = _BIBLE_NGRAM_INDEX.get(key)
        if ref:
            ref_votes[ref] = ref_votes.get(ref, 0) + 1
    if not ref_votes:
        return None, 0, total_grams
    best_ref = max(ref_votes, key=ref_votes.get)
    return best_ref, ref_votes[best_ref], total_grams


def lookup_bible(inner):
    """Zwraca referencję biblijną jeśli tekst pasuje do wersetu, inaczej None.

    Wymaga minimum 2 głosów i pokrycia >= _BIBLE_COVERAGE.
    """
    ref, best_count, total_grams = _bible_votes(inner)
    if ref is None or best_count == 0:
        return None
    total_grams = max(1, total_grams)
    min_coverage = max(2, int(total_grams * _BIBLE_COVERAGE + 0.9999))  # ceil, min 2
    return ref if best_count >= min_coverage else None


# ---------------------------------------------------------------------------
# Regex constants (shared by eq_phase3_*.py sub-modules)
# ---------------------------------------------------------------------------

_BIBLE_OPEN_RE = re.compile(r'\[Bible=[^\]]*\]', re.IGNORECASE)
# Dowolny otwierający tag cytatowy (quote/fquote/Bible) — do odrzucania bloków z zagnieżdżonymi cytatami
_ANY_QUOTE_OPEN_RE = re.compile(r'\[(?:quote|fquote|Bible)(?:[^\]]*)\]', re.IGNORECASE)
_BIBLE_FOUND_RE = re.compile(
    r'\[(?:quote[^\]]*post_id=\d|Bible=)[^\]]*\]', re.IGNORECASE
)

# Unresolved opening tag (bez post_id) — needed locally
_UNRESOLVED_OPEN_RE = re.compile(
    r'\[(?:f?quote)(?:="(?P<author>[^"]*)")?\]',
    re.IGNORECASE,
)
