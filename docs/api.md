# eudaBB REST API — specyfikacja dla aplikacji Android

**Wersja:** 1.0  
**Base URL:** `https://<domena>/api/v1/`  
**Kodowanie:** UTF-8 JSON  
**Dokumentacja projektu Android:** `forum_android_project.md`, `forum_android_moderacja.md`

---

## Odchylenia od projektu Android

Projekt Android (`forum_android_project.md`) był pisany przed głębszą analizą
istniejącej implementacji serwerowej. Poniżej lista zmian względem projektu:

### 1. Parametry Argon2id

Projekt Android podawał: `pamięć 64 MB, iteracje 3, równoległość 4`.  
**Obowiązują parametry z implementacji webowej:**

| Parametr      | Wartość         |
|---------------|-----------------|
| Wariant       | Argon2id        |
| Pamięć        | 262 144 KiB (256 MB) |
| Iteracje      | 2               |
| Równoległość  | 1               |
| Długość hasha | 32 bajty → 64 hex |

Parametry można pobrać z `GET /api/v1/auth/argon2-params` przy starcie aplikacji.

### 2. Salt — deterministyczny, nie losowy

Projekt Android zakładał losowy salt per-user przechowywany w bazie.  
**Rzeczywista implementacja:** salt jest deterministyczny:

```
salt = normalize(username) + ":eudaBB"
```

gdzie `normalize()` zamienia na małe litery i usuwa diakrytyki/znaki specjalne.  
`login-init` i `register-init` obliczają i zwracają ten salt — nie ma kolumny `argon2_salt` w bazie.

### 3. Format soli — raw bytes w base64 (nie hex)

`login-init` i `register-init` zwracają `salt` jako **base64** zakodowany ciąg bajtów.  
Android klient używa `Base64.decode(salt, Base64.NO_WRAP)` przed przekazaniem do Argon2.

### 4. Email — plain text w bazie

Projekt Android zakładał przechowywanie tylko `email_hash` (SHA256).  
**Rzeczywista implementacja:** email przechowywany jest w postaci jawnej w tabeli `forum_users.email`.  
`email_hash` i `email_display` w rejestracji Android **nie są wymagane** — wystarczy `email` (plain).

### 5. Rejestracja — uproszczona (brak weryfikacji emailem)

Projekt Android miał 2-etapowy flow (jak web). **API ma 1-etapowy:**

```
register-init → [Argon2id] → register → JWT
```

Brak kodu email przy rejestracji. Email jest potrzebny tylko do resetu hasła.  

> **TODO:** Uprościć też rejestrację webową — podać email raz podczas rejestracji,
> weryfikacja emailem tylko przy resecie hasła (`request_reset`).

### 6. Push notifications (FCM) — TODO, na razie polling

Projekt Android zakładał FCM push. **Decyzja:** na razie polling.

**Powody:**
- Powiadomienia nie są jeszcze zaimplementowane w wersji webowej.
- FCM wymaga klucza Firebase Server Key po stronie serwera.
- Implementacja obu jednocześnie jest bardziej spójna.

**Obecna implementacja:**
- Endpoint `POST /api/v1/push/register` zapisuje token FCM w tabeli `api_fcm_tokens`.
- Aplikacja Android **nie otrzymuje push** — odpytuje `GET /api/v1/notifications` przy otwarciu.
- Gdy system powiadomień będzie gotowy (web + Android razem), tokeny z `api_fcm_tokens`
  zostaną użyte do wysyłki przez FCM API.

### 7. Struktura kategorii — Section > Forum

Projekt Android miał płaskie `categories`. eudaBB ma dwupoziomową strukturę:

```
Section (sekcja) → Forum (dział) → Topic (wątek) → Post
```

`GET /api/v1/categories` zwraca sekcje z zagnieżdżonymi forami.  
Identyfikator do `GET /api/v1/categories/{id}/threads` to **Forum.id**, nie Section.id.

### 8. Prywatne wiadomości — brak konwersacji

Projekt Android miał threading konwersacji.  
**Rzeczywisty model PM:** każda wiadomość jest niezależna (jak w phpBB).  
`GET /api/v1/conversations` zwraca inbox — każdy wpis to jedna wiadomość.  
Reply tworzy nową wiadomość z prefiksem "Re: " w temacie.

### 9. Unban — endpoint zmieniony

Projekt Android: `DELETE /api/v1/mod/users/{userId}/ban`  
**Implementacja:** `DELETE /api/v1/mod/users/{userId}/ban/lift`  
(Django URL routing nie obsługuje DELETE z body elegancko — zmiana dla czytelności.)

### 10. Rola `root` — nie używać w aplikacji

