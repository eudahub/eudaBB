# Wyszukiwarka

Ten dokument opisuje obecną, wdrożoną wersję wyszukiwarki oraz rzeczy świadomie odłożone do `TODO.md`.

## Indeks wyszukiwania

Forum utrzymuje osobną tabelę indeksową `forum_post_search`.

Obecnie zapisuje ona dla każdego posta:
- referencję do posta
- forum, temat, autora i datę
- `content_search_author`
- `content_search_author_normalized`
- flagę `has_link`
- flagę `has_youtube`

Tekst autora do wyszukiwania:
- jest budowany bez bloków:
  - `[quote]`
  - `[fquote]`
  - `[Bible]`
  - `[AI]`
  - `[code]`
- służy zarówno do wyszukiwania, jak i do snippetów

## Budowa i inspekcja indeksu

Najważniejsze komendy:

```bash
python manage.py build_search_index
python manage.py build_search_index --forum-title "Filozofia"
python manage.py inspect_search_index --forum-title "Filozofia" --limit 20
```

`build_search_index`:
- przebudowuje indeks
- może działać dla całości albo dla jednego forum
- wypisuje też czas wykonania

`inspect_search_index`:
- niczego nie przebudowuje
- pokazuje przykładowe rekordy indeksu do kontroli jakości

## Tryby wyszukiwarki

W `/szukaj/` są obecnie dwa tryby:
- `Posty`
- `Wątki`

### `Posty`

To tryb domyślny.

Zasady:
- wyszukuje po `forum_post_search`
- zwykłe słowa są łączone przez `AND`
- frazy w cudzysłowie są zachowane jako frazy
- stop-words są pomijane tylko poza frazami
- jeśli aktywny jest sam filtr albo sam autor, zapytanie tekstowe nie jest wymagane

Dostępne filtry:
- `Wszystkie`
- `Z linkami`
- `Z YouTube`

Dostępny jest też filtr po autorze.

### `Wątki`

Ten tryb wyszukuje po `Topic.title`, nie po treści postów.

Zasady:
- tytuł wątku prowadzi na początek tematu
- link przy autorze ostatniego posta prowadzi do ostatniego posta

Dostępny filtr:
- `Wszystkie`
- `Z ankietami`

Filtr autora działa także tutaj i odnosi się do autora wątku.

## Snippety i highlight

W wynikach `Posty` snippet:
- nie jest brany z początku posta
- jest budowany wokół trafienia
- priorytet ma:
  - trafiona fraza
  - potem najrzadszy trafiony token
  - na końcu fallback

Highlight:
- działa na znormalizowanym dopasowaniu
- uwzględnia brak rozróżniania wielkości liter
- uwzględnia brak rozróżniania diakrytyków

## Kandydaci na stop-words

Analiza była robiona na `content_user` z `sfiniabb.db`.

Założenia:
- liczymy przede wszystkim `df`, czyli w ilu postach występuje słowo
- normalizacja:
  - bez rozróżniania wielkości liter
  - bez rozróżniania diakrytyków
  - bez stemmingu
- stop-words stosujemy tylko do zwykłych tokenów `AND`, nie do fraz

### Bezpieczna lista startowa

- `nie`
- `to`
- `w`
- `i`
- `sie`
- `ze`
- `na`
- `z`
- `a`
- `do`
- `o`
- `ale`

### Lista do testów

- `co`
- `jak`
- `tak`
- `bo`
- `tym`
- `tego`
- `ma`
- `czy`
- `od`
- `po`
- `ja`
- `sa`
- `za`
- `dla`
- `juz`
- `sobie`
- `byc`
- `jesli`
- `tu`

### Na razie nie pomijać

- `jest`
- `tylko`
- `moze`
- `mozna`
- `bardzo`
- `albo`

Uwagi:
- artefakty typu `b`, `http`, `www`, `pl` nie są prawdziwymi stop-words; to problem tokenizacji
- jeśli user wpisze samo słowo pomijane, system może je odrzucić i pokazać krótką informację
- jeśli user wpisze frazę typu `"do rzeczy"`, słowo `do` nie może być z niej usunięte

## Co jeszcze zostało

W `TODO.md` nadal są rzeczy jeszcze niewdrożone:
- zakres dat i godzin
- wyszukiwanie `Bible`, `fquote`, `AI`, bloków `code`
- wyszukiwanie cytatów jako osobnych obiektów
- wyszukiwanie tylko polubionych przeze mnie
- jawne `OR` jako tryb zaawansowany
- cache sesji wyszukiwania
- dalsze rozszerzenie `forum_quote_refs` o treść cytatów
