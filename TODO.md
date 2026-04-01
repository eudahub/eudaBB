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

## Inne TODO

- Import wątków i postów z archiwum phpBB (z filtrowaniem spamu)
- Szukajka (tylko dla zalogowanych, ochrona przed DDoS) — patrz komentarze TODO w views.py
- Client-side Argon2 przy logowaniu — patrz TODO w views.py
