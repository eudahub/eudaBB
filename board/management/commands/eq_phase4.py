#!/usr/bin/env python3
"""Faza 4 — finalization."""
import re

from eq_common import *

# These regexes are needed by run_fix_status and run_to_fquote
_ANY_POST_ID_RE = re.compile(r'post_id=', re.IGNORECASE)
_BIBLE_OPEN_RE = re.compile(r'\[Bible=[^\]]*\]', re.IGNORECASE)
_BIBLE_FOUND_RE = re.compile(
    r'\[(?:quote[^\]]*post_id=\d|Bible=)[^\]]*\]', re.IGNORECASE
)
_UNRESOLVED_OPEN_RE = re.compile(
    r'\[(?:f?quote)(?:="(?P<author>[^"]*)")?\]',
    re.IGNORECASE,
)


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
