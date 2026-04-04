"""
Enrich [quote] tags in sfiniabb.db with post_id and Unix timestamps.

For each post containing [quote="Username"]...[/quote] or [quote]...[/quote]:
  - Validates the username against known users
  - Searches up to --lookahead preceding posts in the same topic for a text match
  - If found: rewrites tag as [quote=Username post_id=X time=Y]
  - If not found with valid username: keeps [quote=Username]
  - If not found without username: keeps [quote]
  - Writes result to posts.content_quotes
  - Records each quote relationship in the quotes table

The quotes table (citing_post_id, cited_post_id, quote_author, canonical_author,
found, is_foreign) is used for later analysis — is_foreign can be set manually
after reviewing unmatched [quote] blocks to decide which are [fquote].

Usage:
    python manage.py enrich_quotes /path/to/sfiniabb.db
    python manage.py enrich_quotes /path/to/sfiniabb.db --users-db sfinia_users_real.db
    python manage.py enrich_quotes /path/to/sfiniabb.db --lookahead 100 --reset
"""

import re
import sqlite3
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

# Minimum fingerprint length to attempt matching
_MIN_FINGERPRINT = 20
# Length of fingerprint to extract (chars)
_FINGERPRINT_LEN = 120


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
    """Remove BBCode tags and normalize whitespace."""
    stripped = _BBCODE_RE.sub("", text)
    return " ".join(stripped.split()).lower()


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


def _fingerprints(inner: str) -> list[str]:
    """Return ordered list of candidate search needles for a [quote] block.

    Strategy (best → worst):
    1. Each own-text segment (text between/after nested quotes), stripped
    2. Fallback: full inner text stripped (used when all segments are too short)

    Each needle is at most _FINGERPRINT_LEN chars.
    """
    fps = []
    for seg in _own_text_segments(inner):
        fp = _strip_bbcode(seg)[:_FINGERPRINT_LEN]
        if len(fp) >= _MIN_FINGERPRINT:
            fps.append(fp)
    if not fps:
        full = _strip_bbcode(inner)[:_FINGERPRINT_LEN]
        if len(full) >= _MIN_FINGERPRINT:
            fps.append(full)
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

def _match_post(fps: list[str], window: list, canonical: str | None) -> dict | None:
    """Search window (list of post dicts, most recent last) using fps needles.

    Each window entry has a precomputed "stripped" key with the full
    lowercase stripped text (not truncated) used as the haystack.

    For each fingerprint, tries author-filtered posts first (if canonical
    given), then all posts. Returns the most-recent matching post or None.
    """
    if not fps:
        return None

    def _try_one(fp: str, posts) -> dict | None:
        for p in reversed(posts):
            if fp in p["stripped"]:
                return p
        return None

    def _try_fps(posts):
        for fp in fps:
            hit = _try_one(fp, posts)
            if hit:
                return hit
        return None

    if canonical:
        by_author = [p for p in window if p["author_name"] == canonical]
        found = _try_fps(by_author)
        if found:
            return found

    return _try_fps(window)


# ---------------------------------------------------------------------------
# Core enrichment
# ---------------------------------------------------------------------------

def _enrich_content(post_id: int, content: str,
                    window: list, known_users: dict,
                    quotes_out: list) -> str:
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
        match     = _match_post(fps, window, canonical)

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

    def handle(self, *args, **options):
        db_path    = options["archive_db"]
        users_path = options["users_db"]
        lookahead  = options["lookahead"]
        reset      = options["reset"]

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

        # --- Count posts to process ---
        total_posts = conn.execute("SELECT count(*) FROM posts WHERE content LIKE '%[quote%'").fetchone()[0]
        self.stdout.write(f"Postów z cytatami: {total_posts}")

        # --- Process topic by topic ---
        topics = [
            row[0] for row in
            conn.execute("SELECT DISTINCT topic_id FROM posts ORDER BY topic_id")
        ]
        self.stdout.write(f"Tematów: {len(topics)}")

        processed = found_count = not_found_count = 0
        all_quote_rows: list[dict] = []

        for topic_id in topics:
            topic_posts = conn.execute(
                "SELECT post_id, author_name, content, created_at "
                "FROM posts WHERE topic_id=? ORDER BY post_order ASC, post_id ASC",
                (topic_id,)
            ).fetchall()

            # window: list of recent post dicts (up to lookahead)
            window: list[dict] = []

            updates: list[tuple[str, int]] = []

            for row in topic_posts:
                post_id = row["post_id"]
                content = row["content"] or ""

                if "[quote" in content.lower():
                    quotes_for_post: list[dict] = []
                    enriched = _enrich_content(post_id, content, window, known_users, quotes_for_post)
                    updates.append((enriched, post_id))
                    for q in quotes_for_post:
                        if q["found"]:
                            found_count += 1
                        else:
                            not_found_count += 1
                    all_quote_rows.extend(quotes_for_post)
                    processed += 1

                # Add current post to window AFTER processing (can't quote yourself)
                window.append({
                    "post_id":    post_id,
                    "author_name": row["author_name"] or "",
                    "content":    content,
                    "created_at": row["created_at"] or "",
                    # Precompute full stripped text for O(n) haystack search
                    "stripped":   _strip_bbcode(content),
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
