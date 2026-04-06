# TODO — eudaBB

## Prywatne wiadomości (PM) — szyfrowanie E2E

Do zrobienia gdy dojdziemy do modelu PM:

### Schemat kluczy
- Przy rejestracji: generuj parę RSA/ECC → `blob_A` = priv_key szyfrowany hasłem, `blob_B` = priv_key szyfrowany emailem → oba w bazie; pub_key plaintext
- Wysyłanie PM: szyfruj pub_key odbiorcy
- Odczyt PM: Web Crypto API w przeglądarce (klucz prywatny nigdy nie wędruje na serwer)

### Reset hasła — recovery przez email (zamiast 24 słów)
1. User wpisuje email → przeglądarka liczy Argon2(email) → serwer zwraca blob_B
2. Serwer wysyła link weryfikacyjny (potwierdza własność emaila)
3. User klika link → przeglądarka odszyfrowuje blob_B emailem → klucz_prywatny
4. Przeszyfruj nowym hasłem → nowy blob_A → wyślij na serwer
5. Stare PM-y nadal czytelne po resecie hasła ✓

Decyzja: email zamiast 24 słów — user zawsze zna swój email, 24 słowa łatwo zgubić.
Entropia niższa, ale akceptowalna dla forum — atakujący z DB musi odgadnąć plaintext emaila mając tylko hash + maskę.

### Dostęp adminów
- E2E: nawet admin serwera nie czyta PM-ów
- Opcjonalnie: audit log metadanych (kto↔kto, kiedy) bez treści — wystarczy do moderacji

---

## Przycisk "Przełącz" — ograniczenie dostępu

Obecnie "Przełącz" (wyloguj → strona logowania) widoczne dla każdego zalogowanego użytkownika.

**Docelowo:** tylko `root` i ewentualnie `is_staff`. Zwykły user nie potrzebuje — ma "Wyloguj".

**Kiedy potrzebne:** przy testowaniu uprawnień (root sprawdza jak wygląda forum oczami zwykłego usera
lub moderatora) i przy diagnozowaniu problemów konkretnego konta.

**Warianty do rozważenia gdy dojdzie czas:**

1. **Proste ukrycie** — tylko zmiana w `base.html`: `{% if user.is_root or user.is_staff %}`.
   Wystarczy jeśli "Przełącz" = logout+login.

2. **Impersonacja** (mocniejsze narzędzie) — root klika na usera w panelu admina →
   loguje się jako ten user bez znajomości jego hasła → w pasku pojawia się banner
   "Jesteś zalogowany jako X (jako root) — wróć". Implementacja: dodatkowe pole w sesji
   `_impersonating_as` + middleware który podmienia `request.user`. Dostępne jako
   gotowa paczka: `django-hijack`. Przydatne do diagnozowania "dlaczego ten user nie widzi forum X".

**Priorytet:** niski — na razie forum ma 2 użytkowników. Zrobić gdy pojawią się moderatorzy
lub gdy root będzie potrzebował testować widoki z perspektywy zwykłego usera.

---

## Weryfikacja email przy rejestracji (TODO — produkcja)

Obecnie rejestracja nie wysyła linku weryfikacyjnego — celowo, żeby ułatwić testowanie
(można dodawać konta bez dostępu do skrzynki pocztowej).

**Docelowo na produkcji:** po rejestracji wysłać link aktywacyjny na podany email.
Mechanizm jest już częściowo gotowy (ActivationToken, aktywacja przez email dla duchów) —
trzeba go rozszerzyć na zwykłą rejestrację.

---

## Tryb testowy (TEST_MODE)

**Problem:** pewne zachowania powinny być inne na dev/test niż na produkcji:
- brak weryfikacji email przy rejestracji
- "Przełącz" widoczne dla wszystkich (nie tylko root/staff)
- być może: skrócone tokeny, szybsze wygasanie sesji itp.

**Zalecane rozwiązanie: flaga w `.env`**, nie parametr `runserver`.

Powód: `runserver` to tylko serwer deweloperski, na produkcji go nie ma (jest gunicorn/uwsgi).
Flaga w `.env` działa dla obu. `DEBUG=True` jest już blisko tego co potrzeba, ale mieszanie
DEBUG z logiką biznesową (weryfikacja emaila) to zły pomysł — lepiej osobna flaga:

```python
# config/settings.py
TEST_MODE = config("TEST_MODE", default=False, cast=bool)
```

```
# .env (dev)
TEST_MODE=true

# .env (produkcja)
TEST_MODE=false
```

