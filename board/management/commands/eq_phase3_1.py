#!/usr/bin/env python3
"""Faza 3.1 — bible: wykryj cytaty biblijne i zamień na [Bible=ref]."""
import re

from eq_common import *
import eq_phase3
from eq_phase3 import _BIBLE_OPEN_RE, _ANY_QUOTE_OPEN_RE, _BIBLE_FOUND_RE, _UNRESOLVED_OPEN_RE


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
    if eq_phase3._BIBLE_NGRAM_INDEX is None:
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

            ref, votes, total_g = eq_phase3._bible_votes(inner)
            if ref is None or votes < 2:
                continue

            pct = votes / max(1, total_g)
            min_votes = max(2, int(total_g * eq_phase3._BIBLE_COVERAGE + 0.9999))  # ceil

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
            elif dry_run and votes > 1 and pct >= eq_phase3._BIBLE_DRY_MIN:
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
                min_v = max(2, int(total_g * eq_phase3._BIBLE_COVERAGE + 0.9999))
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
