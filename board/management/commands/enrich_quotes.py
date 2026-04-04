"""
Enrich [quote] tags in sfiniabb.db with post_id and Unix timestamps.

Matching pipeline per quote block:
  1. Strip ellipsis markers: (...)  /.../  [...]  before fingerprinting
  2. Extract "own text" segments (between nested [quote] blocks)
  3. Try multiple fingerprint lengths: 120 → 80 → 60 → 40 chars
  4. Search preceding posts in same topic (full topic window by default)
  5. Author-filtered search first, then all posts

After the main pass, --propagate fixes nested [quote] blocks inside already-
resolved citations by chaining through the quotes table:
  post P cites Q (found) → nested [quote=A] in P's copy of Q can be resolved
  if Q also cited A (from quotes table).

Usage:
    python manage.py enrich_quotes /path/to/sfiniabb.db
    python manage.py enrich_quotes /path/to/sfiniabb.db --users-db sfinia_users_real.db
    python manage.py enrich_quotes /path/to/sfiniabb.db --reset
    python manage.py enrich_quotes /path/to/sfiniabb.db --only-unfound   # reprocess status 2+3
    python manage.py enrich_quotes /path/to/sfiniabb.db --propagate      # chain-fix nested quotes
"""

import bisect
import pickle
import re
import sqlite3
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError

_WARSAW = ZoneInfo("Europe/Warsaw")

_PL_MONTHS = {
    "Sty": 1, "Lut": 2, "Mar": 3, "Kwi": 4, "Maj": 5, "Cze": 6,
    "Lip": 7, "Sie": 8, "Wrz": 9, "Paź": 10, "Lis": 11, "Gru": 12,
}

# Patterns for quote tag parsing
_QUOTE_OPEN_RE  = re.compile(r'\[quote(?:="([^"]*)")?\]', re.IGNORECASE)
_QUOTE_CLOSE_RE = re.compile(r'\[/quote\]', re.IGNORECASE)
_BBCODE_RE      = re.compile(r'\[[^\]]*\]')

# Matches [quote=Author post_id=not_found] tags specifically
_NOT_FOUND_TAG_RE = re.compile(
    r'\[quote=(?P<author>[^ \]]+)\s+post_id=not_found\]',
    re.IGNORECASE
)

# Manual quote prefixes: "Username napisał:\n" or "Cytat:\n"
# These appear when users hand-format a quote instead of using BBCode
_NAPISAL_PREFIX_RE = re.compile(r'^\s*\S[^\n]*napisał:\s*\n+', re.IGNORECASE)
_CYTAT_PREFIX_RE   = re.compile(r'^\s*Cytat:\s*\n+', re.IGNORECASE)

# Ellipsis markers inserted by users when omitting part of a quote
# e.g. (...)  /.../  [...]  (…)
_ELLIPSIS_RE = re.compile(
    r'[\(\[/]\s*\.{2,}\s*[\)\]/]'  # (...) /.../ [...]
    r'|[\(\[]\s*…\s*[\)\]]'         # (…) […]
    r'|\s+\.{2,}\s+'                # spaces ... spaces  (inline ellipsis)
    r'|\s+…\s+'                     # spaces … spaces
)

# BBCode-wrapped URLs: [https://...] → unwrap to https://... before stripping tags
_URL_BRACKET_RE = re.compile(r'\[(https?://[^\]]+)\]', re.IGNORECASE)

# Minimum fingerprint length to attempt matching
_MIN_FINGERPRINT = 5
# Fingerprint lengths tried in order (longest first to avoid false positives)
_FINGERPRINT_LENS = [120, 80, 60, 40, 20, 10]
# Short fingerprints (below this) are only tried in a small nearby window,
# never in the full topic or global fallback (too many false positives)
_SHORT_FP_LIMIT = 20
# How many preceding posts to search when using short fingerprints
_SHORT_FP_WINDOW = 10


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_pl_date(s: str):
    """Parse 'Nie 21:08, 22 Sty 2006' → aware datetime (UTC). Returns None on failure."""
    if not s:
        return None
    try:
        parts = s.split()
        time_part = parts[1].rstrip(",")
        hour, minute = map(int, time_part.split(":"))
        day   = int(parts[2])
        month = _PL_MONTHS.get(parts[3])
        year  = int(parts[4])
        if month is None:
            return None
        naive = datetime(year, month, day, hour, minute)
        return naive.replace(tzinfo=_WARSAW)
    except Exception:
        return None


def _to_unix(created_at: str) -> int:
    dt = _parse_pl_date(created_at)
    return int(dt.timestamp()) if dt else 0


# ---------------------------------------------------------------------------
# BBCode helpers
# ---------------------------------------------------------------------------

def _strip_bbcode(text: str) -> str:
    """Remove BBCode tags and normalize whitespace.
    [https://url] is first unwrapped to https://url so bare and bracketed
    URLs match each other.
    """
    text = _URL_BRACKET_RE.sub(r'\1', text)
    stripped = _BBCODE_RE.sub("", text)
    return " ".join(stripped.split()).lower()


def _to_ascii(text: str) -> str:
    """Strip diacritics: 'żółw' → 'zolw'. Used for diacritics-insensitive matching."""
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()


def _strip_bbcode_ascii(text: str) -> str:
    """Like _strip_bbcode but also strips diacritics."""
    return _to_ascii(_strip_bbcode(text))


