#!/usr/bin/env python3
"""Faza 3.3 — bible-filter: cofnij [Bible=] które nie spełniają kryterium pokrycia."""
from eq_common import *
import eq_phase3
from eq_phase3 import _BIBLE_OPEN_RE, _BIBLE_FOUND_RE, _UNRESOLVED_OPEN_RE


def run_bible_filter(conn, dry_run=False, coverage_min=0.0):
    """Sprawdź każdy [Bible=ref] w content_quotes tym samym kryterium pokrycia co bible pass.

    Jeśli pokrycie < _BIBLE_COVERAGE I >= coverage_min → cofa na [quote]...[/quote].
    coverage_min (domyślnie 0) pozwala zobaczyć tylko zakres, np. 0.28–0.40.
    Zwraca liczbę cofniętych tagów.
    """
    import re
    if eq_phase3._BIBLE_NGRAM_INDEX is None:
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
            ws = eq_phase3.norm_for_bible(text).split()
            n = eq_phase3._BIBLE_NGRAM_SIZE

            is_false_positive = False

            if len(ws) < n:
                # Krótki tekst - ufamy że był poprawny
                pass
            else:
                ref_votes = {}
                for i in range(len(ws) - n + 1):
                    key = ' '.join(ws[i:i + n])
                    r = eq_phase3._BIBLE_NGRAM_INDEX.get(key)
                    if r:
                        ref_votes[r] = ref_votes.get(r, 0) + 1

                total_grams = len(ws) - n + 1
                if ref in ref_votes:
                    best_count   = ref_votes[ref]
                    is_nt        = ',' in ref
                    min_coverage = max(1, int(total_grams * eq_phase3._BIBLE_COVERAGE))
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