Konto root (`is_root=True`) to konto serwisowe — zarządzanie strukturą forum, brak emaila, brak resetu hasła.  
Aplikacja Android nie powinna używać konta root. JWT dla root zwraca `role: "root"` — aplikacja powinna go wyświetlać jako nieobsługiwany.

---

## Format odpowiedzi (envelope)

Wszystkie odpowiedzi mają jednolity format:

```json
{
  "status": "ok" | "error",
  "data": { ... } | [ ... ],
  "pagination": {
    "page": 1,
    "per_page": 20,
    "total_pages": 15,
    "total_items": 291
  },
  "error_code": "SOME_CODE",
  "error_message": "Opis błędu po polsku"
}
```

- `pagination` — tylko w odpowiedziach listowych
- `error_code` / `error_message` — tylko gdy `status: "error"`

### Kody błędów

| Kod | HTTP | Opis |
|-----|------|------|
| `MISSING_FIELD` | 400 | Brak wymaganego pola w żądaniu |
| `VALIDATION_ERROR` | 400 | Błąd walidacji treści |
| `INVALID_FIELD` | 400 | Nieprawidłowa wartość pola |
| `INVALID_CREDENTIALS` | 401 | Zły nick lub hasło |
| `INVALID_TOKEN` | 401 | Token JWT wygasł lub nieprawidłowy |
| `BANNED` | 403 | Konto zablokowane |
| `FORBIDDEN` | 403 | Brak uprawnień |
| `NO_PASSWORD` | 403 | Konto archiwalne bez hasła |
| `USER_NOT_FOUND` | 404 | Użytkownik nie istnieje |
| `TOPIC_LOCKED` | 403 | Wątek zamknięty |
| `FLOOD_LIMIT` | 429 | Przekroczono limit postów |
| `USERNAME_TAKEN` | 409 | Nick zajęty |
| `USERNAME_GHOST` | 409 | Nick archiwalny — przejąć przez admina |
| `USERNAME_RESERVED` | 400 | Nick zarezerwowany systemowo |
| `EMAIL_TAKEN` | 409 | Email już zarejestrowany |
| `EMAIL_BLOCKED` | 400 | Email na liście spamu |
| `EMAIL_DOMAIN_BLOCKED` | 400 | Domena tymczasowa/spamowa |
| `ALREADY_REPORTED` | 409 | Post już zgłoszony przez tego usera |
| `NOT_IMPLEMENTED` | 501 | Funkcja jeszcze niezaimplementowana |

---

## Autoryzacja

**Header:** `Authorization: Bearer <access_token>`

**Token JWT** zawiera claimy:
- `user_id` — ID użytkownika
- `username` — nick
- `role` — `"user"` | `"moderator"` | `"admin"` | `"root"`
- `exp` — czas wygaśnięcia

**Access token:** 1 godzina (przechowywać w RAM)  
**Refresh token:** 30 dni (przechowywać w `EncryptedSharedPreferences`)

---

## Endpointy

### Auth

#### `GET /api/v1/auth/argon2-params`
Parametry Argon2id używane przez serwer. Wywołać raz przy starcie aplikacji.

**Odpowiedź:**
```json
{
  "status": "ok",
  "data": {
    "variant": "argon2id",
    "memory_kib": 262144,
    "iterations": 2,
    "parallelism": 1,
    "hash_len": 32,
    "salt_suffix": ":eudaBB"
  }
}
```

---

#### `POST /api/v1/auth/register-init`
Sprawdza dostępność nicka i zwraca salt do prehashowania hasła.

**Żądanie:**
```json
{ "username": "jan123" }
```

**Odpowiedź:**
```json
{
  "status": "ok",
  "data": { "salt": "base64==" }
}
```

**Błędy:** `USERNAME_TAKEN` (409)

---

#### `POST /api/v1/auth/register`
Tworzy konto i zwraca JWT.

**Żądanie:**
```json
{
  "username": "jan123",
  "password_hash": "64-znakowy-hex-argon2id",
  "email": "jan@example.com"
}
```

**Odpowiedź:**
```json
{
  "status": "ok",
  "data": {
    "token": "eyJ...",
    "refresh_token": "eyJ...",
    "expires_in": 3600,
    "user": { "id": 42, "username": "jan123", "role": "user", "post_count": 0, "date_joined": "..." }
  }
}
```

**Błędy:** `USERNAME_TAKEN`, `EMAIL_TAKEN`, `EMAIL_BLOCKED`, `EMAIL_DOMAIN_BLOCKED`

---

#### `POST /api/v1/auth/login-init`
Zwraca salt dla istniejącego użytkownika.

