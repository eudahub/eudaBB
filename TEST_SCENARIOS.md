# Scenariusze testów ręcznych — eudaBB

Plik dla testerów. Każdy scenariusz opisuje: warunki wstępne, kroki, oczekiwany wynik.
Wymagania: serwer uruchomiony lokalnie, `TEST_MODE=true` w `.env` (chyba że zaznaczono inaczej).

---

## 1. Odzyskanie nicka przez email (nie pamiętam, pod jakim nickiem się rejestrowałem)

**Warunki wstępne:**
- Istnieje konto archiwalne (ghost) z emailem `jan@przyklad.pl` i nickiem `januszek72`
- User nie jest zalogowany

**Kroki:**
1. Wejdź na stronę logowania → kliknij „Nie pamiętasz nicka? Podaj email → odzyskaj konto"
2. Wpisz `jan@przyklad.pl` → kliknij „Wyślij"
3. Sprawdź skrzynkę `jan@przyklad.pl` (w TEST_MODE: nick pojawia się na ekranie)
4. Kliknij link aktywacyjny w emailu

**Oczekiwany wynik:**
- Konto `januszek72` aktywowane, user zalogowany
- Ekran potwierdza aktywację z nickiem
- Próba ponownej aktywacji tego samego linku → błąd „link wygasł lub już użyty"

**Weryfikacja bezpieczeństwa:**
- Wpisz email osoby, która NIE ma konta → ta sama odpowiedź „jeśli konto istnieje, wysłaliśmy…"
- Nie można ustalić ze strony, czy dany email jest w bazie

---

## 2. Rejestracja nowego konta (email już w bazie jako ghost)

**Warunki wstępne:**
- Istnieje ghost `januszek72` z emailem `jan@przyklad.pl`

**Kroki:**
1. Wejdź na `/register/`
2. Wpisz username `januszek72` (dokładnie taki sam) + email `jan@przyklad.pl` + hasło
3. Formularz powinien przekierować do aktywacji przez email
4. Wpisz email na ekranie aktywacji
5. (TEST_MODE) Konto aktywowane natychmiast

**Oczekiwany wynik:**
- Nie można zarejestrować się z takim samym nickiem jako zupełnie nowe konto
- Zamiast tego: flow aktywacji istniejącego konta archiwalnego
- Po aktywacji: zalogowany jako `januszek72`

---

## 3. Rejestracja nowego konta (email nie w bazie)

**Kroki:**
1. Wejdź na `/register/`
2. Wpisz unikalny nick, email którego nie ma w bazie, hasło
3. Kliknij „Zarejestruj"

**Oczekiwany wynik:**
- Konto utworzone, user zalogowany od razu
- (Produkcja: wymagana weryfikacja emaila — TODO)

---

## 4. Krótki email — ostrzeżenie i wybór maski

**Warunki wstępne:**
- Brak konta z emailem `jan@wp.pl`

**Kroki:**
1. Rejestracja z emailem `jan@wp.pl` (4 znaki przed @)
2. Formularz powinien pokazać żółte ostrzeżenie i radio-przyciski z wariantami maski

**Oczekiwany wynik:**
- Ostrzeżenie: „Ten email jest krótki — łatwo odgadnąć. Rozważ użycie dłuższego."
- Warianty: `j*n@wp.pl`, `j*@wp.pl`, `*n@wp.pl`, `*@wp.pl`
- Wybranie wariantu → rejestracja przechodzi

---

## 5. Filtrowanie spamu — anonimowy użytkownik

**Warunki wstępne:**
- Istnieją posty od usera z `spam_class=2` (WEB) w widocznym wątku
- Nie jesteś zalogowany

**Kroki:**
1. Otwórz wątek zawierający posty od WEB-spamera
2. Przewiń strony

**Oczekiwany wynik:**
- Posty WEB-spamera pokazane jako szary placeholder „[Post ukryty (filtr spamu) — pokaż]"
- Kliknięcie „pokaż" odkrywa treść bez przeładowania strony
- Numeracja postów i paginacja stabilna (nie skacze)

---

## 6. Filtrowanie spamu — forum niedostępne dla zwykłego usera

