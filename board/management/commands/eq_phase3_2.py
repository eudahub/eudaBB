#!/usr/bin/env python3
"""Faza 3.2 — bible-review-apply: zastosuj decyzje z pliku review (BIBLE/SKIP)."""
import re

from eq_common import *
import eq_phase3
from eq_phase3 import _BIBLE_OPEN_RE, _ANY_QUOTE_OPEN_RE, _BIBLE_FOUND_RE, _UNRESOLVED_OPEN_RE


def run_bible_review_apply(conn, review_path, dry_run=False):
    """Czyta plik review i taguje posty oznaczone BIBLE.

    Format pliku:
        BIBLE  POST=<id>  REF=<ref>
        SKIP   POST=<id>  REF=<ref>
    """
    if eq_phase3._BIBLE_NGRAM_INDEX is None:
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
            ref, votes, _ = eq_phase3._bible_votes(inner)
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
