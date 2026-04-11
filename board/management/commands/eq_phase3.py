#!/usr/bin/env python3
"""Faza 3 — Bible detection."""
import pickle
import re
import unicodedata

from eq_common import *

# ---------------------------------------------------------------------------
# Bible globals and helpers
# ---------------------------------------------------------------------------

_BIBLE_NGRAM_INDEX = None       # załadowany przez --bible-index
_BIBLE_COVERAGE    = 0.40       # minimalny % n-gramów pasujących (--bible-coverage)
_BIBLE_DRY_MIN     = 0.09       # minimalny % do pokazania w dry-run (--bible-dry-min)

_BIBLE_NGRAM_SIZE = 5


def load_bible_index(path):
    global _BIBLE_NGRAM_INDEX
    with open(path, 'rb') as f:
        data = pickle.load(f)
    _BIBLE_NGRAM_INDEX = data['ngrams']


def norm_for_bible(text):
    """Lowercase, strip diacritics, keep only alnum+space."""
    text = unicodedata.normalize('NFKD', text.lower())
    return ''.join(c for c in text
                   if unicodedata.category(c) != 'Mn' and (c.isalnum() or c == ' '))


def _bible_votes(inner):
    """Zwraca (best_ref, best_count, total_grams) dla tekstu wewnętrznego cytatu.

    Nie stosuje żadnego progu — zwraca surowe dane do dalszej oceny.
    Zwraca (None, 0, 0) jeśli brak jakiegokolwiek dopasowania.
    """
    if _BIBLE_NGRAM_INDEX is None:
        return None, 0, 0
    text = _strip_bbcode_tags(inner)
    ws = norm_for_bible(text).split()
    if not ws:
        return None, 0, 0
    n = _BIBLE_NGRAM_SIZE
    if len(ws) < n:
        key = ' '.join(ws)
        ref = _BIBLE_NGRAM_INDEX.get(key)
        if ref:
            return ref, 1, 1
        return None, 0, 0
    total_grams = len(ws) - n + 1
    ref_votes = {}
    for i in range(total_grams):
        key = ' '.join(ws[i:i + n])
        ref = _BIBLE_NGRAM_INDEX.get(key)
        if ref:
            ref_votes[ref] = ref_votes.get(ref, 0) + 1
    if not ref_votes:
        return None, 0, total_grams
    best_ref = max(ref_votes, key=ref_votes.get)
    return best_ref, ref_votes[best_ref], total_grams


def lookup_bible(inner):
    """Zwraca referencję biblijną jeśli tekst pasuje do wersetu, inaczej None.

    Wymaga minimum 2 głosów i pokrycia >= _BIBLE_COVERAGE.
    """
    ref, best_count, total_grams = _bible_votes(inner)
    if ref is None or best_count == 0:
        return None
    total_grams = max(1, total_grams)
    min_coverage = max(2, int(total_grams * _BIBLE_COVERAGE + 0.9999))  # ceil, min 2
    return ref if best_count >= min_coverage else None


# ---------------------------------------------------------------------------
# Pass: bible – zamień unresolved [quote] pasujące do Biblii na [Bible=ref]
# ---------------------------------------------------------------------------

_BIBLE_OPEN_RE = re.compile(r'\[Bible=[^\]]*\]', re.IGNORECASE)
# Dowolny otwierający tag cytatowy (quote/fquote/Bible) — do odrzucania bloków z zagnieżdżonymi cytatami
_ANY_QUOTE_OPEN_RE = re.compile(r'\[(?:quote|fquote|Bible)(?:[^\]]*)\]', re.IGNORECASE)
_BIBLE_FOUND_RE = re.compile(
    r'\[(?:quote[^\]]*post_id=\d|Bible=)[^\]]*\]', re.IGNORECASE
)

# Unresolved opening tag (bez post_id) — needed locally
_UNRESOLVED_OPEN_RE = re.compile(
    r'\[(?:f?quote)(?:="(?P<author>[^"]*)")?\]',
    re.IGNORECASE,
)


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
    if _BIBLE_NGRAM_INDEX is None:
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

            ref, votes, total_g = _bible_votes(inner)
            if ref is None or votes < 2:
                continue

            pct = votes / max(1, total_g)
            min_votes = max(2, int(total_g * _BIBLE_COVERAGE + 0.9999))  # ceil

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
            elif dry_run and votes > 1 and pct >= _BIBLE_DRY_MIN:
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
                min_v = max(2, int(total_g * _BIBLE_COVERAGE + 0.9999))
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