Użycie w kodzie:
```python
# views.py — rejestracja
if not settings.TEST_MODE:
    # wyślij link weryfikacyjny

# base.html — Przełącz
{% if user.is_root or user.is_staff or settings.TEST_MODE %}
```

**Priorytet:** zrobić przed pierwszym publicznym uruchomieniem (razem z weryfikacją email).

---

## Wyszukiwarka

### Dwa tryby — jeden formularz z zakładkami lub przełącznikiem

**A) Wyszukiwanie postów** (pełnotekstowe, domyślne)
- Backend: PostgreSQL `tsvector` / `tsquery` (FTS)
- Język: `polish` (PostgreSQL ma słownik polski — obsługuje odmianę)
- Logika słów: **zawsze AND**, nigdy OR — wpisanie „pies kot" = oba słowa muszą wystąpić
- Fraza w cudzysłowie: `"pies kot"` = dokładna sekwencja słów (phrase search, `<->` w tsquery)
- Lista stop-words: **minimalna** — tylko jednoiterowe `w`, `z`, `i`, `a`, `o`, `do`, `na`, `po`, `za`, `ze`, `ku` itp.
  Uzasadnienie: user może szukać „i" jako nick lub skrót, długa lista blokuje sensowne zapytania.
  PostgreSQL domyślnie ma obszerną listę — skonfigurować własną `TEXT SEARCH CONFIGURATION` z okrojonym słownikiem.
- Brak stemming jeśli zbyt agresywny — rozważyć `simple` config zamiast `polish` jeśli wyniki dziwne
- Wyniki: posty z podświetleniem (`ts_headline`), posortowane po rankingu (`ts_rank`)
- Dostęp: tylko zalogowani (ochrona przed DDoS/scraping)
- Paginacja wyników

**B) Wyszukiwanie wątków** (po tytule)
- Prosty `ILIKE '%...%'` lub FTS tylko na `Topic.title`
- Dodatkowy checkbox: **„tylko ankiety"** (filtr `topic_type = POLL` gdy zaimplementujemy ankiety)
  oraz **„zawiera ankietę"** — do rozważenia czy to samo
- Wyniki: lista wątków z forum, autorem, datą, ilością postów

### Implementacja techniczna

```python
# models.py — dodać do Post:
search_vector = SearchVectorField(null=True)  # django.contrib.postgres

# Indeks GIN (wymagany dla wydajności FTS):
# CREATE INDEX post_search_gin ON board_post USING GIN(search_vector);

# Aktualizacja wektora:
# — przy każdym zapisie posta (sygnał post_save)
# — lub komenda management: python manage.py update_search_vectors
# — docelowo: triggerami PostgreSQL (atomowo, bez race condition)
```

```python
# Własna konfiguracja FTS z okrojoną listą stop-words:
# CREATE TEXT SEARCH CONFIGURATION polish_forum ( COPY = polish );
# ALTER TEXT SEARCH CONFIGURATION polish_forum
#   ALTER MAPPING FOR word WITH polish_stem;
# (+ osobny plik stop-words bez długiej listy)
```

```python
# views.py — logika zapytania:
# "pies kot" (cudzysłów)  → SearchQuery("pies kot", search_type="phrase")
# pies kot (bez)          → SearchQuery("pies", ...) & SearchQuery("kot", ...)
# ts_rank dla sortowania, ts_headline dla podświetlenia
```

### URL
- `/szukaj/` — formularz + wyniki (GET z parametrami `q=`, `type=posts|topics`, `polls_only=1`)

### Integracja z systemem ignorowania (PLONK)
Wyszukiwarka musi filtrować wyniki zgodnie z listą ignorowanych danego usera:
- posty ignorowanych userów nie pojawiają się w wynikach
- wątki na liście ignorowanych wątków nie pojawiają się w wynikach
- fora ignorowane (jeśli zaimplementujemy) też wykluczone
Filtrowanie: `Post.objects.exclude(author__in=ignored_users).exclude(topic__in=ignored_topics)`
— dodać do zapytania FTS przed zwróceniem wyników.

### Priorytet
Średni — zrobić po ustabilizowaniu importu i profili użytkowników.

---

## Klasyfikacja użytkowników — dwa niezależne mechanizmy

### Rozróżnienie: klasy rozłączne vs grupy

**Klasy rozłączne** (`spam_class` na modelu User) — do PLONK i filtrowania treści:
- każdy user należy do dokładnie jednej klasy (0/1/2)
- filtrowanie: `author__spam_class__in=[1,2]` — jeden indeks, zero JOINów
- paginacja stabilna: strony oparte na globalnej liczbie postów, nie przefiltrowanej

