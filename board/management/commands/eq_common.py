#!/usr/bin/env python3
"""Shared utilities for enrich_quotes passes."""
import argparse
import bisect
import pickle
import re
import sqlite3
import sys
import unicodedata

DB_PATH = "/home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_full.db"
USERS_DB_PATH = "/home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_users_real.db"

PASS_TYPES = ['known-user', 'known-user-global', 'anon-topic', 'anon-global', 'ngram',
              'propagate', 'bible', 'bible-filter', 'bible-review-apply',
              'mark-not-found', 'to-fquote', 'fix-status', 'mark-broken',
              'fix-quote-authors', 'fix-quote-post-ids', 'analyze-depth']
# known-user:        szuka w N poprzednich postach tego samego autora w tym samym wątku
# known-user-global: szuka w N poprzednich postach tego samego autora w całej bazie
# anon-topic:        szuka w N poprzednich postach tego samego wątku (dowolny autor)
# anon-global:       szuka w N poprzednich postach całej bazy (dowolny autor)
# ngram:             5-gram voting po content_user, dowolna odległość, najbliższy remis
# propagate:         propaguje post_id do zagnieżdżonych cytatów na podstawie tabeli quotes
# bible:             wykrywa cytaty biblijne przez n-gram indeks, zamienia na [Bible=ref]
# fix-quote-authors: naprawia autora w [quote ... post_id=N] na autora posta N
# fix-quote-post-ids: cofa post_id po łańcuchu cytatów, gdy tekst jest tylko w cytacie

# ---------------------------------------------------------------------------
# BBCode parser – handles nested quotes correctly
# ---------------------------------------------------------------------------

# Tags we preserve during normalization (case-insensitive names)
PRESERVE_TAGS = {"quote", "fquote", "bible"}

# Regex to find any BBCode tag (opening, closing, or self-closing-ish)
_BBCODE_TAG_RE = re.compile(
    r'\[(/?)(\w+)(?:[^\]]*?)\]',
    re.IGNORECASE,
)

# Opening quote-like tag (quote or fquote)
_QUOTE_OPEN_RE = re.compile(
    r'\[(quote|fquote|Bible)(?:[^\]]*?)\]',
    re.IGNORECASE,
)

# Closing quote-like tag
_QUOTE_CLOSE_RE = re.compile(
    r'\[/(quote|fquote|Bible)\]',
    re.IGNORECASE,
)

# Already-enriched tag: [quote="Author" post_id=123] or [quote post_id=123]
_ENRICHED_TAG_RE = re.compile(r'\[quote[^\]]*post_id=\d+', re.IGNORECASE)

# Named quote opening tag: [quote="Author Name"]
_NAMED_QUOTE_RE = re.compile(
    r'\[quote="([^"]+)"\]',
    re.IGNORECASE,
)

# Ellipsis patterns that indicate skipped text in quotes
_ELLIPSIS_RE = re.compile(
    r'(?:/\.\.\./|\(\.\.\.\)|\.\.\.|…)',
)


def _strip_diacritics(text):
    """Remove diacritical marks (ą→a, ś→s, etc.)."""
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


def _strip_bbcode_tags(text):
    """Remove BBCode tags except quote/fquote/Bible."""
    def _replace(m):
        tag_name = m.group(2).lower()
        if tag_name in PRESERVE_TAGS:
            return m.group(0)
        return ''
    return _BBCODE_TAG_RE.sub(_replace, text)