# ---------------------------------------------------------------------------
# Pass: bible-review-apply – zastosuj decyzje z pliku review (BIBLE/SKIP)
# ---------------------------------------------------------------------------

def run_bible_review_apply(conn, review_path, dry_run=False):
    """Czyta plik review i taguje posty oznaczone BIBLE.

    Format pliku:
        BIBLE  POST=<id>  REF=<ref>
        SKIP   POST=<id>  REF=<ref>
    """
    if _BIBLE_NGRAM_INDEX is None:
        print("BŁĄD: Bible index nie załadowany. Użyj --bible-index.")
        return 0

    # Wczytaj decyzje
    bible_decisions = {}   # post_id -> ref
    line_re = re.compile(r'^(BIBLE|SKIP)\s+POST=(\d+)\s+REF=(.+)$', re.IGNORECASE)
    with open(review_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            m = line_re.match(line)
            if not m:
                continue
            action, post_id_s, ref = m.group(1).upper(), int(m.group(2)), m.group(3).strip()
            if action == 'BIBLE':
                bible_decisions[post_id_s] = ref

    if not bible_decisions:
        print("Brak wpisów BIBLE w pliku review.")
        return 0

    print(f"  Wpisów BIBLE do zastosowania: {len(bible_decisions)}")

    # Pobierz posty
    placeholders = ','.join('?' * len(bible_decisions))
    rows = conn.execute(
        f"SELECT post_id, COALESCE(content_quotes, content) FROM posts"
        f" WHERE post_id IN ({placeholders})",
        list(bible_decisions.keys())
    ).fetchall()

    updates = []
    tagged = 0

    for post_id, content in rows:
        if not content:
            continue
        target_ref = bible_decisions[post_id]

        all_blocks = parse_quotes(content)
        unresolved = [b for b in all_blocks
                      if not _ENRICHED_TAG_RE.search(content[b.start:b.tag_end])
                      and not _BIBLE_OPEN_RE.match(content[b.start:b.tag_end])]

        def is_leaf(b):
            for other in unresolved:
                if id(other) != id(b) and other.start >= b.inner_start and other.end <= b.inner_end:
                    return False
            return True

        leaf_blocks = [b for b in unresolved if is_leaf(b)]

        # Znajdź blok który głosował na target_ref
        best_block = None
        best_votes = 0
        for b in leaf_blocks:
            inner = content[b.inner_start:b.inner_end]
            if _ANY_QUOTE_OPEN_RE.search(inner):
                continue
            ref, votes, _ = _bible_votes(inner)
            if ref == target_ref and votes > best_votes:
                best_block = b
                best_votes = votes

        if best_block is None:
            print(f"  UWAGA: nie znaleziono bloku dla POST={post_id} REF={target_ref}")
            continue

        b = best_block
        new_tag   = '[Bible=%s]' % target_ref
        new_close = '[/Bible]'
        new_content = (content[:b.start] + new_tag
                       + content[b.tag_end:b.inner_end] + new_close
                       + content[b.end:])
        tagged += 1

        if dry_run:
            inner = content[b.inner_start:b.inner_end]
            fragment = re.sub(r'\s+', ' ', _strip_bbcode_tags(inner)).strip()[:120]
            print(f"  DRY  POST={post_id}  [{target_ref}]  {fragment!r}")
            continue

        n_unresolved = len(_UNRESOLVED_OPEN_RE.findall(new_content))
        n_found      = len(_BIBLE_FOUND_RE.findall(new_content))
        if n_unresolved == 0 and n_found > 0:
            new_qs = 1
        elif n_found == 0:
            new_qs = 2
        else:
            new_qs = 3
        new_ns = 2 if _UNRESOLVED_OPEN_RE.search(new_content) else 1
        updates.append((new_content, new_qs, new_ns, post_id))

    if not dry_run and updates:
        conn.executemany(
            "UPDATE posts SET content_quotes=?, quote_status=?, nested_status=? WHERE post_id=?",
            updates,
        )
        conn.commit()

    print(f"\n  Otagowano: {tagged} postów")
    return tagged


# ---------------------------------------------------------------------------
# Pass: bible-filter – cofnij [Bible=] które nie spełniają kryterium pokrycia
# ---------------------------------------------------------------------------

def run_bible_filter(conn, dry_run=False, coverage_min=0.0):
    """Sprawdź każdy [Bible=ref] w content_quotes tym samym kryterium pokrycia co bible pass.

    Jeśli pokrycie < _BIBLE_COVERAGE I >= coverage_min → cofa na [quote]...[/quote].
    coverage_min (domyślnie 0) pozwala zobaczyć tylko zakres, np. 0.28–0.40.
    Zwraca liczbę cofniętych tagów.
    """
    if _BIBLE_NGRAM_INDEX is None:
        print("BŁĄD: Bible index nie załadowany. Użyj --bible-index.")
        return 0

    _BIBLE_TAG_RE = re.compile(
        r'\[Bible=(?P<ref>[^\]]+)\](?P<inner>.*?)\[/Bible\]',
        re.IGNORECASE | re.DOTALL,
    )

    rows = conn.execute(
        "SELECT post_id, content_quotes FROM posts"
        " WHERE content_quotes IS NOT NULL AND content_quotes LIKE '%[Bible=%'"
    ).fetchall()

    updates = []
    reverted_total = 0

    for post_id, content in rows:
        if not content:
            continue

        new_content = content
        offset = 0
        reverted = 0

        # Szukaj od lewej, śledź offset
        for m in _BIBLE_TAG_RE.finditer(content):
            ref   = m.group('ref')
            inner = m.group('inner')

            # Sprawdź pokrycie tym samym algorytmem co lookup_bible
            text = _strip_bbcode_tags(inner)
            ws = norm_for_bible(text).split()
            n = _BIBLE_NGRAM_SIZE

            is_false_positive = False

            if len(ws) < n:
                # Krótki tekst - ufamy że był poprawny
                pass
            else:
                ref_votes = {}
                for i in range(len(ws) - n + 1):
                    key = ' '.join(ws[i:i + n])
                    r = _BIBLE_NGRAM_INDEX.get(key)
                    if r:
                        ref_votes[r] = ref_votes.get(r, 0) + 1

                total_grams = len(ws) - n + 1
                if ref in ref_votes:
                    best_count   = ref_votes[ref]
                    is_nt        = ',' in ref
                    min_coverage = max(1, int(total_grams * _BIBLE_COVERAGE))
                    base_min     = 1 if is_nt else 2
                    min_votes    = max(base_min, min_coverage)
                    if best_count < min_votes:
                        is_false_positive = True
                else:
                    best_count = 0
                    total_grams = max(total_grams, 1)
                    is_false_positive = True

            if not is_false_positive:
                continue

            pct_val = best_count / max(1, total_grams)
            if pct_val < coverage_min:
                continue   # poniżej dolnego progu – pomijamy
            print(f"  COFA  post {post_id:>7}  [{ref}]  {best_count}/{total_grams} n-gramów ({pct_val*100:.0f}%)  |  {inner[:60].strip()!r}")

            # Cofnij: [Bible=ref]...[/Bible] → [quote]...[/quote]
            new_tag   = '[quote]'
            new_close = '[/quote]'
            adj_start = m.start() + offset
            adj_end   = m.end()   + offset
            adj_inner_start = adj_start + len(m.group(0)) - len(inner) - len('[/Bible]')

            new_content = (
                new_content[:adj_start]
                + new_tag
                + inner
                + new_close
                + new_content[adj_end:]
            )
            offset += (len(new_tag) + len(new_close)) - len(m.group(0))
            reverted += 1
            reverted_total += 1

        if reverted:
            # Przelicz quote_status
            n_unresolved = len(_UNRESOLVED_OPEN_RE.findall(new_content))
            n_found      = len(_BIBLE_FOUND_RE.findall(new_content))
            if n_unresolved == 0 and n_found > 0:
                new_status = 1
            elif n_found == 0:
                new_status = 2
            else:
                new_status = 3
            new_nested = 2 if _UNRESOLVED_OPEN_RE.search(new_content) else 1
            updates.append((new_content, new_status, new_nested, post_id))

    print(f"  Cofnięto false positives: {reverted_total:,} tagów w {len(updates):,} postach")

    if not dry_run and updates:
        conn.executemany(
            "UPDATE posts SET content_quotes=?, quote_status=?, nested_status=? WHERE post_id=?",
            updates,
        )
        conn.commit()

    return reverted_total
