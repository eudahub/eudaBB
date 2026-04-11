#!/usr/bin/env python3
"""Faza 2.1 — propagate: propagacja post_id do zagnieżdżonych cytatów."""
from eq_common import *
from eq_phase2 import _ENRICHED_OPEN_RE, _UNRESOLVED_OPEN_RE


def run_propagate(conn, dry_run=False, need_repair_only=False):
    """Propaguj post_id do zagnieżdżonych cytatów.

    Dla każdego postu z content_quotes:
      - znajdź enriched outer [quote post_id=N]
      - wewnątrz niego znajdź unresolved [quote]
      - sprawdź w tabeli quotes co cytował post N
      - przypisz post_id z tego wpisu

    Iteruje do stabilizacji (dla zagłębień >2).
    Zwraca liczbę wzbogaconych tagów.
    """
    # Zbuduj mapę: post_id → lista (source_post_id, quoted_user_resolved)
    # dla wpisów found=1
    print("Wczytuję mapę cytatów z tabeli quotes...", flush=True)
    citations = {}  # post_id -> [(source_post_id, author), ...]
    for row in conn.execute(
        "SELECT post_id, source_post_id, quoted_user_resolved FROM quotes WHERE found=1"
    ):
        pid, src, auth = row[0], row[1], row[2] or ''
        if pid not in citations:
            citations[pid] = []
        citations[pid].append((src, auth))
    print(f"  {len(citations):,} postów z known citations")

    # Pobierz post_author dla znalezionych source postów
    all_source_ids = set()
    for lst in citations.values():
        for src, _ in lst:
            all_source_ids.add(src)

    total_enriched = 0
    iteration = 0

    while True:
        iteration += 1
        enriched_this_iter = 0

        # Posty gdzie nested_status != 1 i content_quotes zawiera zagnieżdżone
        extra = " AND need_repair_quotes=1 AND quote_status IN (1,3)" if need_repair_only else ""
        rows = conn.execute(
            "SELECT post_id, content_quotes FROM posts"
            f" WHERE content_quotes IS NOT NULL AND (nested_status IS NULL OR nested_status != 1){extra}"
        ).fetchall()

        updates = []  # (new_content_quotes, nested_status, post_id)
        quote_inserts = []

        for post_id, cq in rows:
            # Szukaj enriched outer tagów
            outer_matches = list(_ENRICHED_OPEN_RE.finditer(cq))
            if not outer_matches:
                updates.append((cq, 1, post_id))
                continue

            # Parsuj strukturę zagnieżdżeń
            events = []
            for m in _QUOTE_OPEN_RE.finditer(cq):
                events.append((m.start(), 'open', m.end(), m.group(0)))
            for m in _QUOTE_CLOSE_RE.finditer(cq):
                events.append((m.start(), 'close', m.end(), m.group(0)))
            events.sort(key=lambda x: x[0])

            # Znajdź bloki (start, end, tag_end, opening_tag, depth_when_opened)
            stack = []
            blocks = []
            for pos, kind, end, tag_text in events:
                if kind == 'open':
                    stack.append((pos, end, tag_text))
                elif kind == 'close' and stack:
                    open_pos, open_tag_end, open_tag_text = stack.pop()
                    depth = len(stack)
                    blocks.append((open_pos, end, open_tag_end, open_tag_text, depth, len(tag_text)))

            # Dla każdego enriched outer bloku (depth=0): szukaj unresolved wewnątrz
            new_cq = cq
            offset = 0
            changed = False
            has_unresolved = False

            for b_start, b_end, b_tag_end, b_tag, b_depth, b_close_len in blocks:
                if b_depth != 0:
                    continue
                m = _ENRICHED_OPEN_RE.match(b_tag)
                if not m:
                    # Outer unresolved - nie obsługujemy tu
                    if _UNRESOLVED_OPEN_RE.match(b_tag):
                        has_unresolved = True
                    continue

                outer_cited_pid = int(m.group('post_id'))
                outer_cits = citations.get(outer_cited_pid, [])
                if not outer_cits:
                    continue

                # Buduj mapę author_lower → (source_pid, author)
                # Jeśli jeden cytat → można przypisać bez dopasowania autora
                author_map = {}  # author_lower -> (src_pid, auth) lub None jeśli ambig
                for src_pid, auth in outer_cits:
                    key = auth.lower() if auth else '__anon__'
                    if key in author_map:
                        author_map[key] = None  # ambiguous
                    else:
                        author_map[key] = (src_pid, auth)

                # Znajdź unresolved tagi wewnątrz tego outer bloku
                inner_start = b_tag_end
                inner_end = b_end - b_close_len

                # Szukaj unresolved bloków wewnętrznych
                for ib_start, ib_end, ib_tag_end, ib_tag, ib_depth, ib_close_len in blocks:
                    if ib_depth != 1:
                        continue
                    if ib_start < inner_start or ib_end > b_end:
                        continue
                    if not _UNRESOLVED_OPEN_RE.match(ib_tag):
                        continue

                    im = _UNRESOLVED_OPEN_RE.match(ib_tag)
                    inner_author = (im.group('author') or '').strip()
                    key = inner_author.lower() if inner_author else '__anon__'

                    hit = author_map.get(key)
                    # Jeśli nie ma dokładnego dopasowania a jest tylko jeden cytat
                    if hit is None and len(outer_cits) == 1:
                        hit = outer_cits[0]

                    if hit is None:
                        has_unresolved = True
                        continue

                    src_pid, src_auth = hit
                    # Zachowaj typ tagu (quote/fquote)
                    tag_type = 'fquote' if ib_tag.lower().startswith('[fquote') else 'quote'
                    if inner_author:
                        new_tag = '[%s="%s" post_id=%d]' % (tag_type, inner_author, src_pid)
                    elif src_auth:
                        new_tag = '[%s="%s" post_id=%d]' % (tag_type, src_auth, src_pid)
                    else:
                        new_tag = '[%s post_id=%d]' % (tag_type, src_pid)

                    adj_start = ib_start + offset
                    adj_tag_end = ib_tag_end + offset
                    new_cq = new_cq[:adj_start] + new_tag + new_cq[adj_tag_end:]
                    offset += len(new_tag) - len(ib_tag)
                    changed = True
                    enriched_this_iter += 1

                    quote_inserts.append((post_id, inner_author or None,
                                         src_auth or None, src_pid,
                                         None, -1, 1))

            nested_status = 2 if (has_unresolved or bool(_UNRESOLVED_OPEN_RE.search(new_cq))) else 1
            updates.append((new_cq, nested_status, post_id))

        print(f"  Iteracja {iteration}: wzbogacono {enriched_this_iter} tagów", flush=True)
        total_enriched += enriched_this_iter

        if dry_run:
            break  # w dry-run nie zapisujemy → kolejne iteracje dałyby ten sam wynik

        if updates:
            conn.executemany(
                "UPDATE posts SET content_quotes=?, nested_status=? WHERE post_id=?",
                updates,
            )
            if quote_inserts:
                conn.executemany(
                    """INSERT OR IGNORE INTO quotes
                       (post_id, quoted_user, quoted_user_resolved,
                        source_post_id, quote_text_preview, quote_index, found)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    quote_inserts,
                )
            conn.commit()
            # Zaktualizuj mapę citations o nowe wpisy
            for pid, qu, qur, src, _, _, _ in quote_inserts:
                if pid not in citations:
                    citations[pid] = []
                citations[pid].append((src, qur or ''))

        if enriched_this_iter == 0:
            break  # stabilizacja

    return total_enriched
