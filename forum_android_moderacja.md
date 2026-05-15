# Dodatek: Szybka moderacja z telefonu

Uzupełnienie do głównego projektu aplikacji Android.
Dotyczy wyłącznie funkcji, które admin/moderator musi wykonać SZYBKO
— reakcja na spam, usunięcie posta, blokada usera.
Zarządzanie kategoriami, masowe operacje, konfiguracja forum → zostają na PC.

---

## 1. ZASADA: MODERACJA KONTEKSTOWA

Nie ma osobnego "panelu admina". Funkcje moderacyjne pojawiają się
**w kontekście** — przy przeglądaniu postów, po kliknięciu w zgłoszenie.
Moderator widzi te same ekrany co zwykły user, ale z dodatkowymi opcjami.

Serwer zwraca rolę usera w tokenie JWT (claim `role`):
```json
{ "sub": "jan123", "role": "moderator", "exp": ... }
```

Aplikacja parsuje role i warunkowo pokazuje elementy UI.

Trzy role z dostępem do moderacji:
- `admin` — pełne uprawnienia
- `moderator` — usuwanie postów, blokady tymczasowe, zamykanie wątków
- `user` — brak (standardowy widok)

---

## 2. ROZSZERZONE MENU POSTA (DLA MODERATORA)

Zwykły user widzi pod postem:
```
[Cytuj]  [Odpowiedz]  [⋮]
```

Moderator widzi:
```
[Cytuj]  [Odpowiedz]  [⋮ MOD]
```

Menu "⋮ MOD" (popup/bottom sheet):

```
┌──────────────────────────────────┐
│  Kopiuj link do posta            │
│  ─────────────────────────────── │
│  🗑  Usuń post                   │  ← moderator+
│  🔒  Zamknij wątek               │  ← moderator+
│  📌  Przypnij / Odepnij wątek   │  ← moderator+
│  🚫  Zablokuj autora             │  ← moderator+
│  ─────────────────────────────── │
│  ✏️  Edytuj post                 │  ← admin only
│  🔀  Przenieś wątek              │  ← admin only
└──────────────────────────────────┘
```

Implementacja: `PostAdapter` sprawdza `UserSession.getRole()` i dodaje
odpowiednie pozycje do popup menu. Logika w osobnej klasie
`ModerationMenuBuilder`, nie w adapterze.

---

## 3. FLOW: USUNIĘCIE POSTA

Priorytet na szybkość — dwa tapy od zobaczenia spamu do usunięcia.

```
1. Moderator widzi spam w PostListFragment
2. Tap "⋮ MOD" → Tap "Usuń post"
3. Dialog potwierdzenia (KRÓTKI):
   ┌──────────────────────────────────┐
   │  Usunąć post użytkownika         │
   │  SpamerXYZ?                      │
   │                                  │
   │  ☐ Zablokuj autora od razu      │  ← checkbox!
   │                                  │
   │       [Anuluj]    [USUŃ]         │
   └──────────────────────────────────┘
4a. Jeśli checkbox odznaczony:
    DELETE /api/v1/mod/posts/{id}
    → post znika z listy (animacja)

4b. Jeśli checkbox zaznaczony:
    DELETE /api/v1/mod/posts/{id}
    + POST /api/v1/mod/users/{id}/ban  (domyślnie 24h)
    → post znika + Snackbar "Użytkownik zablokowany na 24h [Zmień]"
    → Tap "Zmień" → dialog wyboru czasu blokady
```

Kluczowe: checkbox "Zablokuj autora od razu" pozwala na usunięcie
posta i bana W JEDNYM GEŚCIE. Przy spamie to najczęstszy scenariusz.

---

## 4. FLOW: BLOKADA USERA (STANDALONE)

Gdy moderator chce zablokować usera bez kontekstu konkretnego posta
(np. widzi profil wielokrotnego spamera).

Z UserProfileActivity albo z PostListFragment (menu "⋮ MOD"):

```
┌──────────────────────────────────┐
│  Zablokuj: SpamerXYZ             │
│                                  │
│  Czas blokady:                   │
│  ○ 1 godzina                     │
│  ○ 24 godziny    ← domyślne     │
│  ○ 7 dni                         │
│  ○ 30 dni                        │
│  ○ Permanentnie                  │
│                                  │
│  Powód (opcjonalnie):            │
│  [________________________]      │
│                                  │
│       [Anuluj]  [ZABLOKUJ]       │
└──────────────────────────────────┘
```

Endpoint:
```
POST /api/v1/mod/users/{userId}/ban
{
  "duration_hours": 24,       // 0 = permanent
  "reason": "spam"            // opcjonalny
}
```

Odblokowanie:
```
DELETE /api/v1/mod/users/{userId}/ban
```

---

## 5. FLOW: REAKCJA NA ZGŁOSZENIE

Użytkownicy mogą zgłaszać posty (POST /api/v1/posts/{id}/report).
Moderator musi szybko reagować — zwłaszcza na telefonie.

### 5.1. Powiadomienie push o zgłoszeniu

Osobny kanał FCM dla moderatorów: `mod_reports`
```json
{
  "type": "report",
  "post_id": 789,
  "thread_id": 456,
  "reporter": "KowalskiJ",
  "reported_user": "SpamerXYZ",
  "reason": "Spam / reklama",
  "preview": "Kup tanie buty na..."
}
```