def _own_text_segments(inner: str) -> list[str]:
    """Return the non-nested-quote text segments from a quote's inner content.

    For [quote=A][quote=B]nested[/quote] R1 [quote=C]nested2[/quote] R2[/quote]
    returns ["R1", "R2"] — the poster's own words, not the re-quoted material.

    Returns segments in order; each is a stripped text string.
    """
    events = []
    for m in _QUOTE_OPEN_RE.finditer(inner):
        events.append((m.start(), "open", m.end()))
    for m in _QUOTE_CLOSE_RE.finditer(inner):
        events.append((m.start(), "close", m.end()))
    events.sort(key=lambda x: x[0])

    segments = []
    depth = 0
    last = 0
    for pos, kind, end in events:
        if kind == "open":
            if depth == 0:
                seg = inner[last:pos].strip()
                if seg:
                    segments.append(seg)
            depth += 1
        else:
            if depth > 0:
                depth -= 1
                if depth == 0:
                    last = end
    tail = inner[last:].strip()
    if tail:
        segments.append(tail)
    return segments


def _split_on_ellipsis(text: str) -> list[str]:
    """Split text on ellipsis markers (...) /.../ [...] returning non-empty parts."""
    parts = _ELLIPSIS_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Bible detection
# ---------------------------------------------------------------------------

_BIBLE_NGRAM_INDEX: dict | None = None   # loaded lazily from --bible-index pickle
_BIBLE_NGRAM_SIZE = 5

def _load_bible_index(path: str) -> None:
    global _BIBLE_NGRAM_INDEX
    with open(path, 'rb') as f:
        data = pickle.load(f)
    _BIBLE_NGRAM_INDEX = data['ngrams']


def _norm_for_bible(text: str) -> str:
    """Normalize text: lowercase, strip diacritics, keep only alnum+space."""
    text = unicodedata.normalize('NFKD', text.lower())
    return ''.join(c for c in text if unicodedata.category(c) != 'Mn' and (c.isalnum() or c == ' '))


def _lookup_bible(inner: str) -> str | None:
    """Return Bible reference string if inner text matches a Bible verse, else None.

    Uses the n-gram index loaded via --bible-index.  Returns the best matching
    reference (e.g. "J 3,16") or book-only string for OT (e.g. "Ps").

    Confidence rules:
      - NT refs (contain comma, e.g. "J 3,16"): 1 matching n-gram is enough.
        The NT mapping is exact so a single match is reliable.
      - OT refs (book name only, e.g. "Ps"): require at least 2 matching n-grams
        with the same ref, because the OT mapping is approximate and single common
        phrases can produce false positives.
    """
    if _BIBLE_NGRAM_INDEX is None:
        return None
    # Strip BBCode and prefixes, then normalize
    text = _NAPISAL_PREFIX_RE.sub('', inner, count=1).strip()
    text = _CYTAT_PREFIX_RE.sub('', text, count=1).strip()
    text = _strip_bbcode(text)
    ws = _norm_for_bible(text).split()
    if not ws:
        return None
    n = _BIBLE_NGRAM_SIZE
    if len(ws) < n:
        key = ' '.join(ws)
        ref = _BIBLE_NGRAM_INDEX.get(key)
        if ref and (',' in ref or len(ws) >= 4):
            return ref
        return None
    # Count n-gram votes per reference
    ref_votes: dict[str, int] = {}
    for i in range(len(ws) - n + 1):
        key = ' '.join(ws[i:i + n])
        ref = _BIBLE_NGRAM_INDEX.get(key)
        if ref:
            ref_votes[ref] = ref_votes.get(ref, 0) + 1
    if not ref_votes:
        return None
    best_ref = max(ref_votes, key=ref_votes.get)
    best_count = ref_votes[best_ref]
    is_nt = ',' in best_ref   # NT refs have chapter,verse like "J 3,16"
    min_votes = 1 if is_nt else 2
    return best_ref if best_count >= min_votes else None


def _fingerprints(inner: str) -> list[str]:
    """Return ordered list of candidate search needles for a [quote] block.

    Pipeline:
    0. Strip manual "Username napisał:" / "Cytat:" prefix (hand-formatted quotes)
    1. _own_text_segments — removes nested [quote] blocks, keeps poster's own text
    2. _split_on_ellipsis — splits each segment at (...) and /.../ markers;
       each part is searched independently so "A /.../ B" finds posts containing
       either "A" or "B" rather than the impossible joined string "A B"
    3. Each sub-segment tried at lengths 120→80→60→40 chars (long first to
       avoid false positives; shorter catches emoticon/word-level edits)
    4. Fallback: full inner text stripped (when all segments are too short)
    """
    # Strip manual quote prefixes before any processing
    inner = _NAPISAL_PREFIX_RE.sub('', inner, count=1).strip()
    inner = _CYTAT_PREFIX_RE.sub('', inner, count=1).strip()

    fps_seen: set[str] = set()
    fps: list[str] = []

    def _add(text: str) -> None:
        cleaned = _strip_bbcode(text)
        for length in _FINGERPRINT_LENS:
            fp = cleaned[:length].strip()
            if len(fp) >= _MIN_FINGERPRINT and fp not in fps_seen:
                fps_seen.add(fp)
                fps.append(fp)

    for seg in _own_text_segments(inner):
        for sub in _split_on_ellipsis(seg):
            _add(sub)
        # Also add the whole segment (ellipsis replaced by space) for cases
        # where /.../ appears at start/end and the rest is long enough
        cleaned_whole = _strip_bbcode(_ELLIPSIS_RE.sub(" ", seg))
        for length in _FINGERPRINT_LENS:
            fp = cleaned_whole[:length].strip()
            if len(fp) >= _MIN_FINGERPRINT and fp not in fps_seen:
                fps_seen.add(fp)
                fps.append(fp)

    if not fps:
        # Fallback: full inner text including nested-quote content
        for sub in _split_on_ellipsis(inner):
            _add(sub)
        if not fps:
            _add(_ELLIPSIS_RE.sub(" ", inner))

    return fps


# ---------------------------------------------------------------------------
# Quote extraction (stack-based, handles nesting)
# ---------------------------------------------------------------------------

