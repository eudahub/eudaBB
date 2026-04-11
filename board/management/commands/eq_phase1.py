#!/usr/bin/env python3
"""Faza 1 — resolving [quote] → konkretny post."""
import re
import sqlite3

from eq_common import *


def enrich_post(content, post_id, topic_id, post_order,
                known_users, cache, lookback=20,
                pass_type='known-user', gcache=None,
                topic_cache_all=None, global_cache_all=None,
                ngram_index=None, ngram_post_author=None):
    """Process a single post's content.

    Returns (new_content_quotes, quote_status, list_of_quote_records).
    new_content_quotes: enriched content, or None if no quotes at all.
    quote_status: 0=no quotes, 1=all found, 2=none found, 3=mixed.
    """
    is_anon_pass = pass_type in ('anon-topic', 'anon-global', 'ngram')

    quotes = parse_quotes(content)
    # For anon passes: all quotes (named and anonymous)
    # For known-user passes: only named quotes
    if is_anon_pass:
        all_quotes = quotes
    else:
        all_quotes = [q for q in quotes if q.author]

    if not all_quotes:
        if _QUOTE_OPEN_RE.search(content):
            return content, 2, []
        return None, 0, []

    # For known-user passes: skip if no known author
    if not is_anon_pass:
        has_known = any(q.author.lower() in known_users for q in all_quotes)
        if not has_known:
            return content, 2, [{
                'post_id': post_id,
                'quoted_user': q.author,
                'quoted_user_resolved': q.author,
                'source_post_id': None,
                'quote_text_preview': normalize_text(extract_quote_text(content, q))[:100],
                'quote_index': i,
                'found': 0,
            } for i, q in enumerate(all_quotes)]

    new_content = content
    offset = 0
    quote_records = []
    found_count = 0
    total_count = 0

    for i, q in enumerate(all_quotes):
        # Check if already enriched (has post_id= in opening tag)
        raw_open_tag = content[q.start:q.tag_end]
        already_enriched = bool(_ENRICHED_TAG_RE.search(raw_open_tag))

        if already_enriched:
            # Count as found, no record update needed
            found_count += 1
            total_count += 1
            continue

        total_count += 1
        author_lower = q.author.lower() if q.author else None
        quote_norm = normalize_text(extract_quote_text(content, q))
        preview = quote_norm[:100]
        source_post_id = None
        resolved_author = q.author  # may be updated from matched post

        if pass_type == 'known-user':
            if author_lower and author_lower in known_users:
                candidates = cache_lookup(cache, topic_id, author_lower,
                                          post_order, lookback)
                for cand_pid, cand_norm in candidates:
                    if match_quote_in_post(quote_norm, cand_norm):
                        source_post_id = cand_pid
                        break

        elif pass_type == 'known-user-global':
            if author_lower and author_lower in known_users:
                candidates = global_cache_lookup(gcache, author_lower,
                                                 post_id, lookback)
                for cand_pid, cand_norm in candidates:
                    if match_quote_in_post(quote_norm, cand_norm):
                        source_post_id = cand_pid
                        break

        elif pass_type == 'anon-topic':
            candidates = topic_cache_all_lookup(topic_cache_all, topic_id,
                                                post_order, lookback)
            for cand_pid, cand_auth, cand_norm in candidates:
                if match_quote_in_post(quote_norm, cand_norm):
                    source_post_id = cand_pid
                    resolved_author = cand_auth
                    break

        elif pass_type == 'anon-global':
            candidates = global_cache_all_lookup(global_cache_all, post_id,
                                                 lookback)
            for cand_pid, cand_auth, cand_norm in candidates:
                if match_quote_in_post(quote_norm, cand_norm):
                    source_post_id = cand_pid
                    resolved_author = cand_auth
                    break

        elif pass_type == 'ngram':
            found_pid, found_auth = ngram_lookup(
                ngram_index, ngram_post_author, quote_norm, post_id
            )
            if found_pid is not None:
                source_post_id = found_pid
                resolved_author = found_auth

        found = 1 if source_post_id is not None else 0
        found_count += found

        quote_records.append({
            'post_id': post_id,
            'quoted_user': q.author,
            'quoted_user_resolved': resolved_author,
            'source_post_id': source_post_id,
            'quote_text_preview': preview,
            'quote_index': i,
            'found': found,
        })

        if source_post_id is not None:
            if q.author:
                new_tag = '[quote="%s" post_id=%d]' % (q.author, source_post_id)
            else:
                new_tag = '[quote post_id=%d]' % source_post_id
            new_content = (
                new_content[:q.start + offset]
                + new_tag
                + new_content[q.tag_end + offset:]
            )
            offset += len(new_tag) - len(raw_open_tag)

    if total_count == 0:
        quote_status = 0
    elif found_count == total_count:
        quote_status = 1
    elif found_count == 0:
        quote_status = 2
    else:
        quote_status = 3

    return new_content, quote_status, quote_records


