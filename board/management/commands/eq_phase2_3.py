#!/usr/bin/env python3
"""Faza 2.3 — fix-quote-authors: popraw autora w [quote ... post_id=N]."""
from eq_common import *
from eq_phase2 import _QUOTE_WITH_POST_ID_RE


def run_fix_quote_authors(conn, dry_run=False, need_repair_only=False):
    """Napraw autora w tagach [quote ... post_id=N] na autora posta źródłowego.

    Reguły:
      - tylko tagi [quote], bez [fquote] i [Bible]
      - tylko post_id=<liczba>, nie post_id=not_found
      - jeśli autor jest błędny albo pusty, zastąp/dopisz autora posta źródłowego
      - zachowaj pozostałe atrybuty tagu (np. time=...)
    """
    post_author = {
        int(post_id): (author_name or '').strip()
        for post_id, author_name in conn.execute(
            "SELECT post_id, author_name FROM posts"
        )
    }

    extra = " AND need_repair_quotes=1" if need_repair_only else ""
    rows = conn.execute(
        "SELECT post_id, content_quotes FROM posts"
        f" WHERE content_quotes IS NOT NULL"
        f"   AND content_quotes LIKE '%post_id=%'{extra}"
    ).fetchall()

    updates = []
    changed_posts = 0
    changed_tags = 0

    for post_id, content in rows:
        if not content:
            continue

        replacements = []
        for m in _QUOTE_WITH_POST_ID_RE.finditer(content):
            src_post_id = int(m.group('post_id'))
            resolved_author = post_author.get(src_post_id, '')
            if not resolved_author:
                continue

            current_author = (m.group('author') or '').strip()
            if current_author == resolved_author:
                continue

            new_tag = '[quote="%s"%s%s]' % (
                resolved_author,
                m.group('mid'),
                m.group('tail') or '',
            )
            replacements.append((m.start(), m.end(), new_tag, current_author, resolved_author, src_post_id))

        if not replacements:
            continue

        changed_posts += 1
        changed_tags += len(replacements)

        if dry_run:
            print(f"  POST={post_id}  quote-author fixes={len(replacements)}")
            for _, _, _, old_author, new_author, src_post_id in replacements[:5]:
                before = old_author if old_author else '<brak>'
                print(f"    post_id={src_post_id}: {before!r} -> {new_author!r}")
            continue

        new_content = content
        for start, end, new_tag, _, _, _ in reversed(replacements):
            new_content = new_content[:start] + new_tag + new_content[end:]
        updates.append((new_content, post_id))

    print(f"\n  Naprawiono autorów cytatu: {changed_tags:,} tagów w {changed_posts:,} postach")

    if not dry_run and updates:
        conn.executemany(
            "UPDATE posts SET content_quotes=? WHERE post_id=?",
            updates,
        )
        conn.commit()

    return changed_tags
