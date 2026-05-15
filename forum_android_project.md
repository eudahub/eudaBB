# Projekt aplikacji Android — klient forum

**Stack:** Java 17 + XML Views (Android SDK)
**Min SDK:** 26 (Android 8.0) — obejmuje ~95% urządzeń
**Target SDK:** 35

---

## 1. ARCHITEKTURA OGÓLNA

### 1.1. Wzorzec: MVVM + Repository

```
┌─────────────────────────────────────────────────────────┐
│                      UI LAYER                           │
│  Activity / Fragment  ←──  ViewModel  ←──  LiveData     │
└────────────────────────────┬────────────────────────────┘
                             │
┌────────────────────────────┴────────────────────────────┐
│                   REPOSITORY LAYER                      │
│  ForumRepository  ─── AuthRepository  ─── DraftRepo     │
└──────┬─────────────────────┬───────────────────┬────────┘
       │                     │                   │
┌──────┴──────┐    ┌─────────┴────────┐   ┌──────┴──────┐
│ Remote API  │    │  Local DB        │   │ SharedPrefs  │
│ (Retrofit)  │    │  (Room/SQLite)   │   │ (EncPrefs)   │
└─────────────┘    └──────────────────┘   └─────────────┘
```

Zasada: Activity/Fragment **nigdy** nie wywołuje API bezpośrednio.
ViewModel pośredniczy, Repository decyduje czy pobierać z sieci czy z cache.

### 1.2. Komunikacja z serwerem

Serwer forum udostępnia REST JSON API. Aplikacja Android jest jednym
z dwóch klientów — obok przeglądarki webowej.

**Base URL:** konfigurowalny w ustawieniach (np. `https://forum.example.pl/api/v1/`)

**Format odpowiedzi (uniwersalny envelope):**
```json
{
  "status": "ok" | "error",
  "data": { ... },
  "error_code": "INVALID_TOKEN",
  "error_message": "Sesja wygasła",
  "pagination": {
    "page": 1,
    "per_page": 20,
    "total_pages": 15,
    "total_items": 291
  }
}
```

---

## 2. BEZPIECZEŃSTWO I AUTORYZACJA

### 2.1. Client-side Argon2 hashing

Kluczowa cecha architektury: serwer **nigdy nie widzi** plaintextowego hasła.
Klient wylicza hash Argon2id **przed** wysłaniem do serwera.

**Biblioteka:** `org.signal:argon2` (implementacja Signal, czysta Java/JNI)

**Parametry Argon2id (identyczne w web i Android):**
```
Wariant:       Argon2id
Pamięć:        65536 KiB (64 MB)
Iteracje:      3
Równoległość:  4
Długość hasha: 32 bajty
Salt:          pobierany z serwera (per-user, stały)
```

UWAGA: Parametry pamięci 64 MB mogą być obciążające dla starszych telefonów.
Rozważyć fallback: 32 MB dla urządzeń z <4 GB RAM (wykrywane dynamicznie
przez ActivityManager.getMemoryInfo()).

#### 2.1.1. Flow rejestracji

```
Android                              Serwer
  │                                    │
  │  POST /api/v1/auth/register-init   │
  │  { "username": "jan123" }          │
  │  ─────────────────────────────────>│
  │                                    │  generuje salt (16 B, random)
  │  { "salt": "base64..." }           │
  │  <─────────────────────────────────│
  │                                    │
  │  [Argon2id(password, salt)]        │
  │  [Argon2id(email, salt)]           │
  │                                    │
  │  POST /api/v1/auth/register        │
  │  { "username": "jan123",           │
  │    "password_hash": "base64...",   │
  │    "email_hash": "sha256hex",      │
  │    "email_display": "bo***@gm*"  } │
  │  ─────────────────────────────────>│
  │                                    │  serwer hashuje password_hash
  │                                    │  ponownie bcrypt/Argon2 i zapisuje
  │  { "status": "ok",                │
  │    "token": "jwt..." }             │
  │  <─────────────────────────────────│
```

**email_hash** — SHA256 z pełnego adresu email (do weryfikacji unikalności)
**email_display** — zamaskowana wersja widoczna w profilu (np. "bo***@gm***")

#### 2.1.2. Flow logowania

```
Android                              Serwer
  │                                    │
  │  POST /api/v1/auth/login-init      │
  │  { "username": "jan123" }          │
  │  ─────────────────────────────────>│
  │                                    │
  │  { "salt": "base64..." }           │
  │  <─────────────────────────────────│  zwraca salt dla usera
  │                                    │
  │  [Argon2id(password, salt)]        │
  │                                    │
  │  POST /api/v1/auth/login           │
  │  { "username": "jan123",           │
  │    "password_hash": "base64..." }  │
  │  ─────────────────────────────────>│
  │                                    │  weryfikuje hash
  │  { "token": "jwt...",              │
  │    "refresh_token": "...",         │
  │    "expires_in": 3600 }            │
  │  <─────────────────────────────────│
```