def normalize_text(text):
    """Normalize text for comparison: strip non-preserved BBCode tags,
    remove diacritics, collapse whitespace."""
    text = _strip_bbcode_tags(text)
    text = _strip_diacritics(text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ---------------------------------------------------------------------------
# Proper BBCode quote parser (handles nesting)
# ---------------------------------------------------------------------------

class QuoteBlock:
    """Represents a [quote="Author"]...[/quote] block in the content."""
    __slots__ = ('start', 'end', 'tag_end', 'author', 'inner_start',
                 'inner_end', 'inner_text', 'depth')

    def __init__(self, start, end, tag_end, author, close_tag_len, depth=0):
        self.start = start           # position of [ in [quote=
        self.end = end               # position after ] in [/quote]
        self.tag_end = tag_end       # position after ] in opening [quote="..."]
        self.author = author         # author name or None
        self.inner_start = tag_end   # content starts after opening tag
        self.inner_end = end - close_tag_len  # content ends before closing tag
        self.depth = depth


def parse_quotes(content):
    """Parse all quote blocks at all nesting levels.
    Returns list of QuoteBlock sorted by start position.
    Only returns [quote="Author"] blocks (named quotes).
    """
    # Gather all opening and closing events
    events = []
    for m in _QUOTE_OPEN_RE.finditer(content):
        # Extract author if present
        nm = _NAMED_QUOTE_RE.match(content, m.start())
        author = nm.group(1) if nm else None
        events.append((m.start(), 'open', m.end(), author, m.group(0)))
    for m in _QUOTE_CLOSE_RE.finditer(content):
        events.append((m.start(), 'close', m.end(), None, m.group(0)))

    events.sort(key=lambda x: x[0])

    # Stack-based parsing
    stack = []  # (start, tag_end, author, tag_text)
    result = []

    for pos, kind, end, author, tag_text in events:
        if kind == 'open':
            stack.append((pos, end, author, tag_text))
        elif kind == 'close' and stack:
            open_pos, open_tag_end, open_author, open_tag_text = stack.pop()
            close_tag_len = end - pos
            depth = len(stack)
            block = QuoteBlock(
                start=open_pos,
                end=end,
                tag_end=open_tag_end,
                author=open_author,
                close_tag_len=close_tag_len,
                depth=depth,
            )
            result.append(block)

    result.sort(key=lambda b: b.start)
    return result


def extract_quote_text(content, block):
    """Extract the text inside a quote block, excluding nested sub-quotes."""
    inner = content[block.inner_start:block.inner_end]

    # Remove nested quote blocks from inner text
    # We need to re-parse within the inner text to find nested blocks
    nested_events = []
    for m in _QUOTE_OPEN_RE.finditer(inner):
        nested_events.append((m.start(), 'open', m.end()))
    for m in _QUOTE_CLOSE_RE.finditer(inner):
        nested_events.append((m.start(), 'close', m.end()))
    nested_events.sort(key=lambda x: x[0])

    if not nested_events:
        return inner

    # Build ranges to exclude (nested quote blocks)
    exclude_ranges = []
    nstack = []
    for npos, nkind, nend in nested_events:
        if nkind == 'open':
            nstack.append(npos)
        elif nkind == 'close' and nstack:
            nstart = nstack.pop()
            if len(nstack) == 0:
                exclude_ranges.append((nstart, nend))

    # Build text excluding nested quotes
    parts = []
    prev = 0
    for ex_start, ex_end in exclude_ranges:
        parts.append(inner[prev:ex_start])
        prev = ex_end
    parts.append(inner[prev:])

    return ''.join(parts)


# ---------------------------------------------------------------------------
# Text matching: does the quote text appear in the candidate post?
# ---------------------------------------------------------------------------

def _split_on_ellipsis(text):
    """Split quote text on ellipsis markers, returning non-empty fragments."""
    parts = _ELLIPSIS_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def match_quote_in_post(quote_text_norm, post_content_norm, min_fragment_len=15):
    """Check if the normalized quote text can be found in the normalized post content.
    Handles ellipsis-skipped fragments: each fragment must appear in order."""
    if not quote_text_norm or not post_content_norm:
        return False

    fragments = _split_on_ellipsis(quote_text_norm)

    if not fragments:
        return False

    # Filter out very short fragments (likely noise)
    # But if there's only one fragment, allow shorter
    if len(fragments) == 1:
        frag = fragments[0]
        if len(frag) < 8:
            return False
        return frag in post_content_norm

    # Multiple fragments: each must appear in order
    search_from = 0
    matched_count = 0
    for frag in fragments:
        if len(frag) < 5:
            # Skip very short fragments between ellipses
            continue
        idx = post_content_norm.find(frag, search_from)
        if idx == -1:
            return False
        search_from = idx + len(frag)
        matched_count += 1

    return matched_count > 0


# ---------------------------------------------------------------------------
# Main enrichment logic
# ---------------------------------------------------------------------------

def load_known_users(users_db_path):
    """Load usernames from sfinia_users_real.db, return as lowercase set."""
    conn = sqlite3.connect(users_db_path)
    rows = conn.execute("SELECT username FROM users").fetchall()
    conn.close()
    return {row[0].lower() for row in rows}


def build_author_cache(conn, known_users):
    """Load all posts by known users into memory.

    Returns dict: (topic_id, author_lower) -> list of (post_order, post_id, content_norm)
    sorted ascending by post_order (for bisect lookups).
    """
    print("Ładuję cache postów do pamięci...", flush=True)
    cache = {}
    total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    loaded = 0

    cursor = conn.execute(
        "SELECT post_id, topic_id, lower(author_name), post_order, content FROM posts"
        " ORDER BY topic_id, lower(author_name), post_order"
    )
    for post_id, topic_id, author_lower, post_order, content in cursor:
        if author_lower not in known_users:
            continue
        key = (topic_id, author_lower)
        entry = (post_order, post_id, normalize_text(content))
        if key not in cache:
            cache[key] = []
        cache[key].append(entry)
        loaded += 1
        if loaded % 50000 == 0:
            print(f"  {loaded:,} postów załadowanych...", flush=True)

    print(f"  Cache gotowy: {loaded:,} postów znanych userów w {len(cache):,} grupach (topic, autor)")
    return cache


def cache_lookup(cache, topic_id, author_lower, current_post_order, lookback):
    """Return up to `lookback` posts by author in topic before current_post_order.

    Returns list of (post_id, content_norm) in descending post_order (newest first).
    """
    entries = cache.get((topic_id, author_lower))
    if not entries:
        return []
    # entries is sorted ascending by post_order
    idx = bisect.bisect_left(entries, (current_post_order,))
    start = max(0, idx - lookback)
    return [(pid, cnorm) for (_, pid, cnorm) in reversed(entries[start:idx])]


def build_global_cache(conn, known_users):
    """Load all posts by known users into memory, indexed globally by author.

    Returns dict: author_lower -> list of (post_id, content_norm)
    sorted ascending by post_id (chronological order across all topics).
    """
    print("Ładuję globalny cache postów do pamięci...", flush=True)
    cache = {}
    loaded = 0

    cursor = conn.execute(
        "SELECT post_id, lower(author_name), content FROM posts"
        " ORDER BY lower(author_name), post_id"
    )
    for post_id, author_lower, content in cursor:
        if author_lower not in known_users:
            continue
        entry = (post_id, normalize_text(content))
        if author_lower not in cache:
            cache[author_lower] = []
        cache[author_lower].append(entry)
        loaded += 1
        if loaded % 50000 == 0:
            print(f"  {loaded:,} postów załadowanych...", flush=True)

    print(f"  Cache globalny gotowy: {loaded:,} postów w {len(cache):,} autorach")
    return cache


def global_cache_lookup(gcache, author_lower, current_post_id, lookback):
    """Return up to `lookback` posts by author before current_post_id (globally).

    Returns list of (post_id, content_norm) newest first.
    """
    entries = gcache.get(author_lower)
    if not entries:
        return []
    # entries sorted ascending by post_id
    idx = bisect.bisect_left(entries, (current_post_id,))
    start = max(0, idx - lookback)
    return list(reversed(entries[start:idx]))


def build_topic_cache_all(conn):
    """Load all posts indexed by topic_id (any author).

    Returns dict: topic_id -> list of (post_order, post_id, author, content_norm)
    sorted ascending by post_order.
    """
    print("Ładuję cache per-temat (wszyscy autorzy)...", flush=True)
    cache = {}
    loaded = 0
    cursor = conn.execute(
        "SELECT post_id, topic_id, author_name, post_order, content"
        " FROM posts ORDER BY topic_id, post_order"
    )
    for post_id, topic_id, author, post_order, content in cursor:
        entry = (post_order, post_id, author or '', normalize_text(content))
        if topic_id not in cache:
            cache[topic_id] = []
        cache[topic_id].append(entry)
        loaded += 1
        if loaded % 50000 == 0:
            print(f"  {loaded:,} postów załadowanych...", flush=True)
    print(f"  Cache per-temat gotowy: {loaded:,} postów w {len(cache):,} wątkach")
    return cache


def topic_cache_all_lookup(cache, topic_id, current_post_order, lookback):
    """Return up to `lookback` posts in topic before current_post_order (any author).

    Returns list of (post_id, author, content_norm) newest first.
    """
    entries = cache.get(topic_id)
    if not entries:
        return []
    idx = bisect.bisect_left(entries, (current_post_order,))
    start = max(0, idx - lookback)
    return [(pid, auth, cnorm) for (_, pid, auth, cnorm) in reversed(entries[start:idx])]


def build_global_cache_all(conn):
    """Load all posts globally (any author) sorted by post_id.

    Returns list of (post_id, author, content_norm) sorted ascending.
    """
    print("Ładuję globalny cache (wszyscy autorzy)...", flush=True)
    entries = []
    loaded = 0
    cursor = conn.execute(
        "SELECT post_id, author_name, content FROM posts ORDER BY post_id"
    )
    for post_id, author, content in cursor:
        entries.append((post_id, author or '', normalize_text(content)))
        loaded += 1
        if loaded % 50000 == 0:
            print(f"  {loaded:,} postów załadowanych...", flush=True)
    print(f"  Cache globalny (wszyscy) gotowy: {loaded:,} postów")
    return entries


def global_cache_all_lookup(entries, current_post_id, lookback):
    """Return up to `lookback` posts before current_post_id (any author).

    Returns list of (post_id, author, content_norm) newest first.
    """
    idx = bisect.bisect_left(entries, (current_post_id,))
    start = max(0, idx - lookback)
    return list(reversed(entries[start:idx]))


_MAX_POSTS_PER_GRAM = 5   # 5-gramy pojawiające się w >5 postach → zbyt pospolite, pomijane
_NGRAM_SIZE = 5
_NGRAM_MIN_VOTES = 2      # wymagane min. głosów (≥1 dla bardzo krótkich cytatów)
_NGRAM_MAX_GRAMS_TRIED = 80  # ile 5-gramów sprawdzamy na cytat (stride 2)


def _words(text):
    """Tokenizuj znormalizowany tekst na słowa (alnum)."""
    return re.findall(r'[a-z0-9]+', text.lower())


def build_ngram_index(conn):
    """Buduj 5-gram index z content_user wszystkich postów.

    Zwraca (ngram_index, post_author):
      ngram_index: dict str -> list[int post_id]  (max MAX_POSTS_PER_GRAM wpisów)
      post_author: dict int -> str  (post_id -> author_name)
    """
    print("Budowanie 5-gram indeksu z content_user...", flush=True)
    import time as _time
    t0 = _time.time()

    ngram_index = {}
    post_author = {}
    loaded = 0

    for row in conn.execute(
        "SELECT post_id, author_name, content_user FROM posts"
        " WHERE content_user IS NOT NULL AND content_user != ''"
    ):
        pid, author, cu = row[0], row[1] or '', row[2] or ''
        post_author[pid] = author
        ws = _words(normalize_text(cu))
        for i in range(len(ws) - _NGRAM_SIZE + 1):
            key = ' '.join(ws[i:i + _NGRAM_SIZE])
            lst = ngram_index.get(key)
            if lst is None:
                ngram_index[key] = [pid]
            elif len(lst) < _MAX_POSTS_PER_GRAM:
                lst.append(pid)
        loaded += 1
        if loaded % 50000 == 0:
            print(f"  {loaded:,} postów zaindeksowanych...", flush=True)

    t1 = _time.time()
    print(f"  {len(ngram_index):,} unikalnych 5-gramów, {loaded:,} postów, {t1-t0:.1f}s")
    return ngram_index, post_author


def ngram_lookup(ngram_index, post_author, quote_norm, current_post_id):
    """Znajdź post źródłowy cytatu przez 5-gram voting.

    Zwraca (post_id, author) lub (None, None).
    Wśród kandydatów z tą samą liczbą głosów wybiera najbliższy (największy post_id < current).
    """
    ws = _words(quote_norm)
    if len(ws) < _NGRAM_SIZE:
        return None, None

    votes = {}
    grams_tried = 0
    for i in range(0, len(ws) - _NGRAM_SIZE + 1, 2):  # stride 2 dla szybkości
        key = ' '.join(ws[i:i + _NGRAM_SIZE])
        pids = ngram_index.get(key)
        if pids:
            for p in pids:
                if p < current_post_id:  # tylko wcześniejsze posty
                    votes[p] = votes.get(p, 0) + 1
        grams_tried += 1
        if grams_tried >= _NGRAM_MAX_GRAMS_TRIED:
            break

    if not votes:
        return None, None

    min_votes = 1 if grams_tried <= 3 else _NGRAM_MIN_VOTES
    best_cnt = max(votes.values())
    if best_cnt < min_votes:
        return None, None

    # Wśród remisujących → najbliższy (największy post_id < current_post_id)
    candidates = [p for p, c in votes.items() if c == best_cnt]
    best_pid = max(candidates)  # największy post_id = najbliższy chronologicznie

    return best_pid, post_author.get(best_pid, '')


def create_quotes_table(conn):
    """Create the quotes table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            quoted_user TEXT,
            quoted_user_resolved TEXT,
            source_post_id INTEGER,
            quote_text_preview TEXT,
            quote_index INTEGER NOT NULL,
            found INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (post_id) REFERENCES posts(post_id),
            FOREIGN KEY (source_post_id) REFERENCES posts(post_id),
            UNIQUE (post_id, quote_index)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_quotes_post_id ON quotes(post_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_quotes_source ON quotes(source_post_id)
    """)
    # nested_status: 0=niezbadane, 1=wszystkie zagnieżdżone OK, 2=część nierozwiązana
    try:
        conn.execute("ALTER TABLE posts ADD COLUMN nested_status INTEGER NOT NULL DEFAULT 0")
        print("Dodano kolumnę nested_status do posts")
    except Exception:
        pass  # już istnieje
    conn.commit()
