#!/usr/bin/env python3
"""Faza 2 — post-enrichment fixes."""
import re

from eq_common import *

# ---------------------------------------------------------------------------
# Pass: propagate – propagacja post_id do zagnieżdżonych cytatów
# ---------------------------------------------------------------------------

# Regex: enriched opening tag z post_id
_ENRICHED_OPEN_RE = re.compile(
    r'\[(?P<qtype>f?quote)(?:="(?P<author>[^"]*)")?\s+post_id=(?P<post_id>\d+)[^\]]*\]',
    re.IGNORECASE,
)
# Unresolved opening tag (bez post_id)
_UNRESOLVED_OPEN_RE = re.compile(
    r'\[(?:f?quote)(?:="(?P<author>[^"]*)")?\]',
    re.IGNORECASE,
)


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


# ---------------------------------------------------------------------------
# Pass: mark-not-found – oznacz cytaty znanych użytkowników jako not_found
# ---------------------------------------------------------------------------

_NAMED_UNRESOLVED_RE = re.compile(
    r'\[quote="([^"]+)"\]',
    re.IGNORECASE,
)
_ANY_POST_ID_RE = re.compile(r'post_id=', re.IGNORECASE)
_QUOTE_WITH_POST_ID_RE = re.compile(
    r'\[quote(?:="(?P<author>[^"]*)")?(?P<mid>\s+post_id=(?P<post_id>\d+))(?P<tail>[^\]]*)\]',
    re.IGNORECASE,
)
_ANY_ENRICHED_QUOTE_RE = re.compile(
    r'\[(?P<qtype>f?quote)(?:="(?P<author>[^"]*)")?(?P<mid>\s+post_id=(?P<post_id>\d+))(?P<tail>[^\]]*)\]',
    re.IGNORECASE,
)

# Bible open RE (needed by phase2 functions; imported from phase3 would create circular dep)
_BIBLE_OPEN_RE = re.compile(r'\[Bible=[^\]]*\]', re.IGNORECASE)
_ANY_QUOTE_OPEN_RE = re.compile(r'\[(?:quote|fquote|Bible)(?:[^\]]*)\]', re.IGNORECASE)
_BIBLE_FOUND_RE = re.compile(
    r'\[(?:quote[^\]]*post_id=\d|Bible=)[^\]]*\]', re.IGNORECASE
)


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


# ---------------------------------------------------------------------------
# Pass: fix-quote-authors – popraw autora w [quote ... post_id=N]
# ---------------------------------------------------------------------------

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