#### 2.1.3. Flow resetu hasła

```
Android                              Serwer
  │                                    │
  │  POST /api/v1/auth/reset-request   │
  │  { "username": "jan123",           │
  │    "email_hash": "sha256hex" }     │
  │  ─────────────────────────────────>│
  │                                    │  porównuje email_hash z bazą
  │                                    │  jeśli pasuje → wysyła email
  │  { "status": "ok",                │   z tokenem na prawdziwy mail
  │    "message": "Jeśli dane są      │   (serwer zna email z osobnej
  │     poprawne, wysłano email" }     │    tabeli backupu / albo nie)
  │  <─────────────────────────────────│
```

UWAGA: Ponieważ serwer nie przechowuje plain-text emaila (tylko SHA256 hash),
trzeba zdecydować o strategii dostarczenia tokenu reset. Opcje:
  a) Serwer przechowuje email zaszyfrowany kluczem serwera (nie hash) —
     wtedy może wysłać mail. Hash SHA256 służy tylko do weryfikacji unikalności.
  b) Reset wymaga kontaktu z adminem (bardziej paranoiczne, mniej UX).
  c) Reset przez pytanie bezpieczeństwa ustawione przy rejestracji.
Rekomendacja: opcja (a) — szyfrowany email + SHA256 hash osobno.

### 2.2. Zarządzanie tokenami

**Access token:** JWT, czas życia 1h, przechowywany w pamięci (RAM).
**Refresh token:** losowy ciąg, czas życia 30 dni, w EncryptedSharedPreferences.

**Interceptor OkHttp:**
```
Każdy request → dodaje nagłówek Authorization: Bearer <access_token>
Jeśli odpowiedź 401 → automatyczny refresh → retry oryginalnego requestu
Jeśli refresh też 401 → wylogowanie, ekran logowania
```

### 2.3. Przechowywanie danych wrażliwych

- Token JWT: `EncryptedSharedPreferences` (AndroidX Security)
- Hasło: **nigdy** nie zapisywane, nawet zahashowane
- Zapamiętana nazwa usera: zwykłe SharedPreferences (nie jest wrażliwa)

---

## 3. ENDPOINTY API

### 3.1. Autoryzacja

```
POST   /api/v1/auth/register-init     → { salt }
POST   /api/v1/auth/register          → { token, refresh_token, user }
POST   /api/v1/auth/login-init        → { salt }
POST   /api/v1/auth/login             → { token, refresh_token, user }
POST   /api/v1/auth/refresh           → { token, refresh_token }
POST   /api/v1/auth/logout            → { status }
POST   /api/v1/auth/reset-request     → { status, message }
POST   /api/v1/auth/reset-confirm     → { status }
```

### 3.2. Forum — odczyt

```
GET    /api/v1/categories                        → lista działów
GET    /api/v1/categories/{id}/threads?page=1    → wątki w dziale
GET    /api/v1/threads/{id}/posts?page=1         → posty w wątku
GET    /api/v1/posts/{id}                        → pojedynczy post
GET    /api/v1/users/{id}/profile                → profil usera
GET    /api/v1/search?q=...&type=thread|post     → wyszukiwanie
```

### 3.3. Forum — zapis

```
POST   /api/v1/threads                  → tworzenie wątku
POST   /api/v1/threads/{id}/posts       → tworzenie posta (odpowiedź)
PUT    /api/v1/posts/{id}               → edycja posta
DELETE /api/v1/posts/{id}               → usuwanie posta (soft-delete)
POST   /api/v1/posts/{id}/report        → zgłoszenie posta
```

### 3.4. Powiadomienia

```
GET    /api/v1/notifications?page=1&unread=true  → lista powiadomień
PUT    /api/v1/notifications/{id}/read           → oznacz jako przeczytane
PUT    /api/v1/notifications/read-all            → oznacz wszystkie
POST   /api/v1/push/register                     → rejestracja tokenu FCM
DELETE /api/v1/push/unregister                    → wyrejestrowanie
```

### 3.5. Prywatne wiadomości

```
GET    /api/v1/conversations                      → lista konwersacji
GET    /api/v1/conversations/{id}/messages?page=1  → wiadomości
POST   /api/v1/conversations                       → nowa konwersacja
POST   /api/v1/conversations/{id}/messages          → nowa wiadomość
```

---

## 4. MODEL DANYCH (ROOM DATABASE — CACHE OFFLINE)

### 4.1. Encje