def run_phase1(conn, pass_type, known_users, lookback, limit, dry_run, null_only):
    """Main processing loop for phase1 passes.

    Builds the appropriate cache based on pass_type, determines the WHERE clause,
    runs the main loop calling enrich_post for each row.
    Returns a dict with stats: processed, found_total, not_found_total, status_counts,
    content_updates, quote_inserts.
    """
    # Build in-memory cache(s)
    cache = None
    gcache = None
    tcache_all = None
    gcache_all = None
    ngram_index = None
    ngram_post_author = None

    if pass_type == 'known-user':
        cache = build_author_cache(conn, known_users)
    elif pass_type == 'known-user-global':
        gcache = build_global_cache(conn, known_users)
    elif pass_type == 'anon-topic':
        tcache_all = build_topic_cache_all(conn)
    elif pass_type == 'anon-global':
        gcache_all = build_global_cache_all(conn)
    elif pass_type == 'ngram':
        ngram_index, ngram_post_author = build_ngram_index(conn)

    # --null-only: najpierw oznacz posty bez [quote] jako quote_status=0
    if null_only and pass_type == 'known-user' and not dry_run:
        marked = conn.execute(
            "UPDATE posts SET quote_status = 0, nested_status = 0"
            " WHERE quote_status IS NULL AND content NOT LIKE '%[quote%'"
        ).rowcount
        conn.commit()
        print(f"  Oznaczono {marked:,} postów bez cytatów jako quote_status=0.")

    # Determine WHERE clause
    if null_only and pass_type == 'known-user':
        where = "content LIKE '%[quote%' AND quote_status IS NULL"
    elif pass_type == 'known-user':
        where = "content LIKE '%[quote%' AND quote_status = 0"
    else:
        where = "quote_status IN (2, 3)"

    # Count posts to process
    total_posts = conn.execute(
        f"SELECT COUNT(*) FROM posts WHERE {where}"
    ).fetchone()[0]
    print(f"Postów do przetworzenia: {total_posts:,}")

    if limit:
        total_posts = min(total_posts, limit)

    # Process posts
    batch_size = 500
    processed = 0
    found_total = 0
    not_found_total = 0
    status_counts = {0: 0, 1: 0, 2: 0, 3: 0}

    # For global pass, read content_quotes (partially enriched); else content
    content_col = "COALESCE(content_quotes, content)" if pass_type == 'known-user-global' else "content"

    cursor = conn.execute(
        f"""SELECT post_id, topic_id, {content_col}, post_order
            FROM posts WHERE {where}
            ORDER BY post_id
            {"LIMIT " + str(limit) if limit else ""}"""
    )

    content_updates = []
    quote_inserts = []

    for row in cursor:
        post_id, topic_id, content, post_order = row

        new_content, quote_status, quote_records = enrich_post(
            content, post_id, topic_id, post_order,
            known_users, cache, lookback,
            pass_type=pass_type, gcache=gcache,
            topic_cache_all=tcache_all, global_cache_all=gcache_all,
            ngram_index=ngram_index, ngram_post_author=ngram_post_author,
        )

        content_updates.append((new_content, quote_status, post_id))
        for qr in quote_records:
            quote_inserts.append((
                qr['post_id'], qr['quoted_user'], qr['quoted_user_resolved'],
                qr['source_post_id'], qr['quote_text_preview'],
                qr['quote_index'], qr['found'],
            ))

        status_counts[quote_status] = status_counts.get(quote_status, 0) + 1
        for qr in quote_records:
            if qr['found']:
                found_total += 1
            else:
                not_found_total += 1

        processed += 1

        # Progress
        if processed % 1000 == 0 or processed == total_posts:
            pct = processed / total_posts * 100 if total_posts else 0
            print(
                f"\r  [{processed:,}/{total_posts:,}] {pct:.1f}%  "
                f"found={found_total:,} not_found={not_found_total:,}",
                end='', flush=True,
            )

        # Batch write
        if len(content_updates) >= batch_size:
            if not dry_run:
                conn.executemany(
                    "UPDATE posts SET content_quotes=?, quote_status=? WHERE post_id=?",
                    content_updates,
                )
                conn.executemany(
                    """INSERT OR REPLACE INTO quotes
                       (post_id, quoted_user, quoted_user_resolved,
                        source_post_id, quote_text_preview, quote_index, found)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    quote_inserts,
                )
                conn.commit()
            content_updates.clear()
            quote_inserts.clear()

    # Final batch
    if content_updates and not dry_run:
        conn.executemany(
            "UPDATE posts SET content_quotes=?, quote_status=? WHERE post_id=?",
            content_updates,
        )
        conn.executemany(
            """INSERT INTO quotes
               (post_id, quoted_user, quoted_user_resolved,
                source_post_id, quote_text_preview, quote_index, found)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            quote_inserts,
        )
        conn.commit()

    return {
        'processed': processed,
        'found_total': found_total,
        'not_found_total': not_found_total,
        'status_counts': status_counts,
        'content_updates': content_updates,
        'quote_inserts': quote_inserts,
    }