**Grupy** (Django built-in `groups` lub flagi na modelu) — do uprawnień:
- user może należeć do wielu grup (moderator + weryfikowany + VIP)
- nie służą do filtrowania treści w PLONK
- nie muszą być rozłączne

### Klasy spamu (`spam_class`) — **ZAIMPLEMENTOWANE**

Pole `spam_class = SmallIntegerField` na modelu `User`:
```
0 = NORMAL  — normalny user (594 na sfinia)
1 = GRAY    — zaśmiecacz (51 na sfinia)
2 = WEB     — bot/spam rejestracyjny (3110 na sfinia)
```

Import z archiwum:
```
python manage.py import_spam_classes /path/to/sfinia_users_real.db
```

Admin może ręcznie zmieniać `spam_class` przez panel Django admin.
W przyszłości: możliwość dodawania nowych klas bez zmiany schematu (np. 3=troll).

---

## System ignorowania (PLONK — wzorem Usenetu)

### Paginacja — kluczowa decyzja projektowa

Wątek ma 2000 postów, 100 stron po 20. User włącza PLONK (ukrywa klasę `web`).
**Strony NIE zmieniają struktury** — strona 46 nadal zaczyna się od postu #901.
Na stronie może być mniej niż 20 postów (niektóre ukryte), nawet 0.
Posty ignorowanych zastępowane są placeholderem — nie są usuwane z paginacji.

Dlaczego tak? Stabilne URL-e stron (`?page=46`) można linkować i wracać do nich.
Gdyby strony się przesuwały przy włączaniu/wyłączaniu PLONK, linki by się psuły.

### Ignorowanie userów — trzy poziomy

**1. Klasy spamu** (skalar na userze, zero kosztu)
User zaznacza: `[x] ukryj klasę WEB`, `[ ] ukryj klasę GRAY`.
Filtr: `author__spam_class__in=ignored_classes` — jeden warunek SQL z indeksem.

**2. Indywidualny PLONK** (M2M, małe liczby)
User dodaje konkretne osoby spoza klas. Oczekiwana liczba: kilkadziesiąt.

**3. Whitelist klasy** (opcjonalna, niski priorytet)
User ignoruje klasę `web`, ale jeden jej member mu nie przeszkadza.
Whitelist nadpisuje klasę, nie nadpisuje indywidualnego PLONK.

### Model danych

```python
class UserIgnoreSettings(models.Model):
    """Ustawienia PLONK — leniwie tworzone przy pierwszym użyciu."""
    user      = models.OneToOneField(User, related_name="ignore_settings")
    hide_gray = models.BooleanField(default=False)
    hide_web  = models.BooleanField(default=True)   # domyślnie web ukryte

    ignored_users  = models.ManyToManyField(User,  blank=True, related_name="individually_ignored_by")
    ignored_topics = models.ManyToManyField(Topic, blank=True, related_name="ignored_by")
    ignored_forums = models.ManyToManyField(Forum, blank=True, related_name="ignored_by")
    whitelisted_users = models.ManyToManyField(User, blank=True, related_name="whitelisted_by")
```

### Obliczanie filtra w widoku

```python
def get_plonk_q(user):
    """Zwraca Q-obiekt wykluczający ignorowanych autorów."""
    try:
        s = user.ignore_settings
    except UserIgnoreSettings.DoesNotExist:
        return Q()

    ignored_classes = []
    if s.hide_gray: ignored_classes.append(User.SpamClass.GRAY)
    if s.hide_web:  ignored_classes.append(User.SpamClass.WEB)

    q = Q()
    if ignored_classes:
        q &= ~Q(author__spam_class__in=ignored_classes)

    whitelist = set(s.whitelisted_users.values_list("id", flat=True))
    individual = set(s.ignored_users.values_list("id", flat=True)) - whitelist
    if individual:
        q &= ~Q(author_id__in=individual)

    return q
```

### Zachowanie w widokach

| Miejsce | Zachowanie |
|---|---|
| Treść wątku | post ignorowanego → placeholder `[ukryty — kliknij aby pokazać]` |
| Lista wątków | wątki założone przez ignorowanego → ukryte całkowicie |
| Ignorowane wątki/fora | ukryte całkowicie |
| Wyszukiwarka | posty ignorowanych wykluczone z wyników |
| Liczniki forum | globalne — PLONK nie wpływa |
| Paginacja | **stabilna** — numery stron niezależne od PLONK |