def _extract_top_quotes(content: str) -> list:
    """Find all top-level [quote] blocks.

    Returns list of:
        (block_start, block_end, tag_end, raw_username)
    where:
        block_start — index of '[quote...' opening tag
        block_end   — index after '[/quote]' closing tag
        tag_end     — index right after the opening tag (= start of inner content)
        raw_username — string from [quote="..."] or None for [quote]
    """
    events = []
    for m in _QUOTE_OPEN_RE.finditer(content):
        events.append((m.start(), "open", m.group(1), m.end()))
    for m in _QUOTE_CLOSE_RE.finditer(content):
        events.append((m.start(), "close", None, m.end()))
    events.sort(key=lambda x: x[0])

    result = []
    depth = 0
    block_start = -1
    tag_end = -1
    raw_username = None

    for pos, kind, username, end in events:
        if kind == "open":
            if depth == 0:
                block_start   = pos
                tag_end       = end
                raw_username  = username
            depth += 1
        else:  # close
            if depth > 0:
                depth -= 1
                if depth == 0 and block_start >= 0:
                    result.append((block_start, end, tag_end, raw_username))
                    block_start = -1

    return result


# ---------------------------------------------------------------------------
# Username canonicalization
# ---------------------------------------------------------------------------

def _canonicalize(username: str | None, known_users: dict) -> str | None:
    """Return canonical username or None if unknown/garbled."""
    if not username:
        return None
    if username in known_users:
        return username
    # Case-insensitive fallback
    lower = username.lower()
    return known_users.get(lower)


# ---------------------------------------------------------------------------
# Post matching
# ---------------------------------------------------------------------------

def _match_post(fps: list[str], window: list, canonical: str | None,
                global_fallback: list | None = None,
                short_window: list | None = None) -> dict | None:
    """Search window then optional global_fallback using fps needles.

    window          — posts from the same topic, ordered by post_order
    global_fallback — N preceding posts globally by post_id (cross-topic)
    short_window    — last _SHORT_FP_WINDOW posts in topic; used ONLY for
                      short fingerprints (< _SHORT_FP_LIMIT) to avoid false
                      positives — never searched globally

    For each fingerprint, tries author-filtered first, then all posts.
    Regular fps: topic window → global fallback.
    Short fps:   short_window only (nearby context, no global).
    """
    if not fps:
        return None

    regular_fps = [fp for fp in fps if len(fp) >= _SHORT_FP_LIMIT]
    tiny_fps    = [fp for fp in fps if len(fp) <  _SHORT_FP_LIMIT]

    def _try_one(fp: str, fp_ascii: str, posts) -> dict | None:
        for p in reversed(posts):
            if fp in p["stripped"]:
                return p
            # Diacritics fallback: both sides normalized to ASCII
            if fp_ascii and fp_ascii != fp and fp_ascii in p["stripped_ascii"]:
                return p
        return None

    def _try_fps_list(fp_list, posts):
        for fp in fp_list:
            fp_ascii = _to_ascii(fp)
            hit = _try_one(fp, fp_ascii, posts)
            if hit:
                return hit
        return None

    def _search(fp_list, posts):
        if canonical:
            by_author = [p for p in posts if p["author_name"] == canonical]
            found = _try_fps_list(fp_list, by_author)
            if found:
                return found
        return _try_fps_list(fp_list, posts)

    # Phase 1: regular-length fps in full topic window
    result = _search(regular_fps, window) if regular_fps else None

    # Phase 1b: global fallback — only by canonical author (no fall-through to all)
    # Wider window (250 posts) is safe only when author-filtered
    if result is None and regular_fps and global_fallback and canonical:
        by_author = [p for p in global_fallback if p["author_name"] == canonical]
        result = _try_fps_list(regular_fps, by_author) if by_author else None

    # Phase 2: short fps — only in the small nearby window (no global)
    if result is None and tiny_fps and short_window:
        result = _search(tiny_fps, short_window)

    return result


# ---------------------------------------------------------------------------
# Core enrichment
# ---------------------------------------------------------------------------

def _enrich_content(post_id: int, content: str,
                    window: list, known_users: dict,
                    quotes_out: list,
                    global_fallback: list | None = None,
                    short_window: list | None = None) -> str:
    """Replace top-level [quote] tags with enriched versions.

    Appends dicts to quotes_out for each quote found.
    Returns the enriched content string.
    """
    quotes = _extract_top_quotes(content)
    if not quotes:
        return content

    pieces = []
    last_end = 0

    for (block_start, block_end, tag_end, raw_username) in quotes:
        canonical = _canonicalize(raw_username, known_users)
        inner     = content[tag_end:block_end - len("[/quote]")]
        fps       = _fingerprints(inner)
        match     = _match_post(fps, window, canonical, global_fallback, short_window)

        if match:
            cited_id  = match["post_id"]
            unix_time = _to_unix(match["created_at"])
            author    = canonical or match["author_name"] or ""
            if author:
                new_tag = f"[quote={author} post_id={cited_id} time={unix_time}]"
            else:
                new_tag = f"[quote post_id={cited_id} time={unix_time}]"
            quotes_out.append({
                "citing_post_id":  post_id,
                "cited_post_id":   cited_id,
                "quote_author":    raw_username,
                "canonical_author": canonical or (match["author_name"] if not canonical else None),
                "found":           1,
                "is_foreign":      0,
            })
        else:
            # Not found — keep best available form
            if canonical:
                new_tag = f"[quote={canonical}]"
            elif raw_username:
                # garbled username → treat as anonymous
                new_tag = "[quote]"
            else:
                new_tag = "[quote]"
            quotes_out.append({
                "citing_post_id":  post_id,
                "cited_post_id":   None,
                "quote_author":    raw_username,
                "canonical_author": canonical,
                "found":           0,
                "is_foreign":      0,
            })

        pieces.append(content[last_end:block_start])
        pieces.append(new_tag)
        pieces.append(content[tag_end:block_end])   # inner + [/quote]
        last_end = block_end

    pieces.append(content[last_end:])
    return "".join(pieces)


