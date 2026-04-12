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

---

## 12. Reset hasła — zapomniałem hasła (kod emailowy)

**Warunki wstępne:**
- Istnieje aktywne konto `janek` z emailem `janek@przyklad.pl`
- `TEST_MODE=true`

**Kroki:**
1. Wejdź na `/login/` → kliknij „Nie pamiętasz hasła? Zresetuj je."
2. Wpisz nick `janek` → kliknij „Wyślij kod na email"
3. W TEST_MODE: kod pojawia się na ekranie — skopiuj go
4. Kliknij „wpisz kod i ustaw nowe hasło"
5. Wpisz nick `janek`, nowe hasło dwukrotnie, skopiowany kod → „Ustaw hasło"

**Oczekiwany wynik:**
- Hasło zmienione, user zalogowany, przekierowanie na `/`
- Stary kod oznaczony jako użyty — ponowne użycie tego samego kodu → błąd

**Weryfikacja rate limitu:**
1. Wróć do kroku 2 i wyślij kod 3 razy z rzędu
2. Czwarta próba → błąd „Wysłano już 3 kody w ciągu ostatniej godziny"

**Weryfikacja nieistniejącego nicka:**
- Wpisz nick którego nie ma → ta sama odpowiedź „jeśli konto istnieje…" (nie zdradza)

---

## 13. Logowanie — rate limit (20 prób/godzinę)

**Warunki wstępne:**
- Istnieje konto `janek` z poprawnym hasłem

**Kroki:**
1. Wejdź na `/login/`, wpisz nick `janek` i **złe** hasło → powtórz 20 razy
2. Przy 21. próbie wpisz **poprawne** hasło

**Oczekiwany wynik:**
- Przy próbach 1–20: komunikat „Nieprawidłowy nick lub hasło."
- Przy próbie 21 (nawet z poprawnym hasłem): „Zbyt wiele nieudanych prób logowania. Spróbuj ponownie za godzinę lub zresetuj hasło."
- Link „Nie pamiętasz hasła?" nadal dostępny — user może zresetować hasło emailem

---

## 14. Reset hasła — hasło unieważnione przez admina

**Warunki wstępne:**
- Istnieje konto `marta` z hasłem i emailem
- Admin w panelu Django (lub przez shell): `User.objects.filter(username='marta').update(password='!')` (ustawia unusable password)

**Kroki:**
1. Wejdź na `/login/`, wpisz nick `marta` i dowolne hasło → wyślij
2. Forum wykrywa unusable password → przekierowanie na `/reset-hasla/?username=marta&reason=invalidated`
3. Strona pokazuje komunikat „Twoje hasło zostało unieważnione…"
4. Nick `marta` wstępnie wypełniony — kliknij „Wyślij kod na email"
5. (TEST_MODE) Skopiuj kod z ekranu → przejdź do `/ustaw-haslo/`
6. Wpisz nick, nowe hasło × 2, kod → „Ustaw hasło"

**Oczekiwany wynik:**
- Hasło ustawione, user zalogowany
- Przy następnym logowaniu nowe hasło działa

---

## 15. Reset hasła — grace period poprzedniego kodu

**Cel:** sprawdzić że gdy user dostanie dwa kody, oba działają przez 7 minut.

**Warunki wstępne:**
- Konto `piotr` z emailem, `TEST_MODE=true`

**Kroki:**
1. Wejdź na `/reset-hasla/`, wpisz `piotr` → wyślij → skopiuj **kod #1**
2. Natychmiast wróć i wyślij ponownie → skopiuj **kod #2**
3. Wejdź na `/ustaw-haslo/`, wpisz nick + nowe hasło + **kod #1** → wyślij

**Oczekiwany wynik (w ciągu 7 minut od kroku 1):**
- Kod #1 nadal ważny — hasło zmienione, user zalogowany

**Oczekiwany wynik (po upływie 7 minut od kroku 1):**
- Kod #1 odrzucony — „Nieprawidłowy lub wygasły kod"
- Kod #2 nadal działa

**Weryfikacja kolejności przy nowym poście:**
1. Dodaj nowy post do wątku A (nieprzypięty)
2. Odśwież listę wątków
3. Wątek A powinien być teraz pierwszy wśród nieprzypiętych, ale B (przypięty) nadal wyżej

---

## 16. Tryb read-only — nikt nie może się zalogować ani pisać

**Warunki wstępne:** zalogowany jako root

**Kroki:**
1. Wejdź na `/root/config/` → ustaw tryb **Tylko do odczytu** → Zapisz
2. Wyloguj się
3. Wejdź na `/login/` → spróbuj się zalogować

**Oczekiwany wynik:**
- Strona `/login/` renderuje się (GET działa)
- Po kliknięciu „Zaloguj" (POST) → strona 503 z komunikatem o trybie read-only
- Rejestracja (`/register/`), odpowiedź w wątku, nowy wątek — wszystkie POSTy zwracają 503

