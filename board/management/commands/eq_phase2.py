#!/usr/bin/env python3
"""Faza 2 — shared regex hub for post-enrichment fixes."""
import re

from eq_common import *

# ---------------------------------------------------------------------------
# Regex constants (shared by eq_phase2_*.py sub-modules)
# ---------------------------------------------------------------------------

# Regex: enriched opening tag z post_id
_ENRICHED_OPEN_RE = re.compile(
    r'\[(?P<qtype>f?quote)(?:="(?P<author>[^"]*)")?\s+post_id=(?P<post_id>\d+)[^\]]*\]',
    re.IGNORECASE,
)
# Unresolved opening tag (bez post_id)
_UNRESOLVED_OPEN_RE = re.compile(
    r'\[(?:f?quote)(?:="(?P<author>[^"]*)")?\]',
    re.IGNORECASE,
)

_NAMED_UNRESOLVED_RE = re.compile(
    r'\[quote="([^"]+)"\]',
    re.IGNORECASE,
)
_ANY_POST_ID_RE = re.compile(r'post_id=', re.IGNORECASE)
_QUOTE_WITH_POST_ID_RE = re.compile(
    r'\[quote(?:="(?P<author>[^"]*)")?(?P<mid>\s+post_id=(?P<post_id>\d+))(?P<tail>[^\]]*)\]',
    re.IGNORECASE,
)
_ANY_ENRICHED_QUOTE_RE = re.compile(
    r'\[(?P<qtype>f?quote)(?:="(?P<author>[^"]*)")?(?P<mid>\s+post_id=(?P<post_id>\d+))(?P<tail>[^\]]*)\]',
    re.IGNORECASE,
)

# Bible open RE (needed by phase2 functions; imported from phase3 would create circular dep)
_BIBLE_OPEN_RE = re.compile(r'\[Bible=[^\]]*\]', re.IGNORECASE)
_ANY_QUOTE_OPEN_RE = re.compile(r'\[(?:quote|fquote|Bible)(?:[^\]]*)\]', re.IGNORECASE)
_BIBLE_FOUND_RE = re.compile(
    r'\[(?:quote[^\]]*post_id=\d|Bible=)[^\]]*\]', re.IGNORECASE
)