# ---------------------------------------------------------------------------
# Chain propagation
# ---------------------------------------------------------------------------

# Matches enriched quote tags: [quote=Author post_id=N time=T] or [quote post_id=N time=T]
_ENRICHED_TAG_RE = re.compile(
    r'\[quote(?:=(?P<author>[^ \]]+))?\s+post_id=(?P<post_id>\d+)\s+time=(?P<time>\d+)\]',
    re.IGNORECASE,
)
# Matches unenriched opening tags (no post_id): [quote=Author] or [quote]
_PLAIN_TAG_RE = re.compile(
    r'\[quote(?:="?(?P<author>[^"\]\s]+)"?)?\](?!\s*[^\[]*post_id=)',
    re.IGNORECASE,
)
# Matches ANY [quote...] opening tag (both sfinia and enriched formats)
# Used in propagation where content_quotes may contain enriched top-level tags
_QUOTE_ANY_OPEN_RE = re.compile(r'\[quote(?:[^\]]*?)?\]', re.IGNORECASE)
_QUOTE_CLOSE_PLAIN_RE = re.compile(r'\[/quote\]', re.IGNORECASE)


def _extract_top_quotes_enriched(content: str) -> list:
    """Like _extract_top_quotes but also recognises enriched [quote=X post_id=N] tags."""
    events = []
    for m in _QUOTE_ANY_OPEN_RE.finditer(content):
        events.append((m.start(), "open", m.group(0), m.end()))
    for m in _QUOTE_CLOSE_PLAIN_RE.finditer(content):
        events.append((m.start(), "close", None, m.end()))
    events.sort(key=lambda x: x[0])

    result = []
    depth = 0
    block_start = tag_end = -1
    opening_tag = ""

    for pos, kind, tag_text, end in events:
        if kind == "open":
            if depth == 0:
                block_start = pos
                tag_end     = end
                opening_tag = tag_text or ""
            depth += 1
        else:
            if depth > 0:
                depth -= 1
                if depth == 0 and block_start >= 0:
                    result.append((block_start, end, tag_end, opening_tag))
                    block_start = -1
    return result


def _propagate_chain_quotes(conn, stdout) -> int:
    """Enrich nested [quote] blocks inside already-resolved top-level citations.

    For each post P that cites post Q (found in quotes table):
      - Extract top-level [quote=Q_author post_id=Q_id] blocks from P's content_quotes
      - Within each such block, find unenriched nested [quote=A] tags
      - Look up quotes table: does Q cite A with a found citation?
      - If yes (unambiguously), replace [quote=A] with [quote=A post_id=R time=T]

    Returns count of nested tags enriched.
    """
    # Build map: cited_post_id → list of {canonical_author, cited_post_id, created_at}
    # i.e. "what did each post cite?"
    post_citations: dict[int, list[dict]] = {}
    for row in conn.execute(
        "SELECT q.citing_post_id, q.canonical_author, q.cited_post_id, p.created_at "
        "FROM quotes q JOIN posts p ON q.cited_post_id = p.post_id "
        "WHERE q.found = 1 ORDER BY q.citing_post_id"
    ):
        cit_id = row[0]
        if cit_id not in post_citations:
            post_citations[cit_id] = []
        post_citations[cit_id].append({
            "author":     row[1] or "",
            "post_id":    row[2],
            "created_at": row[3] or "",
        })

    # Only process posts that:
    #   a) have at least one found outer citation (q_outer.found=1)
    #   b) whose cited post itself has found citations (q_inner.found=1)
    # This avoids scanning all 200k+ posts — SQL join identifies exact candidates.
    candidate_ids = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT q_outer.citing_post_id "
            "FROM quotes q_outer "
            "JOIN quotes q_inner ON q_outer.cited_post_id = q_inner.citing_post_id "
            "WHERE q_outer.found = 1 AND q_inner.found = 1"
        )
    }
    stdout.write(f"  Kandydatów do propagacji: {len(candidate_ids)}")

    if not candidate_ids:
        return 0

    placeholders = ','.join('?' * len(candidate_ids))
    candidate_rows = conn.execute(
        f"SELECT post_id, content_quotes FROM posts "
        f"WHERE post_id IN ({placeholders}) AND content_quotes IS NOT NULL",
        list(candidate_ids)
    ).fetchall()

    # Process posts that have found outer citations
    updates = []
    enriched_total = 0

    for row in candidate_rows:
        post_id  = row[0]
        content  = row[1]
        if not _PLAIN_TAG_RE.search(content):
            continue  # no unenriched nested tags at all

        # Find top-level blocks (enriched AND unenriched) in this post
        top_quotes = _extract_top_quotes_enriched(content)
        if not top_quotes:
            continue

        new_content = content
        offset = 0   # shift as we make replacements

        for block_start, block_end, tag_end, opening_tag in top_quotes:
            # Identify which cited post this outer block links to
            m = _ENRICHED_TAG_RE.match(opening_tag)
            if not m:
                continue
            outer_cited_id = int(m.group("post_id"))
            inner_citations = post_citations.get(outer_cited_id)
            if not inner_citations:
                continue

            # Build lookup: author → (post_id, created_at) — skip truly ambiguous authors
            author_map: dict[str, tuple[int, str] | None] = {}
            for c in inner_citations:
                a   = c["author"].lower()
                val = (c["post_id"], c["created_at"])
                if a in author_map:
                    if author_map[a] != val:
                        author_map[a] = None  # genuinely different targets → ambiguous
                    # else: duplicate entry for same target — keep as-is
                else:
                    author_map[a] = val

            # Replace plain [quote=A] / [quote] tags within this block's inner content
            inner_start = block_start + (tag_end - block_start)
            inner_end   = block_end - len("[/quote]")
            inner       = new_content[inner_start + offset: inner_end + offset]

            def _replace_plain(m2):
                raw_author = (m2.group("author") or "").strip('"').strip()
                key = raw_author.lower()
                hit = author_map.get(key) if key else None
                # For anonymous [quote]: try if there's exactly one inner citation
                if not key and len([v for v in author_map.values() if v]) == 1:
                    hit = next(v for v in author_map.values() if v)
                    raw_author = next(k for k, v in author_map.items() if v)
                if hit is None:
                    return m2.group(0)  # keep as-is
                cited_pid, cited_cat = hit
                unix = _to_unix(cited_cat)
                if raw_author:
                    return f"[quote={raw_author} post_id={cited_pid} time={unix}]"
                return f"[quote post_id={cited_pid} time={unix}]"

            new_inner, n = _PLAIN_TAG_RE.subn(_replace_plain, inner)
            if n and new_inner != inner:
                new_content = (
                    new_content[:inner_start + offset]
                    + new_inner
                    + new_content[inner_end + offset:]
                )
                offset += len(new_inner) - len(inner)
                enriched_total += n

        if new_content != content:
            updates.append((new_content, post_id))

    conn.executemany(
        "UPDATE posts SET content_quotes=? WHERE post_id=?", updates
    )
    stdout.write(f"  Zaktualizowano {len(updates)} postów.")
    return enriched_total