```java
@Entity(tableName = "categories")
public class CategoryEntity {
    @PrimaryKey
    public long id;
    public String name;
    public String description;
    public int sortOrder;
    public int threadCount;
    public long lastActivityTimestamp;
    public long cachedAt;  // timestamp pobrania — do inwalidacji
}

@Entity(tableName = "threads",
    indices = {@Index("categoryId"), @Index("lastPostTimestamp")})
public class ThreadEntity {
    @PrimaryKey
    public long id;
    public long categoryId;
    public String title;
    public long authorId;
    public String authorName;
    public long createdAt;
    public long lastPostTimestamp;
    public int postCount;
    public int viewCount;
    public boolean isPinned;
    public boolean isLocked;
    public long cachedAt;
}

@Entity(tableName = "posts",
    indices = {@Index("threadId")})
public class PostEntity {
    @PrimaryKey
    public long id;
    public long threadId;
    public long authorId;
    public String authorName;
    public String authorAvatarUrl;
    public String contentBbcode;   // oryginalny BBCode/Markdown
    public String contentHtml;     // przetworzony HTML do wyświetlenia
    public long createdAt;
    public long editedAt;          // 0 jeśli nie edytowany
    public int likeCount;
    public long cachedAt;
}

@Entity(tableName = "drafts")
public class DraftEntity {
    @PrimaryKey(autoGenerate = true)
    public long id;
    public long threadId;          // 0 = nowy wątek
    public long categoryId;        // jeśli nowy wątek
    public String title;           // jeśli nowy wątek
    public String content;         // treść z BBCode/cytatami
    public long quotedPostId;      // 0 jeśli brak cytatu
    public long savedAt;
}

@Entity(tableName = "notifications")
public class NotificationEntity {
    @PrimaryKey
    public long id;
    public String type;            // "reply", "quote", "mention", "pm"
    public long threadId;
    public long postId;
    public long fromUserId;
    public String fromUserName;
    public String summary;         // skrót treści
    public boolean isRead;
    public long createdAt;
}
```

### 4.2. Strategia cache'owania

```
Kategorie:   cache 1h, odświeżanie pull-to-refresh
Wątki:       cache 5 min, paginacja z serwera
Posty:       cache 2 min (aktywny wątek), 30 min (nieaktywne)
Profil:      cache 1h
Drafty:      tylko lokalnie, bez limitu czasowego
```

Polityka czyszczenia: przy starcie app usuwaj cache starszy niż 7 dni.

---

## 5. STRUKTURA EKRANÓW (NAVIGATION)

### 5.1. Graf nawigacyjny

```
┌─────────────┐
│ SplashScreen│──→ sprawdza token ──→ ┌────────────────┐
└─────────────┘       ↓ brak         │ LoginActivity   │
                      │              │ ├─ LoginFragment │
                      │              │ └─ RegisterFrag  │
                      │              └────────┬─────────┘
                      ↓ jest token           │ sukces
                ┌─────┴──────────────────────┴──────┐
                │          MainActivity              │
                │  ┌────────────────────────────┐   │
                │  │  BottomNavigationView       │   │
                │  │  ┌────┬────┬────┬────┐     │   │
                │  │  │Home│Noti│ PM │Prof│     │   │
                │  │  └──┬─┴──┬─┴──┬─┴──┬─┘     │   │
                │  └─────┼────┼────┼────┼───────┘   │
                │        ↓    ↓    ↓    ↓           │
                │   NavHost (fragmenty)              │
                └───────────────────────────────────┘
                         │
         ┌───────────────┼───────────────────┐
         ↓               ↓                   ↓
   CategoryList    ThreadList           PostList
         │               │                   │
         └──→ ThreadList  └──→ PostList       └──→ ComposePost
                                                    (nowy / cytuj)
```

### 5.2. Opis ekranów

#### E1. SplashScreen
- Sprawdza czy jest zapisany refresh_token
- Jeśli tak → próbuje odświeżyć access_token → MainActivity
- Jeśli nie → LoginActivity
- Animacja logo forum (opcjonalnie)

