#!/usr/bin/env python3
"""Faza 2.2 — mark-not-found: oznacz cytaty znanych użytkowników jako not_found."""
from eq_common import *
from eq_phase2 import _NAMED_UNRESOLVED_RE, _ANY_POST_ID_RE, _ANY_ENRICHED_QUOTE_RE, _BIBLE_OPEN_RE, _ANY_QUOTE_OPEN_RE


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