**Weryfikacja wyjątku dla roota:**
1. Wejdź na `/login/` → zaloguj się jako root (POST do `/login/` jest zawsze przepuszczany)
2. Wejdź na `/root/config/` → zmień tryb z powrotem na Produkcja → Zapisz

**Oczekiwany wynik:** root może się zalogować i zapisywać (POST) nawet w trybie read-only

---

## 17. Tryb maintenance — bramka serwisowa

**Warunki wstępne:**
- Istnieje konto `root` z hasłem
- Nick `root` jest na liście serwisowej (zawsze)
- `TEST_MODE=false` (produkcja)

**Kroki:**
1. Zaloguj się jako root → `/root/config/` → tryb **Serwis** → Zapisz
2. Wyloguj się
3. Wejdź na `/` lub dowolną stronę forum

**Oczekiwany wynik:** przekierowanie na `/maintenance/`

**Kroki — przejście przez bramkę:**
1. Na `/maintenance/` wpisz nick `root` i hasło → kliknij „Zaloguj"
2. Przekierowanie na `/`
3. Wejdź na `/login/` → zaloguj się normalnie jako `root`

**Oczekiwany wynik:**
- Po etapie 1: wchodzisz na forum jako anonimowy (widoczny `root ›` w nagłówku)
- Po etapie 3: zalogowany jako `root`, widoczne `root › root  (1 IP)`
- Inny nick bez bramki → nie może wejść na forum

**Weryfikacja blokady nicka spoza listy:**
1. Na `/maintenance/` wpisz nick `januszek72` i poprawne hasło
2. Oczekiwany wynik: błąd „Ten nick nie jest na liście serwisowej."

---

## 18. Tryb maintenance — wylogowanie serwisowe

**Warunki wstępne:** przeszedłeś przez bramkę i jesteś zalogowany na forum

**Kroki:**
1. Kliknij link wylogowania serwisowego (jeśli widoczny w nawigacji)
2. Lub wejdź ręcznie na `/maintenance/logout/` (POST)

**Oczekiwany wynik:**
- Wylogowany z forum I z bramki
- Przekierowanie na `/maintenance/`
- Dowolna strona forum znowu przekierowuje na bramkę

---

## 19. Tryb maintenance — TOR przez bramkę

**Warunki wstępne:**
- TOR browser aktywny
- Nick `root` na liście serwisowej
- Forum w trybie maintenance

**Kroki:**
1. Wejdź przez TOR na `/maintenance/`
2. Zaloguj się jako `root`
3. Wejdź na `/login/` → zaloguj się jako `root` przez normalny formularz

**Oczekiwany wynik:**
- Bramka przepuszcza TOR (brak blokady na `/maintenance/`)
- Po zalogowaniu przez bramkę: można korzystać z forum przez TOR
- Normalny login przez `/login/` przechodzi (bo `maintenance_access` w sesji zwalnia z TOR-blokady)

---

## 20. Rejestracja konta tymczasowego (tryb maintenance/beta)

**Warunki wstępne:**
- Forum w trybie **Serwis** lub **Beta**
- `TEST_MODE=false`

**Kroki:**
1. Wejdź na `/register/`
2. Sprawdź czy widoczny jest fieldset „Typ konta" z dwoma opcjami — żadna nie zaznaczona
3. Kliknij „Dalej" bez wyboru → oczekiwany błąd „Wybierz typ konta"
4. Zaznacz **Konto tymczasowe**
5. Wpisz nick, email (może być wymyślony, musi być unikalny), hasło → Dalej
6. Potwierdź email → na ekranie pojawia się kod (nie wysyłany emailem)
7. Wpisz kod → utwórz konto

**Oczekiwany wynik:**
- Konto `is_temporary=True`
- Nick na liście userów oznaczony `[tymcz.]` szarym kolorem
- Profil usera pokazuje „konto tymczasowe"
- Można wysłać kod wielokrotnie (brak limitu 30 min)
- Kod wyświetlony na ekranie, email NIE wysłany

---

## 21. Rejestracja konta prawdziwego (tryb maintenance/beta)

**Warunki wstępne:**
- Forum w trybie **Serwis** lub **Beta**
- `TEST_MODE=false`, skonfigurowany SendGrid

**Kroki:**
1. Wejdź na `/register/`
2. Zaznacz **Prawdziwe konto**
3. Wpisz nick, prawdziwy email, hasło → Dalej
4. Sprawdź skrzynkę — powinna przyjść wiadomość z kodem

**Oczekiwany wynik:**
- Konto `is_temporary=False`
- Obowiązuje limit 1 kod / 30 min
- Nick na liście userów bez oznaczenia tymczasowego

---

## 22. Posty tymczasowe — tworzenie i oznaczenia

**Warunki wstępne:**
- Forum w trybie **Serwis** lub **Beta**
- Zalogowany jako dowolny user (real lub temporary)

**Kroki:**
1. Utwórz nowy wątek → opublikuj
2. Dodaj odpowiedź w innym wątku → opublikuj
3. Wejdź na stronę wątku