**Warunki wstępne:**
- Fora `Śmietnik`, `Więzienie`, `Magiel więzienny`, `Gwiezdne Wojny` mają `archive_level=2`
- Fora `Blog: IroB`, `Blog: hushek` mają `archive_level=1`

**Kroki (anonimowy):**
1. Wejdź na stronę główną
2. Sprawdź czy wyżej wymienione fora są widoczne

**Oczekiwany wynik:** żadne z tych 6 forów nie pojawia się na liście

**Kroki (zalogowany jako GRAY, spam_class=1):**
1. Zaloguj się kontem z `spam_class=1`
2. Sprawdź stronę główną

**Oczekiwany wynik:** widoczne `Blog: IroB` i `Blog: hushek`, nadal niewidoczne 4 śmietniki

**Kroki (zalogowany jako WEB, spam_class=2):**
1. Zaloguj się kontem z `spam_class=2`

**Oczekiwany wynik:** widoczne wszystkie 6 forów

---

## 7. Logowanie z Argon2 pre-hash (JavaScript)

**Kroki:**
1. Otwórz narzędzia developerskie (F12) → Console
2. Wejdź na stronę logowania
3. Wpisz hasło i kliknij „Zaloguj"
4. Obserwuj Console — powinna pojawić się informacja o haszowaniu

**Oczekiwany wynik:**
- Formularz nie wysyła surowego hasła — widać `password_is_prehashed=1` w żądaniu POST (Network tab)
- Logowanie działa poprawnie

**Fallback (bez JS):**
1. Wyłącz JS w przeglądarce
2. Zaloguj się

**Oczekiwany wynik:** logowanie nadal działa (server-side prehash jako fallback)

---

## 8. Limit rozmiaru posta

**Kroki:**
1. Zaloguj się
2. Wejdź na dowolny wątek → formularz odpowiedzi
3. Wklej tekst > 20 000 znaków (np. powtórz lorem ipsum)
4. Wyślij

**Oczekiwany wynik:**
- Błąd walidacji: „Treść za długa: X B (limit: 20000 B = 20 kB)"
- Post nie zostaje zapisany

---

## 9. Avatar — upload z walidacją

**Kroki:**
1. Zaloguj się → profil → zmień avatar
2. Próba uploadu pliku > 64 kB → oczekiwany błąd
3. Próba uploadu obrazka 200×200 px → oczekiwany błąd (max 128×128)
4. Upload poprawnego avatara (np. 80×80, 30 kB)

**Oczekiwany wynik:**
- Błędy przy krokach 2 i 3
- Krok 4: avatar pojawia się przy postach usera

---

## 10. Konto root — zakaz postowania

**Warunki wstępne:** zalogowany jako `root`

**Kroki:**
1. Wejdź na dowolne forum → „Nowy wątek"
2. Wypełnij formularz → wyślij

**Oczekiwany wynik:** `403 Forbidden` — root nie może pisać postów

---

*Dodawać nowe scenariusze w miarę implementacji kolejnych funkcji.*

---

## 11. Przypinanie wątku przez admina ([Przyklejony])

**Warunki wstępne:**
- Istnieje forum z co najmniej 3 wątkami (A, B, C) — żaden nieprzypięty
- Zalogowany jako admin (is_staff=True lub root)

**Kroki:**
1. Otwórz panel Django admin → Board → Topics
2. Znajdź wątek B → zmień `topic_type` z `Normal` na `Sticky` → zapisz
3. Wejdź na listę wątków danego forum w przeglądarce

**Oczekiwany wynik:**
- Wątek B wyświetla się jako pierwszy, przed A i C
- Wątek B oznaczony jako `[Przyklejony]` w tytule (lub ikoną)
- Pozostałe wątki posortowane jak zwykle (po dacie ostatniego posta — malejąco)
- Jeśli jest kilka przypiętych — też posortowane między sobą po dacie ostatniego posta

**Weryfikacja kolejności przy nowym poście:**
1. Dodaj nowy post do wątku A (nieprzypięty)
2. Odśwież listę wątków
3. Wątek A powinien być teraz pierwszy wśród nieprzypiętych, ale B (przypięty) nadal wyżej