#### E2. LoginActivity
- **LoginFragment:** pola username + hasło, przycisk „Zaloguj"
  - Pobiera salt z `/login-init`
  - Wylicza Argon2id w tle (pokazuje ProgressBar z info „Obliczanie...")
  - Wysyła hash do `/login`
- **RegisterFragment:** username + email + hasło + powtórz hasło
  - Walidacja siły hasła (min. 8 znaków, nie sam lowercase)
  - Pobiera salt z `/register-init`
  - Wylicza Argon2id(hasło, salt) + SHA256(email) + maskę email
  - Wysyła do `/register`
- Przełączanie login↔register: ViewPager2 lub zwykły replace fragment

#### E3. HomeFragment (lista kategorii/działów)
- RecyclerView z kategoriami
- Każda kategoria: nazwa, opis, liczba wątków, ostatnia aktywność
- Pull-to-refresh
- Tap → ThreadListFragment

#### E4. ThreadListFragment
- Toolbar z nazwą kategorii
- RecyclerView z wątkami (sticky: przypięte na górze)
- Każdy wątek: tytuł, autor, liczba postów, data ostatniego posta
- FAB „+" → nowy wątek (ComposeActivity)
- Paginacja: infinite scroll lub przyciski stron
- Pull-to-refresh

#### E5. PostListFragment
- Toolbar z tytułem wątku
- RecyclerView z postami
- Każdy post zawiera:
  ```
  ┌──────────────────────────────────────┐
  │ [Avatar] NazwaUsera    14:32, 2 kwi  │
  │──────────────────────────────────────│
  │ ┌─ Cytat od: Kowalski ────────────┐ │
  │ │ Treść cytowanego posta...        │ │
  │ └──────────────────────────────────┘ │
  │                                      │
  │ Treść posta z formatowaniem...       │
  │ **pogrubienie**, _kursywa_           │
  │                                      │
  │──────────────────────────────────────│
  │ [Cytuj]  [Odpowiedz]  [⋮ Więcej]    │
  └──────────────────────────────────────┘
  ```
- „Cytuj" → ComposeActivity z prefilowanym cytatem
- „Odpowiedz" → ComposeActivity (bez cytatu)
- „⋮ Więcej" → popup: Zgłoś / Edytuj (własne) / Kopiuj link
- Paginacja: infinite scroll
- FAB „Odpowiedz" → ComposeActivity

#### E6. ComposeActivity (osobna Activity, nie fragment)
- Dlaczego osobna Activity: pełny ekran, własny lifecycle,
  niezależny od nawigacji forum, łatwy powrót z back-button
- Tryby: NOWY_WĄTEK, ODPOWIEDŹ, ODPOWIEDŹ_Z_CYTATEM, EDYCJA
- Layout:
  ```
  ┌──────────────────────────────────────┐
  │ [←] Nowy post              [Wyślij]  │
  │──────────────────────────────────────│
  │ [Tytuł wątku]  ← tylko nowy wątek   │
  │──────────────────────────────────────│
  │ Toolbar formatowania:                │
  │ [B] [I] [U] [Link] [Img] [Kod]      │
  │  [Cytuj] [Lista]                     │
  │──────────────────────────────────────│
  │                                      │
  │ ┌─ Cytat od: Kowalski ────────────┐ │
  │ │ Cytowany tekst (edytowalny!)     │ │
  │ │ Można zaznaczyć i usunąć         │ │
  │ │ fragment cytatu                  │ │
  │ └──────────────────────────────────┘ │
  │                                      │
  │ Tutaj piszesz swoją odpowiedź...     │
  │ Tekst jest edytowalny normalnie,     │
  │ z pełną selekcją i kursorem.         │
  │                                      │
  │                                      │
  │                                      │
  └──────────────────────────────────────┘
  ```

##### E6.1. Edytor tekstu — kluczowy komponent

**Problem z forum w przeglądarce na telefonie:**
- Małe literki, trudna selekcja tekstu (brak myszki)
- Cytaty to często surowy BBCode w textarea — trudne do edycji
- Brak WYSIWYG na mobile web

**Rozwiązanie w aplikacji:**
- Custom EditText z obsługą BBCode/Markdown w tle
- Cytat renderowany jako wizualny blok (tło, margines, ikona)
- Selekcja tekstu natywna Androidowa — długie przyciśnięcie,
  uchwyty do zaznaczania, cut/copy/paste z systemu
- Toolbar formatowania: zaznacz tekst → tap [B] → otacza **...**
- Cytat wstawiany jako blok z indeksem — użytkownik widzi
  kolorowy blok, a pod spodem jest [quote=user]...[/quote]
- Możliwość przycinania cytatu: zaznacz fragment → „Usuń zaznaczenie"

**Implementacja:**
- `ForumEditText extends AppCompatEditText`
- Używa `SpannableStringBuilder` do wizualizacji bloków
- Konwersja do BBCode/Markdown przy wysyłce (metoda `toBBCode()`)
- Konwersja z BBCode na Spannable przy ładowaniu (metoda `fromBBCode()`)

**Obsługiwane znaczniki:**
```
[b]...[/b]           → pogrubienie (StyleSpan BOLD)
[i]...[/i]           → kursywa (StyleSpan ITALIC)
[u]...[/u]           → podkreślenie (UnderlineSpan)
[s]...[/s]           → przekreślenie (StrikethroughSpan)
[url=...]...[/url]   → link (URLSpan + kolor)
[img]...[/img]       → obrazek (ImageSpan, lazy-loaded)
[code]...[/code]     → monospace (TypefaceSpan)
[quote=user]...[/quote] → blok cytatu (QuoteSpan + tło)
[list][*]...[/list]  → lista punktowana (BulletSpan)
[color=...]...[/color]  → kolor tekstu (ForegroundColorSpan)
```

##### E6.2. Autozapis draftu

- Co 10 sekund (lub po 3 sekundach bezczynności) → zapis do Room DB
- Przy wejściu na ComposeActivity sprawdza czy jest draft dla
  danego threadId → pyta „Kontynuować pisanie?"
- Draft przechowuje: threadId, categoryId, tytuł, treść BBCode,
  quotedPostId, timestamp

#### E7. NotificationsFragment
- RecyclerView z powiadomieniami
- Typy: odpowiedź, cytat, wzmianki, PM
- Tap → nawigacja do odpowiedniego posta/konwersacji
- Swipe → oznacz jako przeczytane
- Toolbar: „Oznacz wszystkie jako przeczytane"
- Badge na BottomNav z liczbą nieprzeczytanych

#### E8. ConversationsFragment (Prywatne wiadomości)
- Lista konwersacji (jak messaging app)
- Tap → MessageListFragment (wygląd jak chat)
- FAB → nowa konwersacja

#### E9. ProfileFragment
- Wyświetla profil zalogowanego usera
- Avatar, nazwa, data rejestracji, liczba postów
- Przyciski: Edytuj profil, Ustawienia, Wyloguj
- Wylogowanie: czyści tokeny + EncryptedSharedPreferences

#### E10. UserProfileActivity (cudzy profil)
- Avatar, nazwa, statystyki
- Lista ostatnich postów użytkownika
- Przycisk „Wyślij wiadomość"

#### E11. SearchActivity
- SearchView w toolbarze
- Wyniki: wątki i posty (w dwóch zakładkach TabLayout)
- Debounce 500ms na wpisywanie

#### E12. SettingsActivity
- URL serwera forum (zmiana instance)
- Motyw: jasny / ciemny / systemowy
- Rozmiar czcionki postów (suwak)
- Powiadomienia: włącz/wyłącz, dźwięk
- Cache: wyczyść / informacja o rozmiarze
- O aplikacji / wersja

---

## 6. SYSTEM CYTATÓW — SZCZEGÓŁOWY PROJEKT

To kluczowa przewaga aplikacji nad przeglądarką.

### 6.1. Cytowanie pełnego posta

1. User tapuje „Cytuj" pod postem
2. ComposeActivity otwiera się z pre-wstawionym blokiem:
   ```
   [quote=NazwaUsera, post=12345]
   Cała treść cytowanego posta
   [/quote]
   
   |  ← kursor tutaj
   ```
3. Blok cytatu renderowany jako wizualny element (szare tło,
   lewa krawędź kolorowa, nagłówek „Cytat od: NazwaUsera")
4. User może kliknąć w blok cytatu i edytować go:
   - Zaznaczyć fragment i usunąć (przyciąć cytat)
   - Lub wybrać „Usuń cały cytat"

### 6.2. Cytowanie zaznaczonego fragmentu

1. User w PostListFragment **długo przyciska** na tekście posta
2. Pojawia się system selekcji tekstu Androida (uchwyty)
3. User zaznacza fragment
4. W ActionMode (pasek u góry) pojawia się opcja **„Cytuj zaznaczenie"**
5. ComposeActivity otwiera się z blokiem zawierającym tylko zaznaczony tekst

Implementacja: PostListFragment renderuje posty przez WebView lub
TextView ze Spannable. Dla zaznaczania fragmentów — `TextView` z
`setTextIsSelectable(true)` + custom ActionMode.Callback.

### 6.3. Cytowanie wielokrotne

1. User tapuje „Cytuj" pod postem #1 → post dodany do „kolejki cytatów"
2. Na dole ekranu pojawia się Snackbar/pasek: „1 cytat w schowku [Otwórz edytor]"
3. User tapuje „Cytuj" pod postem #2 → „2 cytaty w schowku"
4. Tap „Otwórz edytor" → ComposeActivity z obydwoma cytatami

Implementacja: `QuoteClipboard` — singleton trzymający listę cytatów
w pamięci (czyszczony po wysłaniu posta lub explicit dismiss).

---

## 7. POWIADOMIENIA PUSH (FCM)

### 7.1. Architektura

```
Serwer forum → Firebase Cloud Messaging → Android
```

Przy logowaniu aplikacja:
1. Pobiera token FCM z `FirebaseMessaging.getInstance().getToken()`
2. Wysyła go do serwera: `POST /api/v1/push/register { fcm_token, device_id }`

Serwer przy nowym poście/PM:
1. Sprawdza subskrypcje (kto obserwuje wątek)
2. Wysyła do FCM dane powiadomienia

### 7.2. Typy powiadomień

```json
{
  "type": "reply",
  "thread_id": 456,
  "post_id": 789,
  "from_user": "Kowalski",
  "thread_title": "Jaki Linux na serwer?",
  "preview": "Moim zdaniem Debian jest lepszy bo..."
}
```

Typy: `reply`, `quote`, `mention`, `pm`, `thread_locked`, `post_deleted`

### 7.3. Wyświetlanie

- `NotificationChannel` "Forum" z domyślnym dźwiękiem
- Osobny kanał "Prywatne wiadomości" (wyższy priorytet)
- Tap na powiadomienie → deep link do posta/konwersacji
- Grupowanie: wiele odpowiedzi w jednym wątku → jedno grouped notification

---

## 8. OBSŁUGA OFFLINE I SYNCHRONIZACJA

### 8.1. Zasady

- **Odczyt:** zawsze próbuj sieć, fallback na cache Room
- **Zapis:** jeśli brak sieci → zapisz draft, pokaż komunikat
  „Post zostanie wysłany po przywróceniu połączenia"
- **Outbox pattern:** drafty oczekujące na wysłanie w osobnej
  tabeli `pending_posts`, WorkManager wysyła gdy pojawi się sieć

### 8.2. Detekcja stanu sieci

- `ConnectivityManager.NetworkCallback` — reactive, nie polling
- LiveData<Boolean> `isOnline` obserwowane przez ViewModel
- UI: delikatny pasek u góry „Brak połączenia" (żółty)

---

## 9. RENDEROWANIE POSTÓW

### 9.1. BBCode → HTML → WebView vs Spannable

**Opcja A: WebView (rekomendowana)**
- Każdy post renderowany w małym WebView
- BBCode → HTML konwersja po stronie serwera (pole `content_html`)
- Lepsza obsługa obrazków, tabel, kodu
- Wada: cięższy, wolniejszy scroll przy wielu postach

**Opcja B: Spannable/TextView**
- BBCode → Spannable konwersja lokalna
- Lżejszy, natywny scroll
- Wada: ograniczone formatowanie, trudne tabele

**Rekomendacja: hybrydowe podejście**
- Proste posty (tekst + cytat + pogrubienie): Spannable/TextView
- Złożone posty (obrazki, kod, tabele): WebView
- Decyzja automatyczna na podstawie analizy content_html

### 9.2. Lazy loading obrazków w postach

- Biblioteka: Glide (lub Coil)
- Obrazki w postach ładowane lazy, placeholder do momentu załadowania
- Tap na obrazek → pełny ekran z zoom (PhotoView)
- Cache obrazków: dyskowy, max 100 MB

---

## 10. STRUKTURA PROJEKTU (PAKIETY)

```
pl.forum.android/
│
├── ForumApplication.java            // Application class, DI init
│
├── di/                              // Dependency Injection (Hilt)
│   ├── AppModule.java               // Retrofit, OkHttp, Room, SharedPrefs
│   ├── RepositoryModule.java
│   └── ViewModelModule.java
│
├── data/
│   ├── remote/
│   │   ├── ForumApiService.java     // Retrofit interface
│   │   ├── AuthApiService.java
│   │   ├── AuthInterceptor.java     // dodaje Bearer token
│   │   ├── TokenRefreshAuthenticator.java  // auto-refresh
│   │   └── dto/                     // Data Transfer Objects (JSON ↔ Java)
│   │       ├── LoginInitResponse.java
│   │       ├── LoginRequest.java
│   │       ├── ThreadDto.java
│   │       ├── PostDto.java
│   │       ├── ApiEnvelope.java     // generyczny wrapper
│   │       └── ...
│   ├── local/
│   │   ├── ForumDatabase.java       // Room database
│   │   ├── dao/
│   │   │   ├── CategoryDao.java
│   │   │   ├── ThreadDao.java
│   │   │   ├── PostDao.java
│   │   │   ├── DraftDao.java
│   │   │   └── NotificationDao.java
│   │   └── entity/
│   │       ├── CategoryEntity.java
│   │       ├── ThreadEntity.java
│   │       ├── PostEntity.java
│   │       ├── DraftEntity.java
│   │       └── NotificationEntity.java
│   └── repository/
│       ├── AuthRepository.java
│       ├── ForumRepository.java
│       ├── DraftRepository.java
│       ├── NotificationRepository.java
│       └── PushRepository.java
│
├── domain/                          // modele biznesowe (opcjonalnie)
│   ├── model/
│   │   ├── Category.java
│   │   ├── Thread.java
│   │   ├── Post.java
│   │   └── User.java
│   └── mapper/
│       ├── ThreadMapper.java        // Entity ↔ Domain ↔ Dto
│       └── PostMapper.java
│
├── crypto/
│   ├── Argon2Helper.java           // Argon2id hashing
│   ├── EmailHasher.java            // SHA256 + maskowanie
│   └── TokenStorage.java           // EncryptedSharedPreferences
│
├── ui/
│   ├── splash/
│   │   └── SplashActivity.java
│   ├── auth/
│   │   ├── AuthActivity.java
│   │   ├── LoginFragment.java
│   │   ├── RegisterFragment.java
│   │   └── AuthViewModel.java
│   ├── main/
│   │   └── MainActivity.java       // host BottomNav + NavController
│   ├── home/
│   │   ├── HomeFragment.java       // lista kategorii
│   │   ├── HomeViewModel.java
│   │   └── CategoryAdapter.java
│   ├── threadlist/
│   │   ├── ThreadListFragment.java
│   │   ├── ThreadListViewModel.java
│   │   └── ThreadAdapter.java
│   ├── postlist/
│   │   ├── PostListFragment.java
│   │   ├── PostListViewModel.java
│   │   ├── PostAdapter.java
│   │   └── QuoteClipboard.java     // singleton kolejki cytatów
│   ├── compose/
│   │   ├── ComposeActivity.java
│   │   ├── ComposeViewModel.java
│   │   ├── ForumEditText.java      // custom widget z BBCode
│   │   ├── FormattingToolbar.java  // [B] [I] [U] [Link] ...
│   │   ├── BbcodeParser.java       // BBCode ↔ Spannable
│   │   └── DraftManager.java       // autozapis
│   ├── notification/
│   │   ├── NotificationsFragment.java
│   │   ├── NotificationsViewModel.java
│   │   └── NotificationAdapter.java
│   ├── pm/
│   │   ├── ConversationsFragment.java
│   │   ├── MessageListFragment.java
│   │   └── PmViewModel.java
│   ├── profile/
│   │   ├── ProfileFragment.java
│   │   ├── UserProfileActivity.java
│   │   └── ProfileViewModel.java
│   ├── search/
│   │   ├── SearchActivity.java
│   │   └── SearchViewModel.java
│   ├── settings/
│   │   └── SettingsActivity.java
│   └── common/
│       ├── BaseFragment.java
│       ├── LoadStateView.java       // loading / error / empty states
│       └── PostRenderer.java        // BBCode→HTML renderowanie posta
│
├── push/
│   ├── ForumFirebaseService.java    // extends FirebaseMessagingService
│   └── NotificationHelper.java      // budowanie i wyświetlanie notyfikacji
│
├── util/
│   ├── NetworkMonitor.java          // ConnectivityManager callback
│   ├── DateFormatter.java           // "3 min temu", "wczoraj 14:32"
│   ├── HtmlSanitizer.java          // XSS protection
│   └── Constants.java
│
└── worker/
    └── PendingPostWorker.java       // WorkManager — wysyłanie offline postów
```

---

## 11. ZALEŻNOŚCI (build.gradle)

```groovy
// Android core
implementation 'androidx.appcompat:appcompat:1.7.0'
implementation 'com.google.android.material:material:1.12.0'
implementation 'androidx.constraintlayout:constraintlayout:2.2.0'
implementation 'androidx.recyclerview:recyclerview:1.4.0'
implementation 'androidx.swiperefreshlayout:swiperefreshlayout:1.2.0'
implementation 'androidx.viewpager2:viewpager2:1.1.0'

// Architecture Components
implementation 'androidx.lifecycle:lifecycle-viewmodel:2.8.0'
implementation 'androidx.lifecycle:lifecycle-livedata:2.8.0'
implementation 'androidx.navigation:navigation-fragment:2.8.0'
implementation 'androidx.navigation:navigation-ui:2.8.0'

// Room (local DB)
implementation 'androidx.room:room-runtime:2.6.0'
annotationProcessor 'androidx.room:room-compiler:2.6.0'

// Network
implementation 'com.squareup.retrofit2:retrofit:2.11.0'
implementation 'com.squareup.retrofit2:converter-gson:2.11.0'
implementation 'com.squareup.okhttp3:okhttp:4.12.0'
implementation 'com.squareup.okhttp3:logging-interceptor:4.12.0'

// Image loading
implementation 'com.github.bumptech.glide:glide:4.16.0'
annotationProcessor 'com.github.bumptech.glide:compiler:4.16.0'

// Argon2
implementation 'org.signal:argon2:13.1'

// Security (EncryptedSharedPreferences)
implementation 'androidx.security:security-crypto:1.1.0-alpha06'

// Firebase (push notifications)
implementation platform('com.google.firebase:firebase-bom:33.0.0')
implementation 'com.google.firebase:firebase-messaging'

// DI (Hilt)
implementation 'com.google.dagger:hilt-android:2.51'
annotationProcessor 'com.google.dagger:hilt-compiler:2.51'

// WorkManager (offline queue)
implementation 'androidx.work:work-runtime:2.10.0'

// Photo viewer (zoom na obrazkach)
implementation 'com.github.chrisbanes:PhotoView:2.3.0'
```

---

## 12. WYGLĄD I MOTYW

### 12.1. Material Design 3

- Motyw: Material You, dynamiczne kolory (Android 12+)
- Fallback dla <Android 12: stały motyw z konfigurowalnymi kolorami
- Dark mode: pełna obsługa (night qualifier)

### 12.2. Typografia postów

- Rozmiar czcionki postów: konfigurowalny suwakiem (12sp–22sp)
- Czcionka: Roboto (system default)
- Kod: Roboto Mono
- Cytaty: italic, mniejszy rozmiar, kolorowa lewa krawędź

### 12.3. Rozmiar elementów dotykowych

- Min. 48dp dla elementów interaktywnych (wytyczne Google)
- Przyciski Cytuj/Odpowiedz: pełna szerokość wiersza,
  48dp wysokości, wyraźne etykiety
- Menu „⋮ Więcej": łatwe do trafienia palcem

---

## 13. OBSŁUGA EDGE CASES

### 13.1. Duże posty
- Posty >10 000 znaków: renderuj z „Pokaż więcej" (collapsed)
- Obrazki >2 MB: skalowane przed wyświetleniem

### 13.2. Rate limiting
- Serwer może zwrócić HTTP 429 → pokazuj odliczanie
  „Możesz napisać ponownie za X sekund"

### 13.3. Sesja wygasła
- Interceptor automatycznie refreshuje token
- Jeśli refresh token też wygasł → dialog „Sesja wygasła"
  z opcją „Zaloguj ponownie" (drafty zachowane!)

### 13.4. Zmiana orientacji ekranu
- ViewModel przeżywa configuration change
- ComposeActivity: treść w ViewModel, nie ginie
- Wymuszona portrait? NIE — obsługuj landscape (rotacja tabletu)

### 13.5. Deep links
- `forum://thread/456/post/789` → otwiera konkretny post
- `forum://pm/123` → otwiera konwersację
- Obsługa z powiadomień push i z linków zewnętrznych

---

## 14. PLAN IMPLEMENTACJI (KOLEJNOŚĆ)

### Faza 1: Fundament (tydzień 1–2)
- [ ] Projekt Android Studio, gradle, zależności
- [ ] Room database + encje + DAO
- [ ] Retrofit setup + AuthApiService + ForumApiService
- [ ] Argon2Helper + EmailHasher + TokenStorage
- [ ] AuthInterceptor + TokenRefreshAuthenticator
- [ ] AuthRepository + AuthViewModel

### Faza 2: Autoryzacja (tydzień 3)
- [ ] SplashActivity
- [ ] LoginFragment + RegisterFragment
- [ ] Pełny flow login/register z Argon2 client-side
- [ ] Obsługa refresh tokenu

### Faza 3: Core forum (tydzień 4–5)
- [ ] MainActivity + BottomNav + Navigation
- [ ] HomeFragment (kategorie)
- [ ] ThreadListFragment + paginacja
- [ ] PostListFragment + renderowanie postów
- [ ] ForumRepository z cache'em

### Faza 4: Edytor i cytaty (tydzień 6–7)
- [ ] ComposeActivity
- [ ] ForumEditText + BbcodeParser + Spannable
- [ ] FormattingToolbar
- [ ] System cytatów (pełny + zaznaczenie + wielokrotne)
- [ ] DraftManager + autozapis
- [ ] PendingPostWorker (offline outbox)

### Faza 5: Powiadomienia i PM (tydzień 8)
- [ ] FCM integration
- [ ] NotificationsFragment
- [ ] ConversationsFragment + MessageListFragment
- [ ] Deep links z powiadomień

### Faza 6: Polish (tydzień 9–10)
- [ ] Wyszukiwanie
- [ ] Profil użytkownika
- [ ] Ustawienia
- [ ] Dark mode
- [ ] Testy (JUnit + Espresso kluczowe ścieżki)
- [ ] ProGuard / R8 rules
- [ ] Optymalizacja wydajności (RecyclerView DiffUtil, prefetch)

---

## 15. UWAGI ARCHITEKTONICZNE

### 15.1. Argon2 na telefonie — wydajność

64 MB RAM na Argon2 to sporo na telefonie. Pomiar na typowych urządzeniach:
- Flagship 2023+ (8GB RAM): ~1-2 sekundy — OK
- Mid-range (4GB RAM): ~2-4 sekundy — akceptowalne z progress bar
- Low-end (2-3GB RAM): ~4-8 sekund lub OOM risk

Rozwiązanie: parametry Argon2 muszą być identyczne w web i Android.
Jeśli web używa 64 MB, Android musi też. Ale warto mieć:
- ProgressBar z komunikatem „Zabezpieczanie logowania..."
- Wykonanie w osobnym wątku (Executor/AsyncTask replacement)
- Ewentualnie: serwer podaje parametry per-user (migracja w przyszłości)

### 15.2. WebView vs natywne renderowanie — decyzja

Dla MVP: **Spannable/TextView** — prostsze, szybsze, lżejsze.
Gdy pojawią się problemy z formatowaniem → migracja do WebView
dla złożonych postów. Nie rób od razu hybrydowego podejścia.

### 15.3. Wersjonowanie API

- Nagłówek `X-Client-Version: android/1.0.0`
- Serwer może wymusić aktualizację: `{ "force_update": true, "min_version": "1.2.0" }`
- Dialog „Wymagana aktualizacja" z linkiem do APK / Play Store

### 15.4. Proguard / obfuskacja

Argon2 helper i klasy JNI: exclude z obfuskacji.
Retrofit DTO: annotuj `@Keep` lub konfiguruj w proguard-rules.pro.
```