**Oczekiwany wynik:**
- Nowy wątek widoczny jako `[tymcz.]` na liście wątków
- Post oznaczony `[tymcz.]` w nagłówku, z szarą lewą krawędzią
- Autor tymczasowy (jeśli konto tymczasowe): szary nick + „tymczasowe" badge

---

## 23. Konwersja postu na trwały (admin)

**Warunki wstępne:**
- Forum w trybie **Serwis**
- Zalogowany jako admin (role ≥ 2)
- Istnieje wątek z kilkoma postami tymczasowymi

**Kroki — konwersja poprawna:**
1. Otwórz wątek tymczasowy
2. Znajdź post od **prawdziwego** usera (nie tymczasowego), który nie cytuje tymczasowych postów
3. Kliknij przycisk **Trwały** (zielony) → potwierdź

**Oczekiwany wynik:**
- Post przestaje być oznaczony `[tymcz.]`
- Wątek automatycznie staje się trwały (traci `[tymcz.]` na liście)

**Kroki — konwersja zablokowana (konto tymczasowe):**
1. Znajdź post od usera `is_temporary=True`
2. Przycisk **Trwały** jest wyszarzony z tooltipem „Konto tymczasowe"

**Kroki — konwersja zablokowana (cytat z tymczasowego):**
1. Jeden post cytuje inny tymczasowy post
2. Przycisk **Trwały** jest wyszarzony z tooltipem „Cytuje tymczasowe posty"

---

## 24. Czyszczenie tymczasowych danych

**Warunki wstępne:**
- Forum w trybie **Serwis** lub **Beta**
- Istnieją konta tymczasowe, posty tymczasowe, wątki tymczasowe
- Co najmniej jeden wątek ma też prawdziwy post (po konwersji z scenariusza 23)

**Kroki — ręczne czyszczenie:**
1. Zaloguj się jako `root` → `/root/config/`
2. Widoczna sekcja „Tymczasowe dane" z licznikami (konta, posty, wątki)
3. Kliknij „Wyczyść tymczasowe dane" → potwierdź

**Oczekiwany wynik:**
- Usunięte: tymczasowe konta, tymczasowe posty, wątki bez prawdziwych postów
- Wątek z prawdziwym postem: zostaje, oznaczenie `[tymcz.]` znika
- Liczniki w sekcji wróciły do 0

**Kroki — automatyczne czyszczenie przy zmianie trybu:**
1. Stwórz kilka tymczasowych postów/kont
2. Zaloguj się jako `root` → `/root/config/` → zmień tryb z **Serwis** na **Produkcja** → Zapisz

**Oczekiwany wynik:**
- Automatyczne czyszczenie przeprowadzone
- Flash message informuje ile usunięto (konta / posty / wątki)

---

## 25. Reset hasła — blokada dla tymczasowych kont

**Warunki wstępne:**
- Istnieje konto tymczasowe `tymcz01`

**Kroki:**
1. Wejdź na `/password-reset/`
2. Wpisz nick `tymcz01` → wyślij

**Oczekiwany wynik:**
- Błąd „Konta tymczasowe nie mogą resetować hasła."
- Kod NIE jest generowany ani wysyłany

---

## 26. Lista serwisowa — root zawsze obecny, niedeletowalny

**Warunki wstępne:** zalogowany jako root

**Kroki:**
1. Wejdź na `/root/config/` → sekcja „Lista serwisowa"
2. Sprawdź czy `root` jest na liście
3. Sprawdź czy przy `root` jest przycisk „usuń"

**Oczekiwany wynik:**
- `root` widoczny z etykietą `(stały)`
- Brak przycisku „usuń" przy root
- Inne nicki mają przycisk „usuń"

**Kroki — dodanie i usunięcie usera:**
1. Wpisz nick istniejącego usera w polu „Dodaj" → kliknij
2. Nick pojawia się na liście z przyciskiem „usuń"
3. Kliknij „usuń" przy tym nicku
4. Nick znika z listy

**Weryfikacja nicka nieistniejącego:**
- Wpisz nick którego nie ma w bazie → błąd „Użytkownik 'xyz' nie istnieje"

---

## 27. Tryb beta — rejestracja bez bramki, TOR zablokowany

**Warunki wstępne:** forum w trybie **Beta**

**Kroki:**
1. Wejdź na dowolną stronę forum (bez wcześniejszego logowania)

**Oczekiwany wynik:** brak przekierowania na bramkę — strona widoczna normalnie

**Kroki — TOR w trybie beta:**
1. Włącz TOR browser → wejdź na `/register/`

**Oczekiwany wynik:**
- Błąd 403 — TOR jest blokowany w trybie beta (brak wyjątku bramki)

---

*Przy zmianie URL-i pamiętaj: stare polskie ścieżki zastąpione angielskimi:*
*`/reset-hasla/` → `/password-reset/`, `/ustaw-haslo/` → `/set-password/`, `/szukaj/` → `/search/` itd.*