### UX — strona „Mój PLONK" w profilu
- Checkboxy klas: `[x] Ukryj WEB (boty)`, `[ ] Ukryj GRAY`
- Lista indywidualnie ignorowanych z przyciskiem „Usuń z PLONK"
- Lista ignorowanych wątków i forów z przyciskiem „Usuń"
- Przycisk „Ignoruj" przy każdym poście / „Ignoruj wątek" w nagłówku wątku

### Integracja z wyszukiwarką
- `posts_qs.filter(get_plonk_q(user))` — ten sam Q-obiekt
- `exclude(topic__in=ignored_topics)` — ignorowane wątki wykluczone z wyników

### Priorytet
Średni — zrobić razem z wyszukiwarką.
`import_spam_classes` już gotowe — można od razu zapełnić `spam_class` w bazie.

## API dla aplikacji Android (REST JSON)

### Cel
Aplikacja Android czyta forum bez parsowania HTML — dostaje czysty JSON przez HTTP.
Przeglądarka nadal dostaje HTML. Dwa "fronty", ten sam Django i te same modele.

### Technologia
- **Django REST Framework (DRF)** — de facto standard, `pip install djangorestframework`
- **Autentykacja**: JWT tokeny (`djangorestframework-simplejwt`) zamiast sesji/ciasteczek
  - Android loguje się → dostaje `access_token` (krótki, ~15 min) + `refresh_token` (długi)
  - każdy request: `Authorization: Bearer <token>`
  - odświeżanie tokenu bez ponownego logowania
- **Android**: Retrofit + OkHttp + Gson/Moshi (standardowy stack)

### Endpointy (szkic)

```
GET  /api/v1/forums/                    lista forów z licznikami
GET  /api/v1/forums/<id>/topics/        lista wątków (paginacja)
GET  /api/v1/topics/<id>/posts/         posty w wątku (paginacja)
GET  /api/v1/posts/<id>/                pojedynczy post
POST /api/v1/topics/<forum_id>/         nowy wątek (wymaga auth)
POST /api/v1/posts/<topic_id>/          nowa odpowiedź (wymaga auth)

POST /api/v1/auth/token/                logowanie → {access, refresh}
POST /api/v1/auth/token/refresh/        odświeżenie access tokenu
```

### Zakres danych w JSON
- Treść postów: pole `content_bbcode` (surowy BBCode) lub `content_html` (gotowy HTML)
  — do decyzji: Android może sam renderować BBCode albo wyświetlać HTML w WebView
- Avatary: URL do `MEDIA_URL/avatars/<plik>` — Android pobiera osobno
- Paginacja: `{"count": 438444, "next": "?page=2", "results": [...]}`

### Uwagi bezpieczeństwa
- Te same reguły co dla przeglądarki: PLONK, uprawnienia, is_ghost, is_banned
- Rate limiting na endpointach publicznych (django-ratelimit lub nginx)
- Argon2 pre-hashing przy logowaniu: Android liczy Argon2 po stronie klienta
  (biblioteka Java/Kotlin: `com.lambdapioneer.argon2kt`) — tak samo jak przeglądarka WASM

### Ile roboty?
Mając gotowe modele — DRF `ModelSerializer` + `ViewSet` generuje większość automatycznie.
Szacunkowo: ~200-300 linii kodu dla pełnego read-only API (fora, wątki, posty).
Write API (nowe posty) wymaga więcej — walidacja, uprawnienia, aktualizacja liczników.

### Priorytet
Niski — zrobić po ustabilizowaniu wersji webowej. Read-only API jako pierwszy krok.

---

## Użytkownicy — liczniki i usuwanie kont

### `post_count` po imporcie

- Obecnie `forum_users.post_count` dla użytkowników importowanych zostaje na `0`, bo import userów nie przenosi historycznej liczby postów.
- Docelowy model importu:
  - najpierw import userów
  - potem import postów
  - `Post.author` ma wskazywać na `User` przez FK / `author_id`, nie tekstowy nick
  - `User.post_count` ma być liczone wyłącznie z liczby zaimportowanych postów przypisanych do tego usera
- Nie brać `post_count` z `sfinia_users_real.db`, nawet jeśli liczba wygląda sensownie.
- Po imporcie trzeba wykonać rekalkulację:
  - `User.post_count = Post.objects.filter(author=user).count()`
  - dzięki temu licznik zawsze odpowiada realnie zaimportowanym postom, a nie danym pomocniczym z tabel userów

### Usuwanie kont — wersja docelowa

- Dodać pełną ścieżkę usuwania konta przez roota/moderację, nie tylko dla pustych kont.
- Etapy:
  - usuwanie prywatnych wiadomości użytkownika
  - usuwanie postów użytkownika
  - jeśli po usunięciu postów wątek jest pusty, usunąć cały wątek
  - po usunięciu postów przeliczyć liczniki tematów, forów i userów