**Żądanie:** `{ "username": "jan123" }`  
**Odpowiedź:** `{ "data": { "salt": "base64==" } }`  
**Błędy:** `USER_NOT_FOUND` (404)

---

#### `POST /api/v1/auth/login`
Weryfikuje prehash i zwraca JWT.

**Żądanie:**
```json
{ "username": "jan123", "password_hash": "64-znakowy-hex" }
```

**Odpowiedź:** jak `register`.  
**Błędy:** `INVALID_CREDENTIALS`, `BANNED`, `NO_PASSWORD`

---

#### `POST /api/v1/auth/refresh`
Rotuje refresh token.

**Żądanie:** `{ "refresh_token": "eyJ..." }`  
**Odpowiedź:** nowa para tokenów jak przy logowaniu.  
**Błędy:** `INVALID_TOKEN` (401)

---

#### `POST /api/v1/auth/logout`
Klient usuwa tokeny lokalnie. Serwer jest stateless (brak blacklisty).

Wymaga: `Authorization: Bearer <token>`

---

#### `POST /api/v1/auth/reset-request`
Wysyła 6-cyfrowy kod na email (jeśli konto istnieje i ma email).

**Żądanie:** `{ "username": "jan123" }`  
Odpowiedź jest zawsze `ok` (nie ujawnia czy konto istnieje).

---

#### `POST /api/v1/auth/reset-confirm`
Ustawia nowe hasło po weryfikacji kodu.

**Żądanie:**
```json
{
  "username": "jan123",
  "code": "123456",
  "password_hash": "64-znakowy-hex-nowego-argon2id"
}
```

---

### Forum — odczyt

#### `GET /api/v1/categories`
Drzewo sekcji z forami. Publiczne — nie wymaga autoryzacji.

**Odpowiedź:**
```json
{
  "status": "ok",
  "data": [
    {
      "id": 1, "title": "Ogólne", "order": 0,
      "forums": [
        {
          "id": 5, "title": "Dyskusje", "description": "...",
          "topic_count": 42, "post_count": 1234,
          "last_post_at": "2026-04-15T10:00:00Z",
          "subforums": []
        }
      ]
    }
  ]
}
```

---

#### `GET /api/v1/categories/{forum_id}/threads?page=1&per_page=30`
Wątki w dziale. Sortowanie: przypisane (announcement > sticky > normal), potem ostatnia aktywność.

---

#### `GET /api/v1/threads/{topic_id}/posts?page=1&per_page=20`
Posty w wątku. Każdy post zawiera `content_bbcode` (do edycji) i `content_html` (do wyświetlania).

---

#### `GET /api/v1/posts/{post_id}`
Pojedynczy post.

---

#### `GET /api/v1/users/{user_id}/profile`
Publiczny profil użytkownika.

---

#### `GET /api/v1/search?q=tekst&type=thread|post&page=1`
Wyszukiwanie w tytułach wątków (type=thread) lub treści postów (type=post).  
Minimum 2 znaki zapytania.

---

### Forum — zapis (wymaga JWT)

#### `POST /api/v1/threads`
Tworzy nowy wątek.

**Żądanie:**
```json
{ "forum_id": 5, "title": "Temat wątku", "content": "[b]Treść[/b] w BBCode" }
```

**Odpowiedź:** `{ "topic_id": 123, "post_id": 456 }`  
**Błędy:** `FLOOD_LIMIT` (429)

---

#### `POST /api/v1/threads/{topic_id}/posts`
Dodaje odpowiedź. Treść w BBCode.

**Żądanie:** `{ "content": "Odpowiedź..." }`  
**Błędy:** `TOPIC_LOCKED`, `FLOOD_LIMIT`

---

#### `PUT /api/v1/posts/{post_id}`
Edycja własnego posta.

**Żądanie:** `{ "content": "Nowa treść..." }`

---

#### `DELETE /api/v1/posts/{post_id}/delete`
Usuwa własnego posta (lub dowolnego — dla moderatora).

---

#### `POST /api/v1/posts/{post_id}/report`
Zgłasza post do moderacji.

**Żądanie:** `{ "reason": "spam" }` (opcjonalnie)

---

### Moderacja (wymaga roli `moderator` lub `admin`)

Wszystkie endpointy `/mod/` wymagają JWT z `role: moderator` lub `role: admin`.
Serwer weryfikuje rolę — aplikacja tylko ukrywa UI.