Powiadomienie ma priorytet HIGH → pojawia się natychmiast.
Tap → deep link prosto do zgłoszonego posta w PostListFragment.

### 5.2. Lista zgłoszeń (w NotificationsFragment)

Zgłoszenia pojawiają się w zakładce powiadomień jako osobny typ
z czerwoną ikoną "⚠". Alternatywnie — osobna zakładka w BottomNav
widoczna TYLKO dla moderatorów (badge z liczbą otwartych zgłoszeń).

Rekomendacja: **badge na istniejącej ikonce powiadomień** (prostsze).
Wewnątrz NotificationsFragment — filtr: "Wszystkie | Zgłoszenia".

### 5.3. Widok zgłoszenia

Po tapnięciu w zgłoszenie moderator widzi:

```
┌──────────────────────────────────┐
│ ⚠ Zgłoszenie                     │
│ Zgłosił: KowalskiJ               │
│ Powód: Spam / reklama            │
│──────────────────────────────────│
│ ┌────────────────────────────┐   │
│ │ SpamerXYZ    dziś 14:32    │   │
│ │ Kup tanie buty na...       │   │
│ │ www.spam-link.com          │   │
│ └────────────────────────────┘   │
│                                  │
│ [Odrzuć zgłoszenie]              │
│ [Usuń post]                      │
│ [Usuń post + Zablokuj autora]   │  ← jeden tap
└──────────────────────────────────┘
```

Trzy przyciski — najczęstszy scenariusz (spam) to jeden tap na
"Usuń post + Zablokuj autora".

---

## 6. ENDPOINTY MODERACYJNE

```
DELETE  /api/v1/mod/posts/{postId}             → soft-delete posta
PUT     /api/v1/mod/threads/{threadId}/lock     → zamknij wątek
PUT     /api/v1/mod/threads/{threadId}/unlock   → odblokuj wątek
PUT     /api/v1/mod/threads/{threadId}/pin      → przypnij
PUT     /api/v1/mod/threads/{threadId}/unpin    → odepnij
POST    /api/v1/mod/users/{userId}/ban          → zablokuj usera
DELETE  /api/v1/mod/users/{userId}/ban          → odblokuj
GET     /api/v1/mod/reports?status=open&page=1  → lista zgłoszeń
PUT     /api/v1/mod/reports/{reportId}/resolve  → zamknij zgłoszenie
PUT     /api/v1/mod/reports/{reportId}/dismiss  → odrzuć zgłoszenie
PUT     /api/v1/mod/posts/{postId}              → edycja posta (admin)
PUT     /api/v1/mod/threads/{threadId}/move     → przeniesienie (admin)
```

Wszystkie endpointy `/mod/` wymagają roli moderator lub admin w tokenie.
Serwer sprawdza rolę — aplikacja tylko ukrywa UI, ale zabezpieczenie
jest po stronie serwera.

---

## 7. STRUKTURA KODU — NOWE KLASY

Dodane do struktury z głównego dokumentu:

```
pl.forum.android/
├── ui/
│   ├── postlist/
│   │   ├── ModerationMenuBuilder.java    // buduje menu mod w zależności od roli
│   │   └── ModerationDialogs.java        // dialogi: usuń, zablokuj, potwierdź
│   ├── moderation/
│   │   ├── ReportListFragment.java       // lista zgłoszeń (filtr w notifications)
│   │   ├── ReportDetailSheet.java        // BottomSheet ze zgłoszeniem
│   │   └── BanDialogFragment.java        // dialog wyboru czasu blokady
│   └── common/
│       └── UserSession.java              // singleton: rola, userId, token info
│
├── data/
│   ├── remote/
│   │   └── ModerationApiService.java     // Retrofit interface dla /mod/
│   └── repository/
│       └── ModerationRepository.java     // logika moderacyjna
```

UserSession — parsuje JWT i wystawia:
```java
public class UserSession {
    public String getUsername();
    public String getRole();        // "admin", "moderator", "user"
    public boolean canModerate();   // role == admin || moderator
    public boolean isAdmin();       // role == admin
}
```

---

## 8. SWIPE ACTIONS NA LIŚCIE POSTÓW (OPCJONALNIE)

Dla jeszcze szybszej moderacji — swipe na poście:

- Swipe w lewo → czerwone tło + ikona kosza → "Usuń post"
- Swipe w prawo → pomarańczowe tło + ikona bana → "Zablokuj autora"

Włączone TYLKO dla moderatorów. Implementacja: `ItemTouchHelper`
z custom `SimpleCallback`. Po swipe — dialog potwierdzenia
(nie usuwaj od razu, zbyt łatwo o przypadek na telefonie).

---

## 9. PODSUMOWANIE — PRIORYTET IMPLEMENTACJI

Przy implementacji moderacji zaczynaj od:

1. UserSession + parsowanie roli z JWT
2. ModerationApiService + ModerationRepository
3. Rozszerzone menu posta (ModerationMenuBuilder)
4. Dialog "Usuń post" z checkboxem bana
5. BanDialogFragment (standalone)
6. Powiadomienia push o zgłoszeniach
7. ReportDetailSheet
8. Swipe actions (opcja, na końcu)

Punkty 1-5 to absolutne minimum — reakcja na spam w 2 tapach.
Punkty 6-7 to wygoda — powiadomienie zamiast F5 na liście.
Punkt 8 to bonus.
