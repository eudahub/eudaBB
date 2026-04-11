# Log sesji roboczych

## 2026-04-11

### Integracja postów adminów z główną bazą (sfinia_full.db)

- Scalono `forums_admin` → `forums` (section_id=7/Biuro, order 10,11,12..., access BLOGGER=3)
- Scalono `topics_admin` → `topics` (3 duplikaty zachowane z topics, INSERT OR IGNORE)
- Scalono `posts_admin` → `posts`; dodano kolumnę `need_repair_quotes` (0=oryginalne, 1=z admin)
- Zmieniono `quote_status`/`nested_status` z NOT NULL DEFAULT 0 na nullable (NULL = nieprzetworzone)
- VACUUM: sfinia_full.db 4.2 GB → 2.2 GB

### Naprawy parsowania BBCode (search_index.py)

- `_TAG_RE`: ograniczono do `[A-Za-z0-9]{0,15}` — brak dopasowań przez newline, spacje, non-ASCII
- Wydzielono `_strip_block_tags()` jako wspólny rdzeń
- Dodano `extract_content_user()` — zachowuje strukturę akapitów (wiele pustych linii → jedna)

### Pipeline enrich_quotes dla need_repair_quotes=1 (11 668 postów)

Uruchomiono wszystkie fazy w kolejności:
1. `mark-broken` (--null-only) → 41 postów z niezbalansowanymi tagami (quote_status=4)
2. `known-user --lookback 50 --null-only` → 8 968 bez cytatów (status=0), 1 052 znalezione
3. `known-user-global --lookback 50` → +187 znalezionych
4. `anon-topic --lookback 50` → +417 znalezionych
5. `anon-global --lookback 50` → +73 znalezionych
6. `ngram` (indeks z ~450k postów) → +344 znalezionych
7. `propagate --null-only` → 59 tagów w zagnieżdżonych cytatach
8. `fix-quote-authors --null-only` → 1 316 tagów w 746 postach
9. `fix-quote-post-ids --null-only` → 0
10. `mark-not-found --null-only` → 0
11. `bible --bible-coverage 0.30` → 35 tagów [Bible=] (v≥2 minimum)
12. `to-fquote --null-only` → 493 tagów [quote]→[fquote] w 282 postach
13. `fix-status --null-only` → 10 postów

Wynik końcowy (need_repair_quotes=1): status=0: 8968, status=1: 2659, status=4: 41

### Nowe management commands

- `add_order_columns` — dodaje kolumnę `"order"` do sections/forums w SQLite
- `export_forum_order` — eksport kolejności forów (json/csv)
- `fill_content_user` — wypełnia content_user z content, zachowuje akapity
- `flush_except_morph` — TRUNCATE CASCADE z pominięciem tabel morfologicznych
- `merge_admin_forums` — scala forums_admin → forums w SQLite
- `sqlite_make_quote_nullable` — migracja quote_status/nested_status na nullable

### Admin UI i linki

- Widoki `admin_order*` + template — zamawianie sekcji i forów strzałkami ▲▼
- Link user list → wyszukiwarka po autorze (`/szukaj/?author=`)
- Link wyszukiwarka → lista użytkowników (`/uzytkownicy/?q=`)

### Refaktor reimport scripts

- Wszystkie 5 skryptów używają teraz `sfinia_full.db` (zamiast osobnych baz)
- `flush_except_morph` zamiast `flush` — słownik morfologiczny zachowany
- Nowy `rebuild_morph.sh` do osobnej przebudowy słownika

### Podział enrich_quotes.py

Plik 2415 linii rozbity na 15 modułów (≤11 KB każdy):
`eq_common`, `eq_phase0`, `eq_phase1`, `eq_phase2` (hub regex) + `_1/_2/_3/_4`,
`eq_phase3` (hub globale) + `_1/_2/_3`, `eq_phase4`, `eq_diag`  
`enrich_quotes.py` → 233 linie (tylko entry point)

### Commity

- `0412391` Integrate admin posts, fix quote enrichment, add order UI and search links
- `c4a26be` Split enrich_quotes.py into phase modules (eq_common, eq_phase0-4, eq_diag)
- `0b8d6de` Split eq_phase2 and eq_phase3 into per-command files
- `21138be` Update README_pl with expanded eq_phase2/3 file descriptions