| Endpoint | Metoda | Opis |
|----------|--------|------|
| `/mod/posts/{id}` | DELETE | Usuń post |
| `/mod/posts/{id}/edit` | PUT | Edytuj post (admin) |
| `/mod/threads/{id}/lock` | PUT | Zamknij wątek |
| `/mod/threads/{id}/unlock` | PUT | Odblokuj wątek |
| `/mod/threads/{id}/pin` | PUT | Przypnij (sticky) |
| `/mod/threads/{id}/unpin` | PUT | Odepnij |
| `/mod/threads/{id}/move` | PUT | Przenieś do innego działu (admin) |
| `/mod/users/{id}/ban` | POST | Zablokuj usera |
| `/mod/users/{id}/ban/lift` | DELETE | Odblokuj usera |
| `/mod/reports` | GET | Lista zgłoszeń |
| `/mod/reports/{id}/resolve` | PUT | Zamknij zgłoszenie |
| `/mod/reports/{id}/dismiss` | PUT | Odrzuć zgłoszenie |

#### Ban — szczegóły

```json
POST /api/v1/mod/users/{id}/ban
{
  "duration_hours": 24,
  "reason": "spam"
}
```

`duration_hours: 0` = ban permanentny. Domyślnie 24h.

#### Move — szczegóły

```json
PUT /api/v1/mod/threads/{id}/move
{ "forum_id": 7 }
```

---

### Prywatne wiadomości (wymaga JWT)

#### `GET /api/v1/conversations?page=1`
Inbox (wiadomości odebrane), posortowane od najnowszych.

#### `GET /api/v1/conversations/{id}`
Pełna treść wiadomości. Automatycznie oznacza jako przeczytaną.

#### `POST /api/v1/conversations/new`
Wyślij nową wiadomość.

```json
{ "recipient": "nick_odbiorcy", "subject": "Temat", "content": "Treść BBCode" }
```

#### `POST /api/v1/conversations/{id}/reply`
Odpowiedź na wiadomość.

```json
{ "content": "Treść odpowiedzi..." }
```

---

### Powiadomienia (polling, wymaga JWT)

#### `GET /api/v1/notifications?page=1`
Lista powiadomień. **TODO:** Na razie zawsze zwraca pustą listę.  
Aplikacja powinna odpytywać przy otwarciu (nie utrzymywać stałego połączenia).

#### `PUT /api/v1/notifications/{id}/read`
Oznacz jako przeczytane. **TODO:** Stub (zwraca 501).

#### `PUT /api/v1/notifications/read-all`
Oznacz wszystkie jako przeczytane. **TODO:** Stub.

---

### Push notifications (FCM stub, wymaga JWT)

#### `POST /api/v1/push/register`
Zapisuje FCM token na serwerze.

```json
{ "token": "fcm-device-token-string" }
```

#### `DELETE /api/v1/push/unregister`
Usuwa FCM token (wywoływać przy wylogowaniu).

```json
{ "token": "fcm-device-token-string" }
```

> **TODO:** Serwer nie wysyła jeszcze push notifications przez FCM.
> Tokeny są przechowywane w tabeli `api_fcm_tokens` i będą użyte,
> gdy system powiadomień zostanie zbudowany (jednocześnie dla web i Android).

---

## Cache — zalecenia dla aplikacji

| Zasób | Cache | Odświeżanie |
|-------|-------|-------------|
| Parametry Argon2 (`/auth/argon2-params`) | Do końca sesji | Przy starcie apki |
| Kategorie (`/categories`) | 1 godzina | Pull-to-refresh |
| Lista wątków | 5 minut | Pull-to-refresh |
| Posty (aktywny wątek) | 2 minuty | Scroll do dołu / odświeżenie |
| Profil użytkownika | 1 godzina | Widok profilu |
| PM (inbox) | brak cache | Przy każdym otwarciu zakładki |

---

## Model danych — nowe tabele

### `api_fcm_tokens`
| Kolumna | Typ | Opis |
|---------|-----|------|
| id | bigint PK | |
| user_id | FK → forum_users | |
| token | varchar(255) unique | Token FCM urządzenia |
| created_at | timestamp | |
| updated_at | timestamp | |

### `api_post_reports`
| Kolumna | Typ | Opis |
|---------|-----|------|
| id | bigint PK | |
| post_id | FK → forum_posts | Zgłoszony post |
| reporter_id | FK → forum_users | Kto zgłosił |
| reason | varchar(500) | Powód (opcjonalny) |
| status | varchar(10) | `open` / `resolved` / `dismissed` |
| created_at | timestamp | |
| resolved_by_id | FK → forum_users nullable | Moderator który zamknął |
| resolved_at | timestamp nullable | |

---

## Migracja

Przed uruchomieniem aplikacji:
```bash
pip install djangorestframework djangorestframework-simplejwt PyJWT
python manage.py migrate api
```
