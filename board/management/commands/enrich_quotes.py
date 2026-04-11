#!/usr/bin/env python3
"""Enrich [quote="User"] tags with post_id by matching quote text
against previous posts in the same thread by the same author.

Pass types:
  --pass known-user   (default) Only quotes where author is in sfinia_users_real.db
                      Future passes will handle unknown/misspelled authors, anonymous quotes, etc.

Usage:
    python enrich_quotes.py --pass known-user [--lookback 20] [--dry-run] [--limit N] [--reset]
"""
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

_BIBLE_NGRAM_INDEX = None       # załadowany przez --bible-index
_BIBLE_COVERAGE    = 0.40       # minimalny % n-gramów pasujących (--bible-coverage)
_BIBLE_DRY_MIN     = 0.09       # minimalny % do pokazania w dry-run (--bible-dry-min)

# ---------------------------------------------------------------------------
# Bible index helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Pass: propagate – propagacja post_id do zagnieżdżonych cytatów
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


def run_propagate(conn, dry_run=False, need_repair_only=False):
    """Propaguj post_id do zagnieżdżonych cytatów.

    Dla każdego postu z content_quotes:
      - znajdź enriched outer [quote post_id=N]
      - wewnątrz niego znajdź unresolved [quote]
      - sprawdź w tabeli quotes co cytował post N
      - przypisz post_id z tego wpisu

    Iteruje do stabilizacji (dla zagłębień >2).
    Zwraca liczbę wzbogaconych tagów.
    """
    # Zbuduj mapę: post_id → lista (source_post_id, quoted_user_resolved)
    # dla wpisów found=1
    print("Wczytuję mapę cytatów z tabeli quotes...", flush=True)
    citations = {}  # post_id -> [(source_post_id, author), ...]
    for row in conn.execute(
        "SELECT post_id, source_post_id, quoted_user_resolved FROM quotes WHERE found=1"
    ):
        pid, src, auth = row[0], row[1], row[2] or ''
        if pid not in citations:
            citations[pid] = []
        citations[pid].append((src, auth))
    print(f"  {len(citations):,} postów z known citations")

    # Pobierz post_author dla znalezionych source postów
    all_source_ids = set()
    for lst in citations.values():
        for src, _ in lst:
            all_source_ids.add(src)

    total_enriched = 0
    iteration = 0

    while True:
        iteration += 1
        enriched_this_iter = 0

        # Posty gdzie nested_status != 1 i content_quotes zawiera zagnieżdżone
        extra = " AND need_repair_quotes=1 AND quote_status IN (1,3)" if need_repair_only else ""
        rows = conn.execute(
            "SELECT post_id, content_quotes FROM posts"
            f" WHERE content_quotes IS NOT NULL AND (nested_status IS NULL OR nested_status != 1){extra}"
        ).fetchall()

        updates = []  # (new_content_quotes, nested_status, post_id)
        quote_inserts = []

        for post_id, cq in rows:
            # Szukaj enriched outer tagów
            outer_matches = list(_ENRICHED_OPEN_RE.finditer(cq))
            if not outer_matches:
                updates.append((cq, 1, post_id))
                continue

            # Parsuj strukturę zagnieżdżeń
            events = []
            for m in _QUOTE_OPEN_RE.finditer(cq):
                events.append((m.start(), 'open', m.end(), m.group(0)))
            for m in _QUOTE_CLOSE_RE.finditer(cq):
                events.append((m.start(), 'close', m.end(), m.group(0)))
            events.sort(key=lambda x: x[0])

            # Znajdź bloki (start, end, tag_end, opening_tag, depth_when_opened)
            stack = []
            blocks = []
            for pos, kind, end, tag_text in events:
                if kind == 'open':
                    stack.append((pos, end, tag_text))
                elif kind == 'close' and stack:
                    open_pos, open_tag_end, open_tag_text = stack.pop()
                    depth = len(stack)
                    blocks.append((open_pos, end, open_tag_end, open_tag_text, depth, len(tag_text)))

            # Dla każdego enriched outer bloku (depth=0): szukaj unresolved wewnątrz
            new_cq = cq
            offset = 0
            changed = False
            has_unresolved = False

            for b_start, b_end, b_tag_end, b_tag, b_depth, b_close_len in blocks:
                if b_depth != 0:
                    continue
                m = _ENRICHED_OPEN_RE.match(b_tag)
                if not m:
                    # Outer unresolved - nie obsługujemy tu
                    if _UNRESOLVED_OPEN_RE.match(b_tag):
                        has_unresolved = True
                    continue

                outer_cited_pid = int(m.group('post_id'))
                outer_cits = citations.get(outer_cited_pid, [])
                if not outer_cits:
                    continue

                # Buduj mapę author_lower → (source_pid, author)
                # Jeśli jeden cytat → można przypisać bez dopasowania autora
                author_map = {}  # author_lower -> (src_pid, auth) lub None jeśli ambig
                for src_pid, auth in outer_cits:
                    key = auth.lower() if auth else '__anon__'
                    if key in author_map:
                        author_map[key] = None  # ambiguous
                    else:
                        author_map[key] = (src_pid, auth)

                # Znajdź unresolved tagi wewnątrz tego outer bloku
                inner_start = b_tag_end
                inner_end = b_end - b_close_len

                # Szukaj unresolved bloków wewnętrznych
                for ib_start, ib_end, ib_tag_end, ib_tag, ib_depth, ib_close_len in blocks:
                    if ib_depth != 1:
                        continue
                    if ib_start < inner_start or ib_end > b_end:
                        continue
                    if not _UNRESOLVED_OPEN_RE.match(ib_tag):
                        continue

                    im = _UNRESOLVED_OPEN_RE.match(ib_tag)
                    inner_author = (im.group('author') or '').strip()
                    key = inner_author.lower() if inner_author else '__anon__'

                    hit = author_map.get(key)
                    # Jeśli nie ma dokładnego dopasowania a jest tylko jeden cytat
                    if hit is None and len(outer_cits) == 1:
                        hit = outer_cits[0]

                    if hit is None:
                        has_unresolved = True
                        continue

                    src_pid, src_auth = hit
                    # Zachowaj typ tagu (quote/fquote)
                    tag_type = 'fquote' if ib_tag.lower().startswith('[fquote') else 'quote'
                    if inner_author:
                        new_tag = '[%s="%s" post_id=%d]' % (tag_type, inner_author, src_pid)
                    elif src_auth:
                        new_tag = '[%s="%s" post_id=%d]' % (tag_type, src_auth, src_pid)
                    else:
                        new_tag = '[%s post_id=%d]' % (tag_type, src_pid)

                    adj_start = ib_start + offset
                    adj_tag_end = ib_tag_end + offset
                    new_cq = new_cq[:adj_start] + new_tag + new_cq[adj_tag_end:]
                    offset += len(new_tag) - len(ib_tag)
                    changed = True
                    enriched_this_iter += 1

                    quote_inserts.append((post_id, inner_author or None,
                                         src_auth or None, src_pid,
                                         None, -1, 1))

            nested_status = 2 if (has_unresolved or bool(_UNRESOLVED_OPEN_RE.search(new_cq))) else 1
            updates.append((new_cq, nested_status, post_id))

        print(f"  Iteracja {iteration}: wzbogacono {enriched_this_iter} tagów", flush=True)
        total_enriched += enriched_this_iter

        if dry_run:
            break  # w dry-run nie zapisujemy → kolejne iteracje dałyby ten sam wynik

        if updates:
            conn.executemany(
                "UPDATE posts SET content_quotes=?, nested_status=? WHERE post_id=?",
                updates,
            )
            if quote_inserts:
                conn.executemany(
                    """INSERT OR IGNORE INTO quotes
                       (post_id, quoted_user, quoted_user_resolved,
                        source_post_id, quote_text_preview, quote_index, found)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    quote_inserts,
                )
            conn.commit()
            # Zaktualizuj mapę citations o nowe wpisy
            for pid, qu, qur, src, _, _, _ in quote_inserts:
                if pid not in citations:
                    citations[pid] = []
                citations[pid].append((src, qur or ''))

        if enriched_this_iter == 0:
            break  # stabilizacja

    return total_enriched


# ---------------------------------------------------------------------------
# Pass: bible – zamień unresolved [quote] pasujące do Biblii na [Bible=ref]
# ---------------------------------------------------------------------------

_BIBLE_OPEN_RE = re.compile(r'\[Bible=[^\]]*\]', re.IGNORECASE)
# Dowolny otwierający tag cytatowy (quote/fquote/Bible) — do odrzucania bloków z zagnieżdżonymi cytatami
_ANY_QUOTE_OPEN_RE = re.compile(r'\[(?:quote|fquote|Bible)(?:[^\]]*)\]', re.IGNORECASE)
_BIBLE_FOUND_RE = re.compile(
    r'\[(?:quote[^\]]*post_id=\d|Bible=)[^\]]*\]', re.IGNORECASE
)


def run_bible(conn, dry_run=False, review_path=None):
    """Wykryj cytaty biblijne i zamień [quote...]...[/quote] na [Bible=ref]...[/Bible].

    Przetwarza:
      - posty z quote_status IN (2,3): nierozwiązane cytaty top-level
      - posty z nested_status=2: nierozwiązane cytaty zagnieżdżone

    Używa leaf-blocks (bez zagnieżdżonych nierozwiązanych cytatów w środku)
    i zastępuje je od prawej do lewej, by nie psuć pozycji.

    Bloki zawierające wewnątrz inne [quote]/[fquote]/[Bible] są odrzucane.

    Kryteria:
      - auto-tag:  votes >= max(2, ceil(total_grams * _BIBLE_COVERAGE))
      - review:    votes == 1 AND pct >= 25%  (nie dłuższe niż 4 n-gramy)
      - dry-run pokazuje wszystko z votes >= 1 AND pct >= 10%

    Zwraca liczbę wstawionych tagów [Bible=].
    """
    if _BIBLE_NGRAM_INDEX is None:
        print("BŁĄD: Bible index nie załadowany. Użyj --bible-index.")
        return 0

    rows = conn.execute(
        "SELECT post_id, COALESCE(content_quotes, content) FROM posts"
        " WHERE (quote_status IN (2, 3) OR nested_status = 2)"
        "   AND (content_quotes IS NOT NULL OR content LIKE '%[quote%')"
    ).fetchall()

    updates = []
    bible_total = 0
    dry_log = []     # (pct, post_id, ref, votes, total_grams, inner_text, kind)
    review_items = []  # (post_id, ref, votes, total_grams, pct, inner_text)

    for post_id, content in rows:
        if not content:
            continue

        all_blocks = parse_quotes(content)
        if not all_blocks:
            continue

        # Bloki nierozwiązane: brak post_id= i nie [Bible=]
        unresolved = []
        for b in all_blocks:
            raw_open = content[b.start:b.tag_end]
            if not _ENRICHED_TAG_RE.search(raw_open) and not _BIBLE_OPEN_RE.match(raw_open):
                unresolved.append(b)

        if not unresolved:
            continue

        # Bloki liściowe: żaden inny nierozwiązany blok nie jest w ich wnętrzu
        def is_leaf(b):
            for other in unresolved:
                if id(other) == id(b):
                    continue
                if other.start >= b.inner_start and other.end <= b.inner_end:
                    return False
            return True

        leaf_blocks = [b for b in unresolved if is_leaf(b)]

        # Sprawdź każdy liść przez indeks biblijny
        replacements = []
        for b in leaf_blocks:
            inner = content[b.inner_start:b.inner_end]

            # Odrzuć bloki zawierające zagnieżdżone quote/fquote/Bible
            if _ANY_QUOTE_OPEN_RE.search(inner):
                continue

            ref, votes, total_g = _bible_votes(inner)
            if ref is None or votes < 2:
                continue

            pct = votes / max(1, total_g)
            min_votes = max(2, int(total_g * _BIBLE_COVERAGE + 0.9999))  # ceil

            if votes >= min_votes:
                # Automatyczne tagowanie
                replacements.append((b.start, b.tag_end, b.inner_end, b.end, ref))
                if dry_run:
                    dry_log.append((pct, post_id, ref, votes, total_g, inner, 'auto'))
            elif votes == 1 and pct >= 1/12:
                # Do przeglądu ręcznego (1 trafienie, >=25%)
                if dry_run:
                    dry_log.append((pct, post_id, ref, votes, total_g, inner, 'review'))
                else:
                    review_items.append((post_id, ref, votes, total_g, pct, inner))
            elif dry_run and votes > 1 and pct >= _BIBLE_DRY_MIN:
                # Widoczne w dry-run (>=2 trafień, >=9%), poniżej progu auto
                dry_log.append((pct, post_id, ref, votes, total_g, inner, 'skip'))

        if not replacements:
            continue

        if dry_run:
            bible_total += len(replacements)
            continue   # w dry-run nie modyfikujemy bazy

        # Zastąp od prawej do lewej (pozycje z oryginalnego content są wtedy poprawne)
        replacements.sort(key=lambda x: x[0], reverse=True)
        new_content = content
        for start, tag_end, inner_end, end, ref in replacements:
            new_tag   = '[Bible=%s]' % ref
            new_close = '[/Bible]'
            new_content = (
                new_content[:start]
                + new_tag
                + new_content[tag_end:inner_end]
                + new_close
                + new_content[end:]
            )
            bible_total += 1

        # Przelicz quote_status
        n_unresolved = len(_UNRESOLVED_OPEN_RE.findall(new_content))
        n_found      = len(_BIBLE_FOUND_RE.findall(new_content))
        if n_unresolved == 0 and n_found > 0:
            new_quote_status = 1
        elif n_found == 0:
            new_quote_status = 2
        else:
            new_quote_status = 3

        # Przelicz nested_status
        new_nested_status = 2 if _UNRESOLVED_OPEN_RE.search(new_content) else 1

        updates.append((new_content, new_quote_status, new_nested_status, post_id))

    if dry_run and dry_log:
        # Sortuj od największego % do najmniejszego
        dry_log.sort(key=lambda x: x[0], reverse=True)

        # Główny log: n>1, od 100% do 9% (auto + poniżej progu)
        main_items   = [(p, pid, ref, v, g, inn) for p, pid, ref, v, g, inn, k in dry_log
                        if k in ('auto', 'skip')]
        review_shown = [(p, pid, ref, v, g, inn) for p, pid, ref, v, g, inn, k in dry_log
                        if k == 'review']

        if main_items:
            print(f"\n{'pct':>5}  {'post_id':>8}  {'ref':<18}  {'v/g':<10}  tag?  fragment")
            print('-' * 115)
            for pct, pid, ref, votes, total_g, inner in main_items:
                fragment = re.sub(r'\s+', ' ', _strip_bbcode_tags(inner)).strip()[:200]
                min_v = max(2, int(total_g * _BIBLE_COVERAGE + 0.9999))
                tag_mark = 'TAK' if votes >= min_v else '---'
                print(f"{pct*100:>4.0f}%  {pid:>8}  [{ref:<16}]  {votes}/{total_g:<8}  {tag_mark}  {fragment!r}")

        if review_shown:
            print(f"\n--- REVIEW: 1 trafienie, >=1/12 (8%) ({len(review_shown)} pozycji) ---")
            print(f"{'pct':>5}  {'post_id':>8}  {'ref':<18}  {'v/g':<10}  fragment")
            print('-' * 110)
            for pct, pid, ref, votes, total_g, inner in review_shown:
                fragment = re.sub(r'\s+', ' ', _strip_bbcode_tags(inner)).strip()[:200]
                print(f"{pct*100:>4.0f}%  {pid:>8}  [{ref:<16}]  {votes}/{total_g:<8}  {fragment!r}")

    # Zbierz review_items z dry_log jeśli dry_run
    if dry_run:
        review_items = [(pid, ref, v, g, p, inn)
                        for p, pid, ref, v, g, inn, k in dry_log if k == 'review']

    if review_items and review_path:
        # Zapisz do pliku review z placeholderami do edycji (zawsze, także przy dry-run)
        with open(review_path, 'w', encoding='utf-8') as rf:
            rf.write("# Bible review: 1 trafienie n-gramu, pokrycie >=1/12\n")
            rf.write("# Dla każdej pozycji zmień SKIP na BIBLE jeśli cytat jest biblijny.\n")
            rf.write("# Nie zmieniaj linii zaczynających się od #.\n\n")
            for pid, ref, votes, total_g, pct, inner in sorted(review_items, key=lambda x: x[4], reverse=True):
                fragment = re.sub(r'\s+', ' ', _strip_bbcode_tags(inner)).strip()[:300]
                rf.write(f"# POST {pid}  [{ref}]  {votes}/{total_g}  ({pct*100:.0f}%)\n")
                rf.write(f"# {fragment}\n")
                rf.write(f"SKIP  POST={pid}  REF={ref}\n")
                rf.write("\n")
        print(f"  Zapisano {len(review_items)} pozycji do przeglądu: {review_path}")

    auto_count = len(set(x[1] for x in dry_log if x[6] == 'auto')) if dry_run else len(updates)
    print(f"\n  Wykryto cytatów biblijnych: {bible_total:,} w {auto_count:,} postach")
    if review_items:
        print(f"  Do przeglądu (review): {len(review_items)}")

    if not dry_run and updates:
        conn.executemany(
            "UPDATE posts SET content_quotes=?, quote_status=?, nested_status=? WHERE post_id=?",
            updates,
        )
        conn.commit()

    return bible_total


# ---------------------------------------------------------------------------
# Pass: analyze-depth – maksymalne zagnieżdżenie tagów dla quote_status=1
# ---------------------------------------------------------------------------

_ANYDEPTH_OPEN_RE  = re.compile(r'\[(quote|fquote|Bible)(?:[^\]]*)\]', re.IGNORECASE)
_ANYDEPTH_CLOSE_RE = re.compile(r'\[/(quote|fquote|Bible)\]', re.IGNORECASE)


def run_analyze_depth(conn, sample_count=5):
    """Dla postów z quote_status=1 zlicza maksymalne zagnieżdżenie tagów.

    Reguła głębokości:
      0 = brak tagów
      1 = pojedynczy [quote]/[fquote]/[Bible]
      2 = [quote] w [quote], [Bible] w [quote] itp.
      itd.

    Raporty błędów gdy typ zamykającego != typ ostatniego otwierającego.
    Wypisuje rozkład głębokości i kilka przykładów o max głębokości.
    """
    rows = conn.execute(
        "SELECT post_id, COALESCE(content_quotes, content) FROM posts"
        " WHERE quote_status = 1"
    ).fetchall()

    depth_counts = {}   # max_depth -> count
    max_depth_posts = []  # (post_id, max_depth, content)
    errors = 0
    global_max = 0

    for post_id, content in rows:
        if not content:
            depth_counts[0] = depth_counts.get(0, 0) + 1
            continue

        # Zbierz wszystkie zdarzenia w kolejności
        events = []
        for m in _ANYDEPTH_OPEN_RE.finditer(content):
            events.append((m.start(), 'open', m.group(1).lower()))
        for m in _ANYDEPTH_CLOSE_RE.finditer(content):
            events.append((m.start(), 'close', m.group(1).lower()))
        events.sort(key=lambda x: x[0])

        stack = []
        depth = 0
        max_d = 0
        post_errors = 0

        for _, kind, tag_type in events:
            if kind == 'open':
                stack.append(tag_type)
                depth += 1
                if depth > max_d:
                    max_d = depth
            else:
                if stack:
                    expected = stack[-1]
                    if expected != tag_type:
                        post_errors += 1
                    stack.pop()
                    depth -= 1
                else:
                    post_errors += 1

        if post_errors:
            errors += 1

        depth_counts[max_d] = depth_counts.get(max_d, 0) + 1

        if max_d > global_max:
            global_max = max_d
            max_depth_posts = [(post_id, max_d, content)]
        elif max_d == global_max and max_d > 0:
            max_depth_posts.append((post_id, max_d, content))

    # Wyniki
    print(f"\nAnaliza głębokości tagów (quote_status=1):")
    print(f"  Postów z błędami typów tagów: {errors:,}")
    print(f"\n  Rozkład max zagnieżdżenia:")
    for d in sorted(depth_counts):
        label = {0: 'brak tagów', 1: 'płaskie', 2: 'quote w quote'}.get(d, f'poziom {d}')
        print(f"    głębokość {d} ({label}): {depth_counts[d]:,} postów")

    print(f"\n  Maksymalne zagnieżdżenie: {global_max}")
    print(f"\n  Przykłady (max {sample_count}) z głębokością {global_max}:")
    for post_id, max_d, content in max_depth_posts[:sample_count]:
        # Pokaż fragment okolicy najgłębszego zagnieżdżenia
        snippet = re.sub(r'\s+', ' ', content).strip()[:300]
        print(f"\n  POST={post_id}")
        print(f"    {snippet!r}")

    return global_max


# ---------------------------------------------------------------------------
# Pass: mark-broken – oznacz posty z niezbalansowanymi [quote]/[/quote]
# ---------------------------------------------------------------------------

_QUOTE_OPEN_ONLY_RE  = re.compile(r'\[quote(?:[^\]]*)\]', re.IGNORECASE)
_QUOTE_CLOSE_ONLY_RE = re.compile(r'\[/quote\]', re.IGNORECASE)


def run_mark_broken(conn, dry_run=False):
    """Sprawdza pole content (oryginalne) czy liczba [quote...] == liczba [/quote].
    Jeśli nie → content_quotes=NULL, quote_status=4, nested_status=0.
    """
    rows = conn.execute(
        "SELECT post_id, content FROM posts"
        " WHERE content LIKE '%[quote%'"
    ).fetchall()

    updates = []
    broken = 0

    for post_id, content in rows:
        if not content:
            continue
        n_open  = len(_QUOTE_OPEN_ONLY_RE.findall(content))
        n_close = len(_QUOTE_CLOSE_ONLY_RE.findall(content))
        if n_open == n_close:
            continue

        broken += 1
        if dry_run:
            print(f"  POST={post_id}  open={n_open}  close={n_close}")
        else:
            updates.append((post_id,))

    print(f"\n  Niezbalansowanych postów: {broken:,}")

    if not dry_run and updates:
        conn.executemany(
            "UPDATE posts SET content_quotes=NULL, quote_status=4, nested_status=0"
            " WHERE post_id=?",
            updates,
        )
        conn.commit()

    return broken


# ---------------------------------------------------------------------------
# Pass: fix-status – przelicz quote_status i nested_status z aktualnej treści
# ---------------------------------------------------------------------------

def run_fix_status(conn, dry_run=False, need_repair_only=False):
    """Dla postów z quote_status IN (2,3):
    1. Zamienia pozostałe nierozwiązane [quote] → [fquote] (jak to-fquote).
    2. Przelicza quote_status i nested_status z aktualnej treści.
    """
    extra = " AND need_repair_quotes=1" if need_repair_only else ""
    rows = conn.execute(
        "SELECT post_id, COALESCE(content_quotes, content) FROM posts"
        f" WHERE quote_status IN (2, 3){extra}"
    ).fetchall()

    updates = []
    fixed = 0
    converted = 0

    for post_id, content in rows:
        if not content:
            updates.append((content, 0, 1, post_id))
            fixed += 1
            continue

        # Krok 1: zamień pozostałe nierozwiązane [quote] → [fquote]
        # Przetwarzaj tylko liście per runda (re-parse eliminuje problem pozycji)
        new_content = content
        while True:
            all_blocks = parse_quotes(new_content)
            unresolved = [
                b for b in all_blocks
                if new_content[b.start:b.start + 6].lower() == '[quote'
                and not _ANY_POST_ID_RE.search(new_content[b.start:b.tag_end])
                and not _BIBLE_OPEN_RE.match(new_content[b.start:b.tag_end])
            ]
            if not unresolved:
                break
            leaves = [b for b in unresolved if not any(
                id(o) != id(b) and o.start >= b.inner_start and o.end <= b.inner_end
                for o in unresolved
            )]
            if not leaves:
                break
            for b in sorted(leaves, key=lambda x: x.start, reverse=True):
                old_open = new_content[b.start:b.tag_end]
                author_m = re.search(r'="([^"]*)"', old_open)
                new_open = '[fquote="%s"]' % author_m.group(1) if author_m else '[fquote]'
                new_content = (new_content[:b.start] + new_open
                               + new_content[b.tag_end:b.inner_end] + '[/fquote]'
                               + new_content[b.end:])
                converted += 1

        # Fallback: osierocone [quote] bez pary (niezamknięte)
        orphan_open = len(re.findall(r'\[quote\]', new_content, re.IGNORECASE))
        new_content = re.sub(r'\[quote\]',  '[fquote]',  new_content, flags=re.IGNORECASE)
        new_content = re.sub(r'\[/quote\]', '[/fquote]', new_content, flags=re.IGNORECASE)
        converted += orphan_open

        # Krok 2: przelicz status
        n_unresolved = len(_UNRESOLVED_OPEN_RE.findall(new_content))
        n_found      = len(_BIBLE_FOUND_RE.findall(new_content))
        n_not_found  = len(re.findall(r'post_id=not_found', new_content, re.IGNORECASE))
        n_any_found  = n_found + n_not_found
        if n_unresolved == 0:
            new_qs = 1 if n_any_found > 0 else 0
        elif n_any_found == 0:
            new_qs = 2
        else:
            new_qs = 3
        new_ns = 2 if n_unresolved > 0 else 1

        if dry_run:
            if unresolved:
                print(f"  POST={post_id}  +{len(unresolved)} quote→fquote  status→{new_qs}")
        else:
            updates.append((new_content, new_qs, new_ns, post_id))
        fixed += 1

    print(f"\n  Postów: {fixed:,}  w tym zamieniono quote→fquote: {converted:,}")

    if not dry_run and updates:
        conn.executemany(
            "UPDATE posts SET content_quotes=?, quote_status=?, nested_status=? WHERE post_id=?",
            updates,
        )
        conn.commit()

    return fixed


# ---------------------------------------------------------------------------
# Pass: to-fquote – zamień pozostałe nierozwiązane [quote] na [fquote]
# ---------------------------------------------------------------------------

def run_to_fquote(conn, dry_run=False, need_repair_only=False):
    """Zamienia wszystkie nierozwiązane [quote...] (status 2/3) na [fquote...].

    Dla każdego bloku bez post_id:
      - [quote="X"] → [fquote="X"],  [/quote] → [/fquote]
      - [quote]     → [fquote],      [/quote] → [/fquote]
    Ustawia quote_status=1 i nested_status=1.
    """
    extra = " AND need_repair_quotes=1" if need_repair_only else ""
    rows = conn.execute(
        "SELECT post_id, COALESCE(content_quotes, content) FROM posts"
        f" WHERE quote_status IN (2, 3)"
        f"   AND (content_quotes IS NOT NULL OR content LIKE '%[quote%'){extra}"
    ).fetchall()

    updates = []
    converted_total = 0
    posts_changed = 0

    for post_id, content in rows:
        if not content:
            continue

        # Zamieniaj tylko bloki liściowe (bez zagnieżdżonych nierozwiązanych),
        # re-parsuj po każdej rundzie — bezpieczne pozycje bez przesunięć
        new_content = content
        post_converted = 0
        while True:
            all_blocks = parse_quotes(new_content)
            unresolved = [
                b for b in all_blocks
                if new_content[b.start:b.start + 6].lower() == '[quote'
                and not _ANY_POST_ID_RE.search(new_content[b.start:b.tag_end])
                and not _BIBLE_OPEN_RE.match(new_content[b.start:b.tag_end])
            ]
            if not unresolved:
                break

            # Tylko liście: żaden inny nierozwiązany nie jest w środku
            leaves = [b for b in unresolved if not any(
                id(o) != id(b) and o.start >= b.inner_start and o.end <= b.inner_end
                for o in unresolved
            )]
            if not leaves:
                break

            for b in sorted(leaves, key=lambda x: x.start, reverse=True):
                old_open = new_content[b.start:b.tag_end]
                author_m = re.search(r'="([^"]*)"', old_open)
                new_open  = '[fquote="%s"]' % author_m.group(1) if author_m else '[fquote]'
                new_content = (new_content[:b.start]
                               + new_open
                               + new_content[b.tag_end:b.inner_end]
                               + '[/fquote]'
                               + new_content[b.end:])
                post_converted += 1

        if not post_converted:
            continue

        converted_total += post_converted
        posts_changed += 1

        if dry_run:
            print(f"  POST={post_id}  {len(unresolved)} quote→fquote")
            continue

        updates.append((new_content, 1, 1, post_id))

    print(f"\n  Zamieniono: {converted_total:,} tagów w {posts_changed:,} postach")

    if not dry_run and updates:
        conn.executemany(
            "UPDATE posts SET content_quotes=?, quote_status=?, nested_status=? WHERE post_id=?",
            updates,
        )
        conn.commit()

    return converted_total


# ---------------------------------------------------------------------------
# Pass: mark-not-found – oznacz cytaty znanych użytkowników jako not_found
# ---------------------------------------------------------------------------

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


def extract_nonquote_text(content):
    """Return content with all quote/fquote/Bible blocks removed."""
    events = []
    for m in _QUOTE_OPEN_RE.finditer(content):
        events.append((m.start(), 'open', m.end()))
    for m in _QUOTE_CLOSE_RE.finditer(content):
        events.append((m.start(), 'close', m.end()))
    events.sort(key=lambda x: x[0])

    if not events:
        return content

    exclude_ranges = []
    stack = []
    for pos, kind, end in events:
        if kind == 'open':
            stack.append(pos)
        elif kind == 'close' and stack:
            start = stack.pop()
            if not stack:
                exclude_ranges.append((start, end))

    if not exclude_ranges:
        return content

    parts = []
    prev = 0
    for start, end in exclude_ranges:
        parts.append(content[prev:start])
        prev = end
    parts.append(content[prev:])
    return ''.join(parts)


def find_deeper_quote_source(content, quote_norm, current_source_post_id):
    """If quote text matches an enriched quote inside content, return its source post_id.

    Chooses the nearest earlier source (largest post_id < current_source_post_id).
    """
    candidates = []
    for block in parse_quotes(content):
        raw_open = content[block.start:block.tag_end]
        m = _ANY_ENRICHED_QUOTE_RE.match(raw_open)
        if not m:
            continue
        inner_source_post_id = int(m.group('post_id'))
        if inner_source_post_id >= current_source_post_id:
            continue
        inner_norm = normalize_text(extract_quote_text(content, block))
        if match_quote_in_post(quote_norm, inner_norm):
            candidates.append(inner_source_post_id)

    if not candidates:
        return None
    return max(candidates)


def resolve_quoted_source_post_id(post_content_map, source_post_id, quote_norm, max_hops=20):
    """Follow existing post_id links backwards until text exists outside quotes."""
    visited = set()
    current = source_post_id
    hops = 0

    while current and current not in visited and hops < max_hops:
        visited.add(current)
        source_content = post_content_map.get(current)
        if not source_content:
            break

        outside_norm = normalize_text(extract_nonquote_text(source_content))
        if match_quote_in_post(quote_norm, outside_norm):
            break

        deeper = find_deeper_quote_source(source_content, quote_norm, current)
        if deeper is None:
            break

        current = deeper
        hops += 1

    return current


def run_mark_not_found(conn, known_users, dry_run=False, need_repair_only=False):
    """Oznacza nierozwiązane cytaty jako post_id=not_found w dwóch przypadkach:
    1. Autor jest w sfinia_users_real.db (known user, post nie znaleziony).
    2. Cytat zawiera zagnieżdżone [quote]/[fquote]/[Bible] w środku
       (cytat wielopoziomowy / zagraniczny).
    """
    extra = " AND need_repair_quotes=1" if need_repair_only else ""
    rows = conn.execute(
        "SELECT post_id, COALESCE(content_quotes, content) FROM posts"
        f" WHERE quote_status IN (2, 3)"
        f"   AND (content_quotes IS NOT NULL OR content LIKE '%[quote%'){extra}"
    ).fetchall()

    updates = []
    marked_known = 0
    marked_nested = 0
    posts_changed = 0

    for post_id, content in rows:
        if not content:
            continue

        # --- Reguła 1: znany użytkownik (regex, lewa→prawa z offsetem) ---
        new_content = content
        offset = 0
        changed = 0

        for m in _NAMED_UNRESOLVED_RE.finditer(content):
            author = m.group(1)
            if author.lower() not in known_users:
                continue
            new_tag = '[quote="%s" post_id=not_found]' % author
            pos = m.start() + offset
            new_content = new_content[:pos] + new_tag + new_content[pos + len(m.group(0)):]
            offset += len(new_tag) - len(m.group(0))
            changed += 1
            marked_known += 1

        # --- Reguła 2: nierozwiązany cytat z zagnieżdżoną zawartością ---
        # Parsuj bloki w (już częściowo zaktualizowanym) new_content
        all_blocks = parse_quotes(new_content)
        unresolved_blocks = [
            b for b in all_blocks
            if not _ANY_POST_ID_RE.search(new_content[b.start:b.tag_end])
            and not _BIBLE_OPEN_RE.match(new_content[b.start:b.tag_end])
        ]

        # Zastępuj od prawej do lewej (bezpieczne dla pozycji)
        for b in sorted(unresolved_blocks, key=lambda x: x.start, reverse=True):
            inner = new_content[b.inner_start:b.inner_end]
            if not _ANY_QUOTE_OPEN_RE.search(inner):
                continue
            old_tag = new_content[b.start:b.tag_end]
            author_m = re.search(r'="([^"]*)"', old_tag)
            author = author_m.group(1) if author_m else None
            if author:
                new_tag = '[quote="%s" post_id=not_found]' % author
            else:
                new_tag = '[quote post_id=not_found]'
            new_content = new_content[:b.start] + new_tag + new_content[b.tag_end:]
            changed += 1
            marked_nested += 1

        if not changed:
            continue

        posts_changed += 1

        n_unresolved = len(_UNRESOLVED_OPEN_RE.findall(new_content))
        new_quote_status = 1 if n_unresolved == 0 else 3
        new_nested_status = 2 if _UNRESOLVED_OPEN_RE.search(new_content) else 1

        if dry_run:
            print(f"  POST={post_id}  +{changed} not_found  status→{new_quote_status}")
        else:
            updates.append((new_content, new_quote_status, new_nested_status, post_id))

    print(f"\n  Oznaczono post_id=not_found łącznie: {marked_known + marked_nested:,} w {posts_changed:,} postach")
    print(f"    znany użytkownik (post nie znaleziony): {marked_known:,}")
    print(f"    cytat z zagnieżdżoną zawartością:       {marked_nested:,}")

    if not dry_run and updates:
        conn.executemany(
            "UPDATE posts SET content_quotes=?, quote_status=?, nested_status=? WHERE post_id=?",
            updates,
        )
        conn.commit()

    return marked_known + marked_nested


# ---------------------------------------------------------------------------
# Pass: fix-quote-authors – popraw autora w [quote ... post_id=N]
# ---------------------------------------------------------------------------

def run_fix_quote_authors(conn, dry_run=False, need_repair_only=False):
    """Napraw autora w tagach [quote ... post_id=N] na autora posta źródłowego.

    Reguły:
      - tylko tagi [quote], bez [fquote] i [Bible]
      - tylko post_id=<liczba>, nie post_id=not_found
      - jeśli autor jest błędny albo pusty, zastąp/dopisz autora posta źródłowego
      - zachowaj pozostałe atrybuty tagu (np. time=...)
    """
    post_author = {
        int(post_id): (author_name or '').strip()
        for post_id, author_name in conn.execute(
            "SELECT post_id, author_name FROM posts"
        )
    }

    extra = " AND need_repair_quotes=1" if need_repair_only else ""
    rows = conn.execute(
        "SELECT post_id, content_quotes FROM posts"
        f" WHERE content_quotes IS NOT NULL"
        f"   AND content_quotes LIKE '%post_id=%'{extra}"
    ).fetchall()

    updates = []
    changed_posts = 0
    changed_tags = 0

    for post_id, content in rows:
        if not content:
            continue

        replacements = []
        for m in _QUOTE_WITH_POST_ID_RE.finditer(content):
            src_post_id = int(m.group('post_id'))
            resolved_author = post_author.get(src_post_id, '')
            if not resolved_author:
                continue

            current_author = (m.group('author') or '').strip()
            if current_author == resolved_author:
                continue

            new_tag = '[quote="%s"%s%s]' % (
                resolved_author,
                m.group('mid'),
                m.group('tail') or '',
            )
            replacements.append((m.start(), m.end(), new_tag, current_author, resolved_author, src_post_id))

        if not replacements:
            continue

        changed_posts += 1
        changed_tags += len(replacements)

        if dry_run:
            print(f"  POST={post_id}  quote-author fixes={len(replacements)}")
            for _, _, _, old_author, new_author, src_post_id in replacements[:5]:
                before = old_author if old_author else '<brak>'
                print(f"    post_id={src_post_id}: {before!r} -> {new_author!r}")
            continue

        new_content = content
        for start, end, new_tag, _, _, _ in reversed(replacements):
            new_content = new_content[:start] + new_tag + new_content[end:]
        updates.append((new_content, post_id))

    print(f"\n  Naprawiono autorów cytatu: {changed_tags:,} tagów w {changed_posts:,} postach")

    if not dry_run and updates:
        conn.executemany(
            "UPDATE posts SET content_quotes=? WHERE post_id=?",
            updates,
        )
        conn.commit()

    return changed_tags


def run_fix_quote_post_ids(conn, dry_run=False, need_repair_only=False):
    """Cofnij post_id, jeśli wskazany post zawiera dany tekst wyłącznie w cytacie.

    Działa wyłącznie po istniejących post_id:
      - bierze quote/fquote z liczbowym post_id z content_quotes
      - sprawdza wskazany post źródłowy
      - jeśli tekst cytatu nie występuje tam poza cytatami, szuka w tym poście
        pasującego zagnieżdżonego cytatu z własnym post_id i cofa się dalej
      - kończy, gdy tekst przestaje być "samym cytatem" albo łańcuch się urywa
    """
    post_content_map = {
        int(post_id): (content or '')
        for post_id, content in conn.execute(
            "SELECT post_id, COALESCE(content_quotes, content) FROM posts"
        )
    }

    extra = " AND need_repair_quotes=1" if need_repair_only else ""
    rows = conn.execute(
        "SELECT post_id, content_quotes FROM posts"
        f" WHERE content_quotes IS NOT NULL"
        f"   AND content_quotes LIKE '%post_id=%'{extra}"
    ).fetchall()

    updates = []
    quote_updates = []
    changed_posts = 0
    changed_tags = 0

    for post_id, content in rows:
        if not content:
            continue

        replacements = []
        blocks = parse_quotes(content)
        for quote_index, block in enumerate(blocks):
            raw_open = content[block.start:block.tag_end]
            m = _ANY_ENRICHED_QUOTE_RE.match(raw_open)
            if not m:
                continue

            old_source_post_id = int(m.group('post_id'))
            quote_norm = normalize_text(extract_quote_text(content, block))
            if not quote_norm:
                continue

            new_source_post_id = resolve_quoted_source_post_id(
                post_content_map, old_source_post_id, quote_norm
            )
            if new_source_post_id == old_source_post_id:
                continue

            new_tag = '[%s%s post_id=%d%s]' % (
                m.group('qtype'),
                '="%s"' % m.group('author') if m.group('author') is not None else '',
                new_source_post_id,
                m.group('tail') or '',
            )
            replacements.append((
                block.start, block.tag_end, new_tag,
                quote_index, old_source_post_id, new_source_post_id, quote_norm[:100],
            ))

        if not replacements:
            continue

        changed_posts += 1
        changed_tags += len(replacements)

        if dry_run:
            print(f"  POST={post_id}  post_id fixes={len(replacements)}")
            for _, _, _, _, old_src, new_src, preview in replacements[:5]:
                print(f"    {old_src} -> {new_src}  |  {preview!r}")
            continue

        new_content = content
        for start, end, new_tag, quote_index, old_src, new_src, _ in reversed(replacements):
            new_content = new_content[:start] + new_tag + new_content[end:]
            quote_updates.append((new_src, post_id, quote_index, old_src))
        updates.append((new_content, post_id))

    print(f"\n  Skorygowano post_id cytatu: {changed_tags:,} tagów w {changed_posts:,} postach")

    if not dry_run and updates:
        conn.executemany(
            "UPDATE posts SET content_quotes=? WHERE post_id=?",
            updates,
        )
        if quote_updates:
            conn.executemany(
                "UPDATE quotes SET source_post_id=? WHERE post_id=? AND quote_index=? AND source_post_id=?",
                quote_updates,
            )
        conn.commit()

    return changed_tags


# ---------------------------------------------------------------------------
# Pass: bible-review-apply – zastosuj decyzje z pliku review (BIBLE/SKIP)
# ---------------------------------------------------------------------------

def run_bible_review_apply(conn, review_path, dry_run=False):
    """Czyta plik review i taguje posty oznaczone BIBLE.

    Format pliku:
        BIBLE  POST=<id>  REF=<ref>
        SKIP   POST=<id>  REF=<ref>
    """
    if _BIBLE_NGRAM_INDEX is None:
        print("BŁĄD: Bible index nie załadowany. Użyj --bible-index.")
        return 0

    # Wczytaj decyzje
    bible_decisions = {}   # post_id -> ref
    line_re = re.compile(r'^(BIBLE|SKIP)\s+POST=(\d+)\s+REF=(.+)$', re.IGNORECASE)
    with open(review_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            m = line_re.match(line)
            if not m:
                continue
            action, post_id_s, ref = m.group(1).upper(), int(m.group(2)), m.group(3).strip()
            if action == 'BIBLE':
                bible_decisions[post_id_s] = ref

    if not bible_decisions:
        print("Brak wpisów BIBLE w pliku review.")
        return 0

    print(f"  Wpisów BIBLE do zastosowania: {len(bible_decisions)}")

    # Pobierz posty
    placeholders = ','.join('?' * len(bible_decisions))
    rows = conn.execute(
        f"SELECT post_id, COALESCE(content_quotes, content) FROM posts"
        f" WHERE post_id IN ({placeholders})",
        list(bible_decisions.keys())
    ).fetchall()

    updates = []
    tagged = 0

    for post_id, content in rows:
        if not content:
            continue
        target_ref = bible_decisions[post_id]

        all_blocks = parse_quotes(content)
        unresolved = [b for b in all_blocks
                      if not _ENRICHED_TAG_RE.search(content[b.start:b.tag_end])
                      and not _BIBLE_OPEN_RE.match(content[b.start:b.tag_end])]

        def is_leaf(b):
            for other in unresolved:
                if id(other) != id(b) and other.start >= b.inner_start and other.end <= b.inner_end:
                    return False
            return True

        leaf_blocks = [b for b in unresolved if is_leaf(b)]

        # Znajdź blok który głosował na target_ref
        best_block = None
        best_votes = 0
        for b in leaf_blocks:
            inner = content[b.inner_start:b.inner_end]
            if _ANY_QUOTE_OPEN_RE.search(inner):
                continue
            ref, votes, _ = _bible_votes(inner)
            if ref == target_ref and votes > best_votes:
                best_block = b
                best_votes = votes

        if best_block is None:
            print(f"  UWAGA: nie znaleziono bloku dla POST={post_id} REF={target_ref}")
            continue

        b = best_block
        new_tag   = '[Bible=%s]' % target_ref
        new_close = '[/Bible]'
        new_content = (content[:b.start] + new_tag
                       + content[b.tag_end:b.inner_end] + new_close
                       + content[b.end:])
        tagged += 1

        if dry_run:
            inner = content[b.inner_start:b.inner_end]
            fragment = re.sub(r'\s+', ' ', _strip_bbcode_tags(inner)).strip()[:120]
            print(f"  DRY  POST={post_id}  [{target_ref}]  {fragment!r}")
            continue

        n_unresolved = len(_UNRESOLVED_OPEN_RE.findall(new_content))
        n_found      = len(_BIBLE_FOUND_RE.findall(new_content))
        if n_unresolved == 0 and n_found > 0:
            new_qs = 1
        elif n_found == 0:
            new_qs = 2
        else:
            new_qs = 3
        new_ns = 2 if _UNRESOLVED_OPEN_RE.search(new_content) else 1
        updates.append((new_content, new_qs, new_ns, post_id))

    if not dry_run and updates:
        conn.executemany(
            "UPDATE posts SET content_quotes=?, quote_status=?, nested_status=? WHERE post_id=?",
            updates,
        )
        conn.commit()

    print(f"\n  Otagowano: {tagged} postów")
    return tagged


# ---------------------------------------------------------------------------
# Pass: bible-filter – cofnij [Bible=] które nie spełniają kryterium pokrycia
# ---------------------------------------------------------------------------

def run_bible_filter(conn, dry_run=False, coverage_min=0.0):
    """Sprawdź każdy [Bible=ref] w content_quotes tym samym kryterium pokrycia co bible pass.

    Jeśli pokrycie < _BIBLE_COVERAGE I >= coverage_min → cofa na [quote]...[/quote].
    coverage_min (domyślnie 0) pozwala zobaczyć tylko zakres, np. 0.28–0.40.
    Zwraca liczbę cofniętych tagów.
    """
    if _BIBLE_NGRAM_INDEX is None:
        print("BŁĄD: Bible index nie załadowany. Użyj --bible-index.")
        return 0

    _BIBLE_TAG_RE = re.compile(
        r'\[Bible=(?P<ref>[^\]]+)\](?P<inner>.*?)\[/Bible\]',
        re.IGNORECASE | re.DOTALL,
    )

    rows = conn.execute(
        "SELECT post_id, content_quotes FROM posts"
        " WHERE content_quotes IS NOT NULL AND content_quotes LIKE '%[Bible=%'"
    ).fetchall()

    updates = []
    reverted_total = 0

    for post_id, content in rows:
        if not content:
            continue

        new_content = content
        offset = 0
        reverted = 0

        # Szukaj od lewej, śledź offset
        for m in _BIBLE_TAG_RE.finditer(content):
            ref   = m.group('ref')
            inner = m.group('inner')

            # Sprawdź pokrycie tym samym algorytmem co lookup_bible
            text = _strip_bbcode_tags(inner)
            ws = norm_for_bible(text).split()
            n = _BIBLE_NGRAM_SIZE

            is_false_positive = False

            if len(ws) < n:
                # Krótki tekst - ufamy że był poprawny
                pass
            else:
                ref_votes = {}
                for i in range(len(ws) - n + 1):
                    key = ' '.join(ws[i:i + n])
                    r = _BIBLE_NGRAM_INDEX.get(key)
                    if r:
                        ref_votes[r] = ref_votes.get(r, 0) + 1

                total_grams = len(ws) - n + 1
                if ref in ref_votes:
                    best_count   = ref_votes[ref]
                    is_nt        = ',' in ref
                    min_coverage = max(1, int(total_grams * _BIBLE_COVERAGE))
                    base_min     = 1 if is_nt else 2
                    min_votes    = max(base_min, min_coverage)
                    if best_count < min_votes:
                        is_false_positive = True
                else:
                    best_count = 0
                    total_grams = max(total_grams, 1)
                    is_false_positive = True

            if not is_false_positive:
                continue

            pct_val = best_count / max(1, total_grams)
            if pct_val < coverage_min:
                continue   # poniżej dolnego progu – pomijamy
            print(f"  COFA  post {post_id:>7}  [{ref}]  {best_count}/{total_grams} n-gramów ({pct_val*100:.0f}%)  |  {inner[:60].strip()!r}")

            # Cofnij: [Bible=ref]...[/Bible] → [quote]...[/quote]
            new_tag   = '[quote]'
            new_close = '[/quote]'
            adj_start = m.start() + offset
            adj_end   = m.end()   + offset
            adj_inner_start = adj_start + len(m.group(0)) - len(inner) - len('[/Bible]')

            new_content = (
                new_content[:adj_start]
                + new_tag
                + inner
                + new_close
                + new_content[adj_end:]
            )
            offset += (len(new_tag) + len(new_close)) - len(m.group(0))
            reverted += 1
            reverted_total += 1

        if reverted:
            # Przelicz quote_status
            n_unresolved = len(_UNRESOLVED_OPEN_RE.findall(new_content))
            n_found      = len(_BIBLE_FOUND_RE.findall(new_content))
            if n_unresolved == 0 and n_found > 0:
                new_status = 1
            elif n_found == 0:
                new_status = 2
            else:
                new_status = 3
            new_nested = 2 if _UNRESOLVED_OPEN_RE.search(new_content) else 1
            updates.append((new_content, new_status, new_nested, post_id))

    print(f"  Cofnięto false positives: {reverted_total:,} tagów w {len(updates):,} postach")

    if not dry_run and updates:
        conn.executemany(
            "UPDATE posts SET content_quotes=?, quote_status=?, nested_status=? WHERE post_id=?",
            updates,
        )
        conn.commit()

    return reverted_total


def enrich_post(content, post_id, topic_id, post_order,
                known_users, cache, lookback=20,
                pass_type='known-user', gcache=None,
                topic_cache_all=None, global_cache_all=None,
                ngram_index=None, ngram_post_author=None):
    """Process a single post's content.

    Returns (new_content_quotes, quote_status, list_of_quote_records).
    new_content_quotes: enriched content, or None if no quotes at all.
    quote_status: 0=no quotes, 1=all found, 2=none found, 3=mixed.
    """
    is_anon_pass = pass_type in ('anon-topic', 'anon-global', 'ngram')

    quotes = parse_quotes(content)
    # For anon passes: all quotes (named and anonymous)
    # For known-user passes: only named quotes
    if is_anon_pass:
        all_quotes = quotes
    else:
        all_quotes = [q for q in quotes if q.author]

    if not all_quotes:
        if _QUOTE_OPEN_RE.search(content):
            return content, 2, []
        return None, 0, []

    # For known-user passes: skip if no known author
    if not is_anon_pass:
        has_known = any(q.author.lower() in known_users for q in all_quotes)
        if not has_known:
            return content, 2, [{
                'post_id': post_id,
                'quoted_user': q.author,
                'quoted_user_resolved': q.author,
                'source_post_id': None,
                'quote_text_preview': normalize_text(extract_quote_text(content, q))[:100],
                'quote_index': i,
                'found': 0,
            } for i, q in enumerate(all_quotes)]

    new_content = content
    offset = 0
    quote_records = []
    found_count = 0
    total_count = 0

    for i, q in enumerate(all_quotes):
        # Check if already enriched (has post_id= in opening tag)
        raw_open_tag = content[q.start:q.tag_end]
        already_enriched = bool(_ENRICHED_TAG_RE.search(raw_open_tag))

        if already_enriched:
            # Count as found, no record update needed
            found_count += 1
            total_count += 1
            continue

        total_count += 1
        author_lower = q.author.lower() if q.author else None
        quote_norm = normalize_text(extract_quote_text(content, q))
        preview = quote_norm[:100]
        source_post_id = None
        resolved_author = q.author  # may be updated from matched post

        if pass_type == 'known-user':
            if author_lower and author_lower in known_users:
                candidates = cache_lookup(cache, topic_id, author_lower,
                                          post_order, lookback)
                for cand_pid, cand_norm in candidates:
                    if match_quote_in_post(quote_norm, cand_norm):
                        source_post_id = cand_pid
                        break

        elif pass_type == 'known-user-global':
            if author_lower and author_lower in known_users:
                candidates = global_cache_lookup(gcache, author_lower,
                                                 post_id, lookback)
                for cand_pid, cand_norm in candidates:
                    if match_quote_in_post(quote_norm, cand_norm):
                        source_post_id = cand_pid
                        break

        elif pass_type == 'anon-topic':
            candidates = topic_cache_all_lookup(topic_cache_all, topic_id,
                                                post_order, lookback)
            for cand_pid, cand_auth, cand_norm in candidates:
                if match_quote_in_post(quote_norm, cand_norm):
                    source_post_id = cand_pid
                    resolved_author = cand_auth
                    break

        elif pass_type == 'anon-global':
            candidates = global_cache_all_lookup(global_cache_all, post_id,
                                                 lookback)
            for cand_pid, cand_auth, cand_norm in candidates:
                if match_quote_in_post(quote_norm, cand_norm):
                    source_post_id = cand_pid
                    resolved_author = cand_auth
                    break

        elif pass_type == 'ngram':
            found_pid, found_auth = ngram_lookup(
                ngram_index, ngram_post_author, quote_norm, post_id
            )
            if found_pid is not None:
                source_post_id = found_pid
                resolved_author = found_auth

        found = 1 if source_post_id is not None else 0
        found_count += found

        quote_records.append({
            'post_id': post_id,
            'quoted_user': q.author,
            'quoted_user_resolved': resolved_author,
            'source_post_id': source_post_id,
            'quote_text_preview': preview,
            'quote_index': i,
            'found': found,
        })

        if source_post_id is not None:
            if q.author:
                new_tag = '[quote="%s" post_id=%d]' % (q.author, source_post_id)
            else:
                new_tag = '[quote post_id=%d]' % source_post_id
            new_content = (
                new_content[:q.start + offset]
                + new_tag
                + new_content[q.tag_end + offset:]
            )
            offset += len(new_tag) - len(raw_open_tag)

    if total_count == 0:
        quote_status = 0
    elif found_count == total_count:
        quote_status = 1
    elif found_count == 0:
        quote_status = 2
    else:
        quote_status = 3

    return new_content, quote_status, quote_records


def parse_args():
    p = argparse.ArgumentParser(description='Enrich BBCode quotes with post_id')
    p.add_argument('--pass', dest='pass_type', choices=PASS_TYPES,
                   default='known-user',
                   help='Rodzaj przetwarzania (default: known-user)')
    p.add_argument('--lookback', type=int, default=20,
                   help='Ile postów wstecz tego samego autora przeszukiwać (default: 20)')
    p.add_argument('--limit', type=int, default=None,
                   help='Ogranicz liczbę przetwarzanych postów')
    p.add_argument('--dry-run', action='store_true',
                   help='Nie zapisuj zmian w bazie')
    p.add_argument('--reset', action='store_true',
                   help='Wyczyść content_quotes, quote_status i tabelę quotes')
    p.add_argument('--bible-index', default=None, metavar='PATH',
                   help='Ścieżka do bible_index.pkl (wymagana dla --pass bible/bible-filter)')
    p.add_argument('--bible-coverage', type=float, default=0.40, metavar='FRAC',
                   help='Max. pokrycie n-gramów przy bible-filter (0–1, domyślnie 0.40)')
    p.add_argument('--bible-coverage-min', type=float, default=0.0, metavar='FRAC',
                   help='Min. pokrycie przy bible-filter – pokaż/usuń tylko powyżej tego progu (domyślnie 0)')
    p.add_argument('--bible-review', default=None, metavar='PATH',
                   help='Plik do zapisu cytatów do ręcznego przeglądu (1 głos, >=25%)')
    p.add_argument('--bible-dry-min', type=float, default=0.09, metavar='FRAC',
                   help='Min. pokrycie do wyświetlenia w dry-run (0–1, domyślnie 0.09)')
    p.add_argument('--null-only', action='store_true',
                   help='Przetwarzaj tylko posty z quote_status IS NULL (nowe/niezbadane); '
                        'ustawia quote_status=0 dla postów bez [quote]')
    return p.parse_args()


def main():
    args = parse_args()
    lookback = args.lookback
    dry_run = args.dry_run
    reset = args.reset
    limit = args.limit
    pass_type = args.pass_type
    null_only = args.null_only
    bible_index_path     = args.bible_index
    bible_coverage_min   = args.bible_coverage_min
    bible_review_path    = args.bible_review
    global _BIBLE_COVERAGE, _BIBLE_DRY_MIN
    _BIBLE_COVERAGE = args.bible_coverage
    _BIBLE_DRY_MIN  = args.bible_dry_min

    print(f"=== Enrich Quotes (--pass {pass_type}) ===")
    print(f"  DB:       {DB_PATH}")
    print(f"  Users DB: {USERS_DB_PATH}")
    print(f"  Lookback: {lookback} posts")
    print(f"  Dry run:  {dry_run}")
    if limit:
        print(f"  Limit:    {limit}")
    print()

    # Load known users
    known_users = load_known_users(USERS_DB_PATH)
    print(f"Załadowano {len(known_users)} znanych użytkowników")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    if reset:
        print("Resetuję content_quotes, quote_status i tabelę quotes...")
        conn.execute("UPDATE posts SET content_quotes = NULL, quote_status = 0")
        conn.execute("DROP TABLE IF EXISTS quotes")
        conn.commit()

    create_quotes_table(conn)

    # Pass propagate i bible są obsługiwane osobno
    if pass_type == 'propagate':
        total = run_propagate(conn, dry_run=dry_run, need_repair_only=null_only)
        print(f"\n=== Propagacja zakończona: {total:,} tagów wzbogaconych ===")
        conn.close()
        return

    if pass_type == 'mark-not-found':
        total = run_mark_not_found(conn, known_users, dry_run=dry_run, need_repair_only=null_only)
        print(f"\n=== Mark-not-found zakończony: {total:,} cytatów oznaczonych ===")
        if dry_run:
            print("[DRY RUN] Nie zapisano zmian.")
        conn.close()
        return

    if pass_type == 'to-fquote':
        total = run_to_fquote(conn, dry_run=dry_run, need_repair_only=null_only)
        print(f"\n=== To-fquote zakończony: {total:,} tagów zamienionych ===")
        if dry_run:
            print("[DRY RUN] Nie zapisano zmian.")
        conn.close()
        return

    if pass_type == 'fix-status':
        total = run_fix_status(conn, dry_run=dry_run, need_repair_only=null_only)
        print(f"\n=== Fix-status zakończony: {total:,} postów poprawionych ===")
        if dry_run:
            print("[DRY RUN] Nie zapisano zmian.")
        conn.close()
        return

    if pass_type == 'mark-broken':
        total = run_mark_broken(conn, dry_run=dry_run)
        print(f"\n=== Mark-broken zakończony: {total:,} postów oznaczonych ===")
        if dry_run:
            print("[DRY RUN] Nie zapisano zmian.")
        conn.close()
        return

    if pass_type == 'fix-quote-authors':
        total = run_fix_quote_authors(conn, dry_run=dry_run, need_repair_only=null_only)
        print(f"\n=== Fix-quote-authors zakończony: {total:,} tagów poprawionych ===")
        if dry_run:
            print("[DRY RUN] Nie zapisano zmian.")
        conn.close()
        return

    if pass_type == 'fix-quote-post-ids':
        total = run_fix_quote_post_ids(conn, dry_run=dry_run, need_repair_only=null_only)
        print(f"\n=== Fix-quote-post-ids zakończony: {total:,} tagów poprawionych ===")
        if dry_run:
            print("[DRY RUN] Nie zapisano zmian.")
        conn.close()
        return

    if pass_type == 'analyze-depth':
        run_analyze_depth(conn)
        conn.close()
        return

    if pass_type in ('bible', 'bible-filter', 'bible-review-apply'):
        if not bible_index_path:
            print("BŁĄD: --pass bible wymaga --bible-index PATH")
            conn.close()
            sys.exit(1)
        print(f"Wczytuję indeks biblijny z {bible_index_path}...")
        load_bible_index(bible_index_path)
        print(f"  Załadowano {len(_BIBLE_NGRAM_INDEX):,} n-gramów")
        if pass_type == 'bible':
            total = run_bible(conn, dry_run=dry_run, review_path=bible_review_path)
            print(f"\n=== Bible pass zakończony: {total:,} tagów [Bible=] wstawionych ===")
        elif pass_type == 'bible-filter':
            total = run_bible_filter(conn, dry_run=dry_run,
                                     coverage_min=bible_coverage_min)
            print(f"\n=== Bible-filter zakończony: {total:,} false positives cofniętych ===")
        else:
            if not bible_review_path:
                print("BŁĄD: --pass bible-review-apply wymaga --bible-review PATH")
                conn.close()
                sys.exit(1)
            total = run_bible_review_apply(conn, bible_review_path, dry_run=dry_run)
            print(f"\n=== Bible-review-apply zakończony: {total:,} tagów wstawionych ===")
        if dry_run:
            print("[DRY RUN] Nie zapisano zmian.")
        conn.close()
        return

    # Build in-memory cache(s)
    cache = None
    gcache = None
    tcache_all = None
    gcache_all = None
    ngram_index = None
    ngram_post_author = None

    if pass_type == 'known-user':
        cache = build_author_cache(conn, known_users)
    elif pass_type == 'known-user-global':
        gcache = build_global_cache(conn, known_users)
    elif pass_type == 'anon-topic':
        tcache_all = build_topic_cache_all(conn)
    elif pass_type == 'anon-global':
        gcache_all = build_global_cache_all(conn)
    elif pass_type == 'ngram':
        ngram_index, ngram_post_author = build_ngram_index(conn)

    # --null-only: najpierw oznacz posty bez [quote] jako quote_status=0
    if null_only and pass_type == 'known-user' and not dry_run:
        marked = conn.execute(
            "UPDATE posts SET quote_status = 0, nested_status = 0"
            " WHERE quote_status IS NULL AND content NOT LIKE '%[quote%'"
        ).rowcount
        conn.commit()
        print(f"  Oznaczono {marked:,} postów bez cytatów jako quote_status=0.")

    # Count posts to process
    if reset:
        where = "content LIKE '%[quote%'"
    elif null_only and pass_type == 'known-user':
        where = "content LIKE '%[quote%' AND quote_status IS NULL"
    elif pass_type == 'known-user':
        where = "content LIKE '%[quote%' AND quote_status = 0"
    else:
        where = "quote_status IN (2, 3)"

    total_posts = conn.execute(
        f"SELECT COUNT(*) FROM posts WHERE {where}"
    ).fetchone()[0]
    print(f"Postów do przetworzenia: {total_posts:,}")

    if limit:
        total_posts = min(total_posts, limit)

    # Process posts
    batch_size = 500
    processed = 0
    found_total = 0
    not_found_total = 0
    status_counts = {0: 0, 1: 0, 2: 0, 3: 0}

    # For global pass, read content_quotes (partially enriched); else content
    content_col = "COALESCE(content_quotes, content)" if pass_type == 'known-user-global' else "content"

    cursor = conn.execute(
        f"""SELECT post_id, topic_id, {content_col}, post_order
            FROM posts WHERE {where}
            ORDER BY post_id
            {"LIMIT " + str(limit) if limit else ""}"""
    )

    content_updates = []
    quote_inserts = []

    for row in cursor:
        post_id, topic_id, content, post_order = row

        new_content, quote_status, quote_records = enrich_post(
            content, post_id, topic_id, post_order,
            known_users, cache, lookback,
            pass_type=pass_type, gcache=gcache,
            topic_cache_all=tcache_all, global_cache_all=gcache_all,
            ngram_index=ngram_index, ngram_post_author=ngram_post_author,
        )

        content_updates.append((new_content, quote_status, post_id))
        for qr in quote_records:
            quote_inserts.append((
                qr['post_id'], qr['quoted_user'], qr['quoted_user_resolved'],
                qr['source_post_id'], qr['quote_text_preview'],
                qr['quote_index'], qr['found'],
            ))

        status_counts[quote_status] = status_counts.get(quote_status, 0) + 1
        for qr in quote_records:
            if qr['found']:
                found_total += 1
            else:
                not_found_total += 1

        processed += 1

        # Progress
        if processed % 1000 == 0 or processed == total_posts:
            pct = processed / total_posts * 100 if total_posts else 0
            print(
                f"\r  [{processed:,}/{total_posts:,}] {pct:.1f}%  "
                f"found={found_total:,} not_found={not_found_total:,}",
                end='', flush=True,
            )

        # Batch write
        if len(content_updates) >= batch_size:
            if not dry_run:
                conn.executemany(
                    "UPDATE posts SET content_quotes=?, quote_status=? WHERE post_id=?",
                    content_updates,
                )
                conn.executemany(
                    """INSERT OR REPLACE INTO quotes
                       (post_id, quoted_user, quoted_user_resolved,
                        source_post_id, quote_text_preview, quote_index, found)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    quote_inserts,
                )
                conn.commit()
            content_updates.clear()
            quote_inserts.clear()

    # Final batch
    if content_updates and not dry_run:
        conn.executemany(
            "UPDATE posts SET content_quotes=?, quote_status=? WHERE post_id=?",
            content_updates,
        )
        conn.executemany(
            """INSERT INTO quotes
               (post_id, quoted_user, quoted_user_resolved,
                source_post_id, quote_text_preview, quote_index, found)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            quote_inserts,
        )
        conn.commit()

    print()  # newline after progress
    print()
    print(f"=== Wyniki ===")
    print(f"  Przetworzono postów: {processed:,}")
    print(f"  Cytatów znalezionych:    {found_total:,}")
    print(f"  Cytatów nieznalezionych: {not_found_total:,}")
    print(f"  quote_status=0 (brak cytatów):       {status_counts.get(0, 0):,}")
    print(f"  quote_status=1 (wszystkie znalezione): {status_counts.get(1, 0):,}")
    print(f"  quote_status=2 (żadne nieznalezione):  {status_counts.get(2, 0):,}")
    print(f"  quote_status=3 (mieszane):             {status_counts.get(3, 0):,}")

    if dry_run:
        print("\n[DRY RUN] Nie zapisano zmian w bazie.")
        # Show some examples
        print("\nPrzykłady (pierwsze 3 znalezione):")
        shown = 0
        conn2 = sqlite3.connect(DB_PATH)
        for cu in content_updates[:50]:
            new_content, status, pid = cu
            if status in (1, 3) and new_content:
                orig = conn2.execute(
                    "SELECT content FROM posts WHERE post_id=?", (pid,)
                ).fetchone()[0]
                if orig != new_content:
                    print(f"\n--- post {pid} (status={status}) ---")
                    # Show just the quote tags
                    for m in re.finditer(r'\[quote="[^"]*"(?:\s+post_id=\d+)?\]', new_content):
                        print(f"  {m.group(0)}")
                    shown += 1
                    if shown >= 3:
                        break
        conn2.close()

    conn.close()
    print("\nGotowe.")


if __name__ == '__main__':
    main()