# ---------------------------------------------------------------------------
# Management command
# ---------------------------------------------------------------------------

_SETUP_SQL = """
-- Add content_quotes column if missing
-- (SQLite doesn't have IF NOT EXISTS for ALTER TABLE, handled in Python)

-- quote_status per post:
--   0 = brak cytatów (default)
--   1 = wszystkie cytaty znalezione
--   2 = żadne cytaty nieznalezione
--   3 = część znaleziona, część nie

CREATE TABLE IF NOT EXISTS quotes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    citing_post_id   INTEGER NOT NULL,
    cited_post_id    INTEGER,
    quote_author     TEXT,
    canonical_author TEXT,
    found            INTEGER NOT NULL DEFAULT 0,
    is_foreign       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_quotes_citing ON quotes(citing_post_id);
CREATE INDEX IF NOT EXISTS idx_quotes_cited  ON quotes(cited_post_id);
"""


class Command(BaseCommand):
    help = "Enrich [quote] tags in sfiniabb.db with post_id and Unix timestamps"

    def add_arguments(self, parser):
        parser.add_argument("archive_db",
                            help="Path to sfiniabb.db")
        parser.add_argument("--users-db", default="",
                            help="Path to sfinia_users_real.db (for username validation)")
        parser.add_argument("--lookahead", type=int, default=0,
                            help="Max preceding posts per topic to search (0 = full topic, default)")
        parser.add_argument("--reset", action="store_true",
                            help="Drop and recreate quotes table; reprocess all posts")
        parser.add_argument("--only-unfound", action="store_true",
                            help="Only reprocess posts with quote_status 2 or 3 (faster re-run)")
        parser.add_argument("--propagate", action="store_true",
                            help="Propagate enrichments to nested quotes via the quotes chain table")
        parser.add_argument("--bible-index", default="",
                            help="Path to bible_ngram.pkl; enables Bible-verse detection on unfound quotes")
        parser.add_argument("--detect-bible", action="store_true",
                            help="Run Bible-detection pass on posts with unfound quotes (requires --bible-index)")
        parser.add_argument("--search-not-found", action="store_true",
                            help="Search all posts by known author for not_found quotes (unlimited distance)")

    def handle(self, *args, **options):
        db_path          = options["archive_db"]
        users_path       = options["users_db"]
        lookahead        = options["lookahead"]
        reset            = options["reset"]
        only_unfound     = options["only_unfound"]
        do_propagate     = options["propagate"]
        bible_idx_path   = options["bible_index"]
        do_bible         = options["detect_bible"]
        do_search_nf     = options["search_not_found"]

        if bible_idx_path:
            try:
                _load_bible_index(bible_idx_path)
                self.stdout.write(f"Załadowano indeks Biblii z {bible_idx_path} ({len(_BIBLE_NGRAM_INDEX)} n-gramów)")
            except Exception as e:
                raise CommandError(f"Nie można otworzyć bible-index: {e}")
        elif do_bible:
            raise CommandError("--detect-bible wymaga --bible-index")

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        except Exception as e:
            raise CommandError(f"Cannot open {db_path}: {e}")

        # --- Schema setup ---
        if reset:
            conn.execute("DROP TABLE IF EXISTS quotes")
            self.stdout.write("Usunięto starą tabelę quotes.")

        conn.executescript(_SETUP_SQL)

        # Add content_quotes column if missing
        cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        if "content_quotes" not in cols:
            conn.execute("ALTER TABLE posts ADD COLUMN content_quotes TEXT")
            conn.commit()
            self.stdout.write("Dodano kolumnę content_quotes.")

        # --- Load known usernames ---
        known_users: dict[str, str] = {}   # lowercase → canonical

        if users_path:
            try:
                uconn = sqlite3.connect(users_path)
                for row in uconn.execute("SELECT username FROM users"):
                    name = row[0]
                    if name:
                        known_users[name]        = name   # exact key
                        known_users[name.lower()] = name  # lowercase fallback
                uconn.close()
                self.stdout.write(f"Załadowano {len(known_users)//2} userów z {users_path}")
            except Exception as e:
                self.stderr.write(f"Ostrzeżenie: nie można otworzyć users-db: {e}")

        # Supplement from posts table (author_name column)
        if not known_users:
            for row in conn.execute("SELECT DISTINCT author_name FROM posts WHERE author_name IS NOT NULL"):
                name = row[0]
                known_users[name]        = name
                known_users[name.lower()] = name
            self.stdout.write(f"Załadowano {len(known_users)//2} userów z posts.author_name")

        # Skip main enrichment loop when running a standalone auxiliary pass
        _aux_only = (do_bible or do_search_nf) and not only_unfound and not reset
        run_main_loop = not _aux_only

        # --- Count posts to process ---
        if only_unfound:
            total_posts = conn.execute(
                "SELECT count(*) FROM posts WHERE quote_status IN (2,3)"
            ).fetchone()[0]
            self.stdout.write(f"Postów do ponownego przetworzenia (status 2+3): {total_posts}")
            unfound_ids = {
                row[0] for row in
                conn.execute("SELECT post_id FROM posts WHERE quote_status IN (2,3)")
            }
        else:
            total_posts = conn.execute(
                "SELECT count(*) FROM posts WHERE content LIKE '%[quote%'"
            ).fetchone()[0]
            self.stdout.write(f"Postów z cytatami: {total_posts}")
            unfound_ids = None

        # --- Build global index for cross-topic fallback (50 preceding posts by post_id) ---
        # Load all posts once; only stripped content needed for matching
        _GLOBAL_FALLBACK_N = 250
        global_sorted: list[dict] = []   # sorted by post_id ASC
        global_post_ids: list[int] = []  # parallel list for bisect

        for row in conn.execute(
            "SELECT post_id, author_name, content, created_at FROM posts ORDER BY post_id ASC"
        ):
            raw = row[2] or ""
            s   = _strip_bbcode(raw)
            d = {
                "post_id":      row[0],
                "author_name":  row[1] or "",
                "content":      raw,
                "created_at":   row[3] or "",
                "stripped":     s,
                "stripped_ascii": _to_ascii(s),
            }
            global_sorted.append(d)
            global_post_ids.append(row[0])

        # --- Process topic by topic ---
        if run_main_loop:
            topics = [
                row[0] for row in
                conn.execute("SELECT DISTINCT topic_id FROM posts ORDER BY topic_id")
            ]
            self.stdout.write(f"Tematów: {len(topics)}")
        else:
            topics = []

        processed = found_count = not_found_count = 0
        all_quote_rows: list[dict] = []

        for topic_id in topics:
            topic_posts = conn.execute(
                "SELECT post_id, author_name, content, content_quotes, quote_status, created_at "
                "FROM posts WHERE topic_id=? ORDER BY post_order ASC, post_id ASC",
                (topic_id,)
            ).fetchall()

            # Set of post_ids in this topic — used to exclude from global fallback
            topic_post_id_set = {row["post_id"] for row in topic_posts}

            # window: list of post dicts for this topic (all preceding posts)
            window: list[dict] = []

            updates: list[tuple[str, int]] = []

            for row in topic_posts:
                post_id      = row["post_id"]
                content      = row["content"] or ""
                content_q    = row["content_quotes"] or ""
                post_qs      = row["quote_status"] or 0

                should_process = (
                    "[quote" in content.lower() and
                    (unfound_ids is None or post_id in unfound_ids)
                )

                if should_process:
                    # Cross-topic fallback: N posts globally preceding this post_id,
                    # excluding posts from the same topic (already in window)
                    idx = bisect.bisect_left(global_post_ids, post_id)
                    fallback_slice = global_sorted[max(0, idx - _GLOBAL_FALLBACK_N): idx]
                    global_fallback = [p for p in fallback_slice
                                       if p["post_id"] not in topic_post_id_set]

                    short_window = window[-_SHORT_FP_WINDOW:] if window else None

                    # For status=3 (mixed), use content_quotes as base so already-found
                    # enriched tags [quote=X post_id=N time=T] are preserved and skipped
                    # by _extract_top_quotes (which only matches plain [quote]/[quote="X"]).
                    # For status=0/2 (none found or fresh), use original content.
                    if only_unfound and post_qs == 3 and content_q:
                        base_content = content_q
                    else:
                        base_content = content

                    quotes_for_post: list[dict] = []
                    enriched = _enrich_content(
                        post_id, base_content, window, known_users,
                        quotes_for_post, global_fallback or None, short_window,
                    )
                    updates.append((enriched, post_id))
                    for q in quotes_for_post:
                        if q["found"]:
                            found_count += 1
                        else:
                            not_found_count += 1
                    if only_unfound:
                        if post_qs == 3:
                            # Preserve already-found citations; only replace unfound ones
                            conn.execute(
                                "DELETE FROM quotes WHERE citing_post_id=? AND found=0",
                                (post_id,)
                            )
                        else:
                            conn.execute(
                                "DELETE FROM quotes WHERE citing_post_id=?", (post_id,)
                            )
                    all_quote_rows.extend(quotes_for_post)
                    processed += 1

                # Add current post to window AFTER processing (can't quote yourself)
                _s = _strip_bbcode(content)
                window.append({
                    "post_id":       post_id,
                    "author_name":   row["author_name"] or "",
                    "content":       content,
                    "created_at":    row["created_at"] or "",
                    "stripped":      _s,
                    "stripped_ascii": _to_ascii(_s),
                })
                # lookahead=0 means full topic (no limit)
                if lookahead and len(window) > lookahead:
                    window.pop(0)

            # Batch-write enriched content
            if updates:
                conn.executemany(
                    "UPDATE posts SET content_quotes=? WHERE post_id=?",
                    updates
                )

        # Write quotes table
        conn.executemany(
            "INSERT INTO quotes (citing_post_id, cited_post_id, quote_author, canonical_author, found, is_foreign) "
            "VALUES (:citing_post_id, :cited_post_id, :quote_author, :canonical_author, :found, :is_foreign)",
            all_quote_rows
        )

        # Add quote_status column if missing, then populate
        cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        if "quote_status" not in cols:
            conn.execute("ALTER TABLE posts ADD COLUMN quote_status INTEGER NOT NULL DEFAULT 0")
        else:
            conn.execute("UPDATE posts SET quote_status = 0")   # reset before repopulating

        conn.execute("""
            UPDATE posts SET quote_status = (
                SELECT
                    CASE
                        WHEN sum(found) = count(*) THEN 1
                        WHEN sum(found) = 0         THEN 2
                        ELSE                              3
                    END
                FROM quotes q
                WHERE q.citing_post_id = posts.post_id
            )
            WHERE post_id IN (SELECT DISTINCT citing_post_id FROM quotes)
        """)

        conn.commit()

        # Optional search-not-found pass
        if do_search_nf:
            nf_found = self._search_not_found_by_author(conn, self.stdout)
            conn.commit()
            self.stdout.write(f"Search-not-found: znaleziono {nf_found} cytatów.")

        # Optional Bible detection pass
        if do_bible and _BIBLE_NGRAM_INDEX:
            bible_count = self._detect_bible_quotes(conn)
            conn.commit()
            self.stdout.write(f"Biblia: znaleziono {bible_count} cytatów biblijnych.")

        # Optional propagation pass
        if do_propagate:
            prop_count = _propagate_chain_quotes(conn, self.stdout)
            conn.commit()
            self.stdout.write(f"Propagacja: wzbogacono {prop_count} zagnieżdżonych cytatów.")

        conn.close()

        total_quotes = found_count + not_found_count
        pct = (found_count / total_quotes * 100) if total_quotes else 0
        self.stdout.write(self.style.SUCCESS(
            f"\nGotowe!\n"
            f"  Postów przetworzonych: {processed}\n"
            f"  Cytatów ogółem:        {total_quotes}\n"
            f"  Znaleziono:            {found_count} ({pct:.1f}%)\n"
            f"  Nieznalezionych:       {not_found_count}\n"
            f"\nNastępny krok — przejrzyj nieznalezione:\n"
            f"  SELECT q.*, substr(p.content,1,200) FROM quotes q\n"
            f"  JOIN posts p ON q.citing_post_id=p.post_id\n"
            f"  WHERE q.found=0 LIMIT 20;"
        ))

    # -----------------------------------------------------------------------
    # Bible detection pass
    # -----------------------------------------------------------------------

    def _detect_bible_quotes(self, conn) -> int:
        """Scan posts with unfound quotes; replace matching [quote] blocks with [Bible=ref].

        Returns total number of Bible tags inserted.
        """
        # Regex for Bible tag detection (already tagged from a previous pass)
        _BIBLE_TAG_RE = re.compile(r'\[Bible=[^\]]*\]', re.IGNORECASE)

        # Posts that still have unfound quotes
        post_rows = conn.execute(
            "SELECT post_id, content_quotes, content "
            "FROM posts WHERE quote_status IN (2, 3)"
        ).fetchall()

        # Regex patterns for counting found/unfound tags in content_quotes
        _UNFOUND_TAG_RE = re.compile(
            r'\[quote(?:="[^"]*")?\]',   # [quote] or [quote="X"] — no post_id
            re.IGNORECASE
        )
        _FOUND_TAG_RE = re.compile(
            r'\[(?:quote[^\]]+post_id=|Bible=)[^\]]*\]',  # [quote ... post_id=] or [Bible=...]
            re.IGNORECASE
        )

        updates = []
        bible_total = 0

        for row in post_rows:
            post_id  = row[0]
            base     = row[1] or row[2] or ""
            if not base:
                continue

            blocks = _extract_top_quotes(base)
            if not blocks:
                continue

            new_content = base
            offset = 0
            found_any = False

            for block_start, block_end, tag_end, raw_username in blocks:
                inner = base[tag_end:block_end - len("[/quote]")]
                ref = _lookup_bible(inner)
                if ref is None:
                    continue

                # Build [Bible=ref] replacement tag
                new_tag = f"[Bible={ref}]"
                # Replace the opening tag only; keep inner content and [/quote]
                adj_start = block_start + offset
                adj_tag_end = tag_end + offset
                new_content = (
                    new_content[:adj_start]
                    + new_tag
                    + new_content[adj_tag_end:]
                )
                offset += len(new_tag) - (tag_end - block_start)
                bible_total += 1
                found_any = True

            if found_any:
                updates.append((new_content, post_id))
                # Save updated content_quotes
                conn.execute(
                    "UPDATE posts SET content_quotes=? WHERE post_id=?",
                    (new_content, post_id)
                )
                # Recompute quote_status from tag counts in new content_quotes
                # (Don't touch quotes table — we can't map blocks to rows reliably)
                n_unfound = len(_UNFOUND_TAG_RE.findall(new_content))
                n_found   = len(_FOUND_TAG_RE.findall(new_content))
                if n_unfound == 0 and n_found > 0:
                    status = 1
                elif n_found == 0:
                    status = 2
                else:
                    status = 3
                conn.execute(
                    "UPDATE posts SET quote_status=? WHERE post_id=?",
                    (status, post_id)
                )

        self.stdout.write(f"  Zaktualizowano {len(updates)} postów ({bible_total} tagów [Bible]).")
        return bible_total

    # -----------------------------------------------------------------------
    # Search not-found quotes by author (unlimited distance)
    # -----------------------------------------------------------------------

    def _search_not_found_by_author(self, conn, stdout) -> int:
        """Try to find real posts for [quote=Author post_id=not_found] blocks.

        Builds a per-author full-text index from content_user, then for each
        not_found block searches ALL posts by that author using fingerprints
        (same algorithm as the main pass, but without distance limit).

        Returns number of blocks successfully resolved.
        """
        stdout.write("Budowanie indeksu per-autor z content_user...")

        # author_lower → list of post dicts (stripped content_user)
        author_index: dict[str, list[dict]] = {}
        for row in conn.execute(
            "SELECT post_id, author_name, content_user, created_at FROM posts "
            "WHERE content_user IS NOT NULL AND content_user != ''"
        ):
            author = (row[1] or "").lower()
            if not author:
                continue
            s = _strip_bbcode(row[2] or "")
            author_index.setdefault(author, []).append({
                "post_id":       row[0],
                "author_name":   row[1] or "",
                "stripped":      s,
                "stripped_ascii": _to_ascii(s),
                "created_at":    row[3] or "",
            })

        total_authors = len(author_index)
        total_posts_indexed = sum(len(v) for v in author_index.values())
        stdout.write(
            f"  {total_authors} autorów, {total_posts_indexed:,} postów zaindeksowanych."
        )

        # Find all posts with not_found blocks
        nf_posts = conn.execute(
            "SELECT post_id, content_quotes FROM posts "
            "WHERE content_quotes LIKE '%post_id=not_found%'"
        ).fetchall()
        stdout.write(f"  Postów z not_found do przeszukania: {len(nf_posts)}")

        found_total = 0
        post_updates: list[tuple[str, int]] = []

        # Regex to find all not_found opening tags with their author
        _NF_OPEN_RE  = _NOT_FOUND_TAG_RE   # [quote=Author post_id=not_found]
        _CLOSE_RE    = re.compile(r'\[/quote\]', re.IGNORECASE)
        _ANY_OPEN_RE = _QUOTE_ANY_OPEN_RE  # any [quote...] opening

        for citing_post_id, cq in nf_posts:
            if not cq:
                continue

            # Find all not_found blocks using a depth-aware scan
            # We need to pair each not_found opening tag with its [/quote]
            new_cq = cq
            offset = 0
            changed = False

            # Collect all events: (pos, kind, tag_text, end)
            events = []
            for m in _ANY_OPEN_RE.finditer(cq):
                events.append((m.start(), "open", m.group(0), m.end()))
            for m in _CLOSE_RE.finditer(cq):
                events.append((m.start(), "close", None, m.end()))
            events.sort(key=lambda x: x[0])

            # Walk events to find top-level not_found blocks
            depth = 0
            block_start = tag_end = -1
            opening_tag = ""

            blocks: list[tuple[int, int, int, str]] = []  # (block_start, block_end, tag_end, opening_tag)
            for pos, kind, tag_text, end in events:
                if kind == "open":
                    if depth == 0:
                        block_start = pos
                        tag_end     = end
                        opening_tag = tag_text or ""
                    depth += 1
                else:
                    if depth > 0:
                        depth -= 1
                        if depth == 0 and block_start >= 0:
                            blocks.append((block_start, end, tag_end, opening_tag))
                            block_start = -1

            for block_start, block_end, tag_end, opening_tag in blocks:
                # Only process not_found blocks
                nf_m = _NF_OPEN_RE.match(opening_tag)
                if not nf_m:
                    continue
                raw_author = nf_m.group("author")
                author_key = raw_author.lower()

                posts_by_author = author_index.get(author_key, [])
                if not posts_by_author:
                    continue

                inner = cq[tag_end: block_end - len("[/quote]")]
                fps   = _fingerprints(inner)
                if not fps:
                    continue

                # Search all posts by this author — no distance limit
                match = _match_post(fps, posts_by_author, raw_author)
                if match is None:
                    continue

                # Found — build replacement tag
                cited_id  = match["post_id"]
                unix_time = _to_unix(match["created_at"])
                new_tag   = f"[quote={raw_author} post_id={cited_id} time={unix_time}]"

                # Apply replacement in new_cq (adjusting for prior offset changes)
                adj_start   = block_start + offset
                adj_tag_end = tag_end    + offset
                new_cq = (
                    new_cq[:adj_start]
                    + new_tag
                    + new_cq[adj_tag_end:]
                )
                offset  += len(new_tag) - (tag_end - block_start)
                found_total += 1
                changed = True

                # Update quotes table: mark the corresponding row as found
                conn.execute(
                    "UPDATE quotes SET found=1, cited_post_id=? "
                    "WHERE citing_post_id=? AND found=0 AND canonical_author=? "
                    "ORDER BY id LIMIT 1",
                    (cited_id, citing_post_id, raw_author)
                )

            if changed:
                # Recompute quote_status from tag counts
                _UNFOUND = re.compile(
                    r'\[quote(?:="[^"]*")?\]|\[quote=[^ \]]+\s+post_id=not_found\]',
                    re.IGNORECASE
                )
                _FOUND   = re.compile(
                    r'\[(?:quote[^\]]+post_id=(?!not_found)|Bible=)[^\]]*\]',
                    re.IGNORECASE
                )
                n_u = len(_UNFOUND.findall(new_cq))
                n_f = len(_FOUND.findall(new_cq))
                if n_u == 0 and n_f > 0:
                    status = 1
                elif n_f == 0:
                    status = 2
                else:
                    status = 3
                conn.execute(
                    "UPDATE posts SET content_quotes=?, quote_status=? WHERE post_id=?",
                    (new_cq, status, citing_post_id)
                )
                post_updates.append(citing_post_id)

        stdout.write(
            f"  Zaktualizowano {len(post_updates)} postów, "
            f"znaleziono {found_total} cytatów."
        )
        return found_total