### Cytaty usuwanego użytkownika

- Sprawdzić, czy potrzebna jest osobna tabela/słownik cytowań analogiczna do `quotes` w `sfiniabb.db`.
- Przy usuwaniu użytkownika:
  - usuwać u innych osób cytaty pochodzące z postów usuwanego użytkownika
  - obsłużyć cytaty zagnieżdżone
  - jeśli usuwany jest tylko podcytat, zostawić nadcytat bez rozwalania reszty posta
- Najpewniej wymaga to parsera/transformacji BBCode na drzewo, nie prostego regex replace.

### Priorytet

- Później, po ustabilizowaniu podstawowego flow rejestracji i administracji użytkownikami.

---

## Paginacja i numeracja postów

### Dwa rodzaje "rzadkości" stron

**PLONK/filtrowanie** — sparse pages akceptowalne:
- różne dla każdego usera (preferencja)
- strona 46 obiektywnie zawiera te same posty dla wszystkich, tylko niektórzy widzą mniej
- nie renumerujemy

**Moderacja (usunięcie/przeniesienie bloku)** — renumeracja konieczna:
- obiektywna zmiana dla wszystkich
- moderator może przenosić bloki 20-30 postów → wiele stron pustych dla wszystkich
- po usunięciu/przeniesieniu: renumeruj `post_order` w dotkniętych wątkach

### Dwa pola — NIE są potrzebne

`id` (Django auto PK) jest już immutawalnym, rosnącym numerem oryginalnym.
`post_order` — gęsty, renumerowany po moderacji.

Nie dodawać `post_order_original` — zbędne, `id` spełnia tę rolę.

### Renumeracja po moderacji

```python
def renumber_topic(topic_id):
    from django.db import connection
    with connection.cursor() as c:
        c.execute("""
            UPDATE board_post p
            SET post_order = sub.new_order
            FROM (
                SELECT id,
                       ROW_NUMBER() OVER (ORDER BY post_order) AS new_order
                FROM board_post WHERE topic_id = %s
            ) sub
            WHERE p.id = sub.id
        """, [topic_id])
```

Uruchamiać synchronicznie po każdym usunięciu lub przeniesieniu postów.
Koszt: O(n) gdzie n = liczba postów w wątku — zwykle kilkaset, pomijalne.

### Priorytet
Zaimplementować razem z widokami moderacji (usuwanie/przenoszenie postów).

---

## Chat (kolejka wiadomości)

### Decyzja projektowa
Forum blokuje niezarejestrowanych userów od pisania postów. Chat jest kompensatą —
anonimowi mogą pisać na żywo, nawet bez konta. Zarchiwizowane posty są tylko dla
zarejestrowanych; chat jest efemeryczny.

### Charakterystyka
- Wiadomości przechowywane w DB przez konfigurowalny czas (domyślnie 4 godziny)
- Po upływie czasu — automatycznie usuwane (rolling window / kolejka)
- Dostęp: wszyscy — zarejestrowani i anonimowi
- Czas retencji ustawiany przez moderatora (np. 1h–24h)
- Anonimowi wyświetlani jako „Gość" lub z losowym identyfikatorem sesji

### Model danych

```python
class ChatMessage(models.Model):
    author      = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    author_name = models.CharField(max_length=64, default="Gość")  # dla anonimowych
    content     = models.TextField(max_length=500)
    created_at  = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]
```

### Czyszczenie starych wiadomości
Przy każdym GET lub POST czatu: usuń wiadomości starsze niż `CHAT_RETENTION_HOURS`.
Lub cron co 15 minut. Nie potrzeba Celery — prosta operacja DELETE WHERE.

```python
CHAT_RETENTION_HOURS = 4   # ustawiany przez moderatora (w DB lub settings)
```

### UX
- Prosta strona `/chat/` z listą wiadomości + formularzem
- Auto-odświeżanie co N sekund (polling) lub WebSocket (prostsze: polling)
- Limit długości wiadomości: 500 znaków
- Anonimowy może podać nick "na sesję" (przechowywany w sesji, nie w DB)
- Moderator może wyczyścić chat ręcznie lub ustawić krótszy czas retencji

### Rate limiting
- Anonimowi: max 1 wiadomość / 10 sekund (sesja)
- Zarejestrowani: max 1 wiadomość / 3 sekundy

### Priorytet
Niski — zrobić po ustabilizowaniu głównych funkcji forum.
