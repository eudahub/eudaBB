#!/usr/bin/env python3
"""Faza 2.4 — fix-quote-post-ids: cofnij post_id jeśli wskazany post zawiera tekst tylko w cytacie."""
from eq_common import *
from eq_phase2 import _ANY_ENRICHED_QUOTE_RE, _ANY_POST_ID_RE
from eq_phase2_2 import find_deeper_quote_source, extract_nonquote_text


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
