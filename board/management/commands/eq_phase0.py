#!/usr/bin/env python3
"""Faza 0 — oznaczanie postów z niezbalansowanymi [quote]/[/quote]."""
import re

from eq_common import *

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
