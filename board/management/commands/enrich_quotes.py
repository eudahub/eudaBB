#!/usr/bin/env python3
"""Enrich [quote="User"] tags with post_id by matching quote text
against previous posts in the same thread by the same author.

Pass types:
  --pass known-user   (default) Only quotes where author is in sfinia_users_real.db
                      Future passes will handle unknown/misspelled authors, anonymous quotes, etc.

Usage:
    python enrich_quotes.py --pass known-user [--lookback 20] [--dry-run] [--limit N] [--reset]
"""
import re
import sqlite3
import sys

from eq_common import *
from eq_phase0 import run_mark_broken
from eq_phase1 import run_phase1
from eq_phase2 import run_propagate, run_mark_not_found, run_fix_quote_authors, run_fix_quote_post_ids
from eq_phase3 import run_bible, run_bible_filter, run_bible_review_apply, load_bible_index
import eq_phase3
from eq_phase4 import run_fix_status, run_to_fquote
from eq_diag import run_analyze_depth


def parse_args():
    import argparse
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
    eq_phase3._BIBLE_COVERAGE = args.bible_coverage
    eq_phase3._BIBLE_DRY_MIN  = args.bible_dry_min

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
        print(f"  Załadowano {len(eq_phase3._BIBLE_NGRAM_INDEX):,} n-gramów")
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

    # Phase 1 passes (known-user, known-user-global, anon-topic, anon-global, ngram)
    if reset:
        where = "content LIKE '%[quote%'"
    else:
        where = None  # run_phase1 will determine based on pass_type and null_only

    stats = run_phase1(conn, pass_type, known_users, lookback, limit, dry_run, null_only)

    processed = stats['processed']
    found_total = stats['found_total']
    not_found_total = stats['not_found_total']
    status_counts = stats['status_counts']
    content_updates = stats['content_updates']

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
