#!/usr/bin/env python3
"""Diagnostyka — analiza zagnieżdżenia tagów cytatów."""
import re

from eq_common import *

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
