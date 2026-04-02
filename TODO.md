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

## Kategorie użytkowników (UserCategory)

Ogólny mechanizm grupowania userów przez admina — niezależny od PLONK.
Te same kategorie mogą być używane przez wiele funkcji: PLONK, ograniczenia uprawnień,
statystyki, filtrowanie widoków dla moderatorów itp.

### Dane źródłowe — sfinia_users_real.db
Kolumna `spam`: 0 = normalny, 1 = gray (51 userów), 2 = web (3110 userów).
Wartości `spam=1/2` to wynik analizy aktywności — stosunek postów w sekcjach spamu.
Import: komenda `import_user_categories` czyta te dane i tworzy kategorie `gray`/`web`.

### Model

```python
class UserCategory(models.Model):
    """Nazwana kategoria userów zarządzana przez admina."""
    name        = models.CharField(max_length=64, unique=True)  # np. "gray", "web"
    description = models.TextField(blank=True)
    members     = models.ManyToManyField(User, blank=True, related_name="categories")

    def __str__(self):
        return self.name
```

### Komenda importu
```
python manage.py import_user_categories /path/to/sfinia_users_real.db
```
- Tworzy kategorie `gray` (spam=1) i `web` (spam=2) jeśli nie istnieją
- Przypisuje userów po `username` (pomija nieznalezionych)
- Idempotentna — można uruchamiać wielokrotnie

---

## System ignorowania (PLONK — wzorem Usenetu)

### Problem skalowalności
Na sfinia.fora.pl 3110/3755 userów to web-spamerzy. Gdyby każdy user musiał ręcznie
dodawać tysiące osób do PLONK, byłoby to drogie i uciążliwe.
Rozwiązanie: PLONK korzysta z **UserCategory** — user subskrybuje kategorię `web`
jednym kliknięciem zamiast dodawać 3110 osób.

### Dwa poziomy ignorowania userów

**1. Subskrypcja kategorii**
User ignoruje całą kategorię (np. `web`, `gray`).
Koszt: O(1) na usera — jedna relacja `user → category`.

**2. Indywidualne PLONK na konkretnego usera**
Dla przypadków spoza kategorii — user dodaje konkretną osobę.
Oczekiwana liczba: dziesiątki.

**Odwrotność (whitelist) — opcjonalna**
User ignoruje kategorię `web`, ale jeden user z tej kategorii mu nie przeszkadza →
dodaje go do whitelist. Whitelist nadpisuje tylko kategorie, nie ignory indywidualne.
Priorytet niski — można dodać później bez zmian schematu.

### Model danych

```python
class UserIgnoreSettings(models.Model):
    """Ustawienia PLONK dla jednego usera. Relacja 1:1 z User (leniwie tworzona)."""
    user = models.OneToOneField(User, related_name="ignore_settings")

    # Indywidualne ignory — małe liczby
    ignored_users  = models.ManyToManyField(User,  blank=True, related_name="individually_ignored_by")
    ignored_topics = models.ManyToManyField(Topic, blank=True, related_name="ignored_by")
    ignored_forums = models.ManyToManyField(Forum, blank=True, related_name="ignored_by")

    # Subskrypcja kategorii userów (UserCategory) — skalowalne ignorowanie grup
    ignored_categories = models.ManyToManyField(
        "UserCategory", blank=True, related_name="ignored_by_users"
    )

    # Whitelist — wyłączenia spod kategorii (opcjonalne)
    whitelisted_users = models.ManyToManyField(User, blank=True, related_name="whitelisted_by")
```

### Obliczanie zbioru ignorowanych userów (helper)

```python
def get_ignored_user_ids(user) -> set[int]:
    """Zwraca set ID userów ignorowanych przez danego usera."""
    try:
        settings = user.ignore_settings
    except UserIgnoreSettings.DoesNotExist:
        return set()

    ids = set(settings.ignored_users.values_list("id", flat=True))

    for cat in settings.ignored_categories.prefetch_related("members"):
        ids.update(group.members.values_list("id", flat=True))

    # Whitelist nadpisuje listy grupowe (ale nie ignory indywidualne)
    whitelist = set(settings.whitelisted_users.values_list("id", flat=True))
    return ids - whitelist
```

Wynik cachować w sesji lub Redis (TTL ~5 min) — unikamy zapytania przy każdym requescie.

### UX — strona „Mój PLONK" w profilu

- Sekcja **Listy grupowe**: checkboxy `gray [ ]`, `dark [ ]` z opisem i liczbą członków
- Sekcja **Ignorowani userzy**: lista z przyciskiem „Usuń"; przycisk „Ignoruj" przy każdym poście
- Sekcja **Ignorowane wątki**: lista z przyciskiem „Usuń"; przycisk „Ignoruj wątek" w nagłówku wątku
- Sekcja **Ignorowane fora**: lista z przyciskiem „Usuń"; przycisk w nagłówku forum
- Sekcja **Whitelist** (jeśli zaimplementowana): userzy wyłączeni spod list grupowych

### Zachowanie w widokach

| Miejsce | Zachowanie |
|---|---|
| Lista wątków | ignorowane wątki ukryte; wątki założone przez ignorowanego ukryte |
| Treść wątku | post ignorowanego → szary placeholder „[ignorowany — kliknij]" |
| Wyszukiwarka | exclude po autorze i temacie zgodnie z PLONK |
| Liczniki forum | liczyć pomimo PLONK (liczniki są globalne, nie per-user) |

### Działanie w zapytaniach Django

```python
# Wspólny helper do użycia w views
ignored_ids    = get_ignored_user_ids(request.user)   # set[int]
ignored_topics = settings.ignored_topics.values_list("id", flat=True)
ignored_forums = settings.ignored_forums.values_list("id", flat=True)

topics_qs = (
    Topic.objects
    .exclude(author_id__in=ignored_ids)
    .exclude(pk__in=ignored_topics)
    .exclude(forum_id__in=ignored_forums)
)

posts_qs = (
    Post.objects
    .exclude(author_id__in=ignored_ids)
    .exclude(topic_id__in=ignored_topics)
    .exclude(topic__forum_id__in=ignored_forums)
)
```

### Priorytet
Średni — zrobić razem z wyszukiwarką (oba muszą współpracować).
Listy grupowe (`PlonkGroup`) warto stworzyć wcześniej — admin może zacząć
budować listę `gray`/`dark` zanim UX dla userów będzie gotowy.
