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

## Inne TODO

- Import wątków i postów z archiwum phpBB (z filtrowaniem spamu)
- Szukajka (tylko dla zalogowanych, ochrona przed DDoS) — patrz komentarze TODO w views.py
- Client-side Argon2 przy logowaniu — patrz TODO w views.py
