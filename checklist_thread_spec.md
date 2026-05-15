# eudaBB — Wątek-Checklista: Specyfikacja funkcjonalności

## 1. Koncept ogólny

Wątek-Checklista to specjalny typ wątku (obok zwykłego i ankietowego), w którym zamiast klasycznej dyskusji głównym elementem jest **interaktywna lista zadań / zgłoszeń**. Każda pozycja checklisty ma statusy, upvote'y, priorytet twórcy i rozwijane komentarze.

**Główny use case:** Beta testy — zbieranie bugów, feature requestów i feedbacku od społeczności, z przejrzystym śledzeniem postępu.

---

## 2. Role i uprawnienia

| Akcja | Twórca wątku | Moderator | Zarejestrowany user |
|-------|:---:|:---:|:---:|
| Utworzenie wątku-checklisty | ✅ | ✅ | ❌ (lub konfigurowalnie) |
| Dodanie pozycji (propozycja) | ✅ natychmiast | ✅ natychmiast | ✅ wymaga zatwierdzenia |
| Zatwierdzenie propozycji usera | ✅ | ✅ | ❌ |
| Odrzucenie propozycji usera | ✅ | ✅ | ❌ |
| Zmiana statusu pozycji | ✅ | ✅ | ❌ |
| Ustawienie priorytetu | ✅ | ✅ | ❌ |
| Upvote pozycji | ✅ | ✅ | ✅ (1 głos / pozycję) |
| Komentowanie pozycji | ✅ | ✅ | ✅ |
| Edycja kategorii checklisty | ✅ | ✅ | ❌ |
| Usunięcie pozycji | ✅ | ✅ | ❌ |
| Edycja własnej propozycji (przed zatwierdzeniem) | — | — | ✅ |

---

## 3. Cykl życia pozycji checklisty

### 3.1 Statusy pozycji

```
  [Propozycja usera]
        │
        ▼
   ┌─────────┐     odrzucenie      ┌───────────┐
   │ PENDING  │ ──────────────────► │ REJECTED  │
   │(do zatw.)│                     └───────────┘
   └────┬─────┘
        │ zatwierdzenie
        ▼
   ┌─────────┐
   │   NEW    │◄──────────────────────────────┐
   │  (nowe)  │                               │
   └────┬─────┘                               │
        │                                     │ cofnięcie
        ▼                                     │
   ┌──────────┐                          ┌────┴──────┐
   │IN_PROGRESS│ ───────────────────────►│   DONE    │
   │(w trakcie)│                         │ (zrobione)│
   └────┬──────┘                         └───────────┘
        │
        ├──────────────────────────► ┌───────────┐
        │                           │ WONT_FIX  │
        │                           │(nie będzie)│
        │                           └───────────┘
        │
        └──────────────────────────► ┌───────────┐
                                     │ DUPLICATE │
                                     │ (duplikat)│
                                     └───────────┘
```

Twórca/mod dodaje pozycje bezpośrednio w statusie `NEW` (pomija `PENDING`).

### 3.2 Statusy — definicje

| Status | Kto ustawia | Znaczenie |
|--------|-------------|-----------|
| `PENDING` | automatycznie | Propozycja usera czeka na zatwierdzenie |
| `REJECTED` | twórca / mod | Propozycja odrzucona (widoczna wyszarzona z powodem) |
| `NEW` | twórca / mod | Przyjęte, jeszcze nie rozpoczęte |
| `IN_PROGRESS` | twórca / mod | W trakcie realizacji |
| `DONE` | twórca / mod | Zrealizowane ✅ |
| `WONT_FIX` | twórca / mod | Świadoma decyzja: nie będzie realizowane |
| `DUPLICATE` | twórca / mod | Duplikat innej pozycji (z linkiem do oryginału) |

---

## 4. Upvote + priorytet twórcy (podwójny sygnał)

### 4.1 Upvote (warstwa społeczności)

- Każdy zalogowany user — **1 głos na pozycję** (toggle: klik dodaje, ponowny klik cofa).
- Licznik upvote'ów widoczny przy każdej pozycji.
- Domyślne sortowanie listy: **po liczbie upvote'ów** (malejąco).
- Własne propozycje: user automatycznie upvote'uje swoją propozycję.

### 4.2 Priorytet twórcy (warstwa decyzyjna)

Twórca wątku / mod może niezależnie oznaczyć priorytet:

| Priorytet | Etykieta | Kolor (sugestia) |
|-----------|----------|-------------------|
| `CRITICAL` | 🔴 Krytyczne | czerwony |
| `IMPORTANT` | 🟠 Ważne | pomarańczowy |
| `MINOR` | 🔵 Drobnostka | niebieski |
| `PLANNED` | 🟢 Planowane | zielony |
| *(brak)* | — | bez etykiety |

Priorytet jest **niezależny** od upvote'ów. User widzi oba sygnały obok siebie.

### 4.3 Sortowanie i filtrowanie

Dostępne tryby sortowania:
- **Po upvote'ach** (domyślne) — głos społeczności
- **Po priorytecie** — wizja twórcy
- **Po dacie dodania** — chronologicznie
- **Po statusie** — grupowanie: aktywne → w trakcie → zrobione → zamknięte

Filtrowanie:
- Po statusie (multi-select: np. pokaż tylko NEW + IN_PROGRESS)
- Po kategorii
- Po priorytecie
- „Moje zgłoszenia" — pozycje dodane przeze mnie
- „Moje głosy" — pozycje które upvote'owałem

---

## 5. Kategorie (tagi)

### 5.1 Definiowanie

- Twórca wątku definiuje **zestaw kategorii** przy tworzeniu checklisty.
- Kategorie można dodawać/edytować/usuwać później (twórca + mod).
- Przykładowe zestawy:
  - Beta testy: `Bug`, `Feature request`, `UX/UI`, `Wydajność`, `Dokumentacja`
  - Ogólne: `Do zrobienia`, `Pomysł`, `Pytanie`

### 5.2 Przypisywanie

- User proponujący pozycję **wybiera kategorię** z listy (wymagane).
- Twórca/mod może zmienić kategorię po zatwierdzeniu.
- Jedna pozycja = jedna kategoria (prostota).

### 5.3 Wizualizacja

Kategorie wyświetlane jako kolorowe badge'e przy pozycji. Kolory definiuje twórca wątku (paleta do wyboru lub własny hex).

---

## 6. System komentarzy pod pozycjami

### 6.1 Mechanika

- Każda pozycja checklisty ma **rozwijalną sekcję komentarzy**.
- Domyślnie widoczny jest tylko **licznik komentarzy**: `💬 7`
- Kliknięcie rozwija listę komentarzy inline (bez przeładowania strony).
- Komentarze to krótkie wpisy (limit np. 1000 znaków) — nie pełne posty forumowe.

### 6.2 Cechy komentarzy

- Chronologiczne (najstarsze na górze).
- Możliwość edycji własnego komentarza (w oknie czasowym, np. 15 min).
- Możliwość usunięcia przez autora, twórcę wątku lub moda.
- Brak zagnieżdżania (flat, nie drzewo) — prostota.
- Brak cytowania — komentarze są krótkie, kontekst jest jasny (dotyczą jednej pozycji).

### 6.3 Interakcja z wątkiem

Komentarze pod pozycjami checklisty są **niezależne** od postów w wątku. Wątek nadal może mieć klasyczne posty (proza, dłuższe dyskusje), ale operacyjny feedback trafia do komentarzy przy konkretnej pozycji.

---

## 7. Zatwierdzanie propozycji (moderacja zgłoszeń)

### 7.1 Flow zgłaszania

1. User klika „Dodaj pozycję".
2. Wypełnia formularz: **tytuł** (wymagane), **opis** (opcjonalne), **kategoria** (wymagane z listy).
3. Pozycja trafia do kolejki ze statusem `PENDING`.
4. Twórca/mod widzi **badge z liczbą oczekujących** propozycji.
5. Twórca/mod może:
   - **Zatwierdzić** → status zmienia się na `NEW`, pozycja pojawia się na liście.
   - **Odrzucić z powodem** → status `REJECTED`, user widzi powód.
   - **Edytować i zatwierdzić** → poprawić tytuł/opis/kategorię przed zatwierdzeniem.

### 7.2 Widoczność propozycji oczekujących

- **Autor propozycji**: widzi swoją propozycję ze statusem „Oczekuje na zatwierdzenie".
- **Inni userzy**: nie widzą propozycji oczekujących (unikamy spamu / duplikatów).
- **Twórca/mod**: widzi pełną kolejkę z akcjami.

### 7.3 Edycja propozycji przed zatwierdzeniem

User może edytować swoją propozycję dopóki jest w statusie `PENDING`. Po zatwierdzeniu — tylko twórca/mod.

---

## 8. Struktura danych (model Django)

### 8.1 Modele

```
ChecklistThread (rozszerzenie Thread)
├── categories: JSON lub osobna tabela ChecklistCategory
├── allow_user_proposals: bool (domyślnie True)
└── default_sort: enum (upvotes | priority | date | status)

ChecklistItem
├── thread: FK → ChecklistThread
├── author: FK → User
├── title: str (max 200)
├── description: text (opcjonalne, max 2000)
├── category: FK → ChecklistCategory
├── status: enum (PENDING, REJECTED, NEW, IN_PROGRESS, DONE, WONT_FIX, DUPLICATE)
├── priority: enum (CRITICAL, IMPORTANT, MINOR, PLANNED, null)
├── duplicate_of: FK → ChecklistItem (nullable, self-reference)
├── rejection_reason: str (nullable)
├── upvote_count: int (denormalizacja dla wydajności)
├── comment_count: int (denormalizacja)
├── created_at: datetime
├── updated_at: datetime
├── status_changed_at: datetime
└── status_changed_by: FK → User

ChecklistUpvote
├── item: FK → ChecklistItem
├── user: FK → User
└── created_at: datetime
    (unique_together: item + user)

ChecklistComment
├── item: FK → ChecklistItem
├── author: FK → User
├── content: text (max 1000)
├── created_at: datetime
└── updated_at: datetime

ChecklistCategory
├── thread: FK → ChecklistThread
├── name: str (max 50)
├── color: str (hex, max 7)
└── order: int
```

### 8.2 Indeksy

- `ChecklistItem`: indeks na `(thread, status)`, `(thread, upvote_count)`, `(thread, created_at)`
- `ChecklistUpvote`: unique na `(item, user)`
- `ChecklistComment`: indeks na `(item, created_at)`

---

## 9. API Endpoints (REST)

```
# Wątek-checklista
POST   /api/threads/                          # type: "checklist"
GET    /api/threads/{id}/checklist/            # lista pozycji (z filtrami i sortowaniem)

# Pozycje
POST   /api/threads/{id}/checklist/items/      # dodaj pozycję (user: PENDING, twórca: NEW)
PATCH  /api/checklist/items/{id}/              # edytuj pozycję
DELETE /api/checklist/items/{id}/              # usuń pozycję

# Statusy i priorytet
PATCH  /api/checklist/items/{id}/status/       # zmień status (twórca/mod)
PATCH  /api/checklist/items/{id}/priority/     # zmień priorytet (twórca/mod)

# Moderacja propozycji
GET    /api/threads/{id}/checklist/pending/     # kolejka propozycji (twórca/mod)
POST   /api/checklist/items/{id}/approve/      # zatwierdź
POST   /api/checklist/items/{id}/reject/       # odrzuć (z powodem)

# Upvote
POST   /api/checklist/items/{id}/upvote/       # toggle upvote
GET    /api/checklist/items/{id}/voters/        # kto głosował (opcjonalne)

# Komentarze
GET    /api/checklist/items/{id}/comments/      # lista komentarzy
POST   /api/checklist/items/{id}/comments/      # dodaj komentarz
PATCH  /api/checklist/comments/{id}/           # edytuj (autor, w oknie czasowym)
DELETE /api/checklist/comments/{id}/           # usuń (autor/twórca/mod)

# Kategorie
GET    /api/threads/{id}/checklist/categories/  # lista kategorii
POST   /api/threads/{id}/checklist/categories/  # dodaj kategorię (twórca/mod)
PATCH  /api/checklist/categories/{id}/         # edytuj
DELETE /api/checklist/categories/{id}/         # usuń
```

---

## 10. Widok UI — elementy interfejsu

### 10.1 Nagłówek checklisty

```
╔═══════════════════════════════════════════════════════╗
║  📋 Beta testy eudaBB v0.1 — Zgłoszenia              ║
║                                                       ║
║  Sortuj: [Upvote'y ▼] [Priorytet] [Data] [Status]    ║
║  Filtruj: [Bug ✓] [Feature ✓] [UX ✓]  Status: [Wszystkie ▼] ║
║                                                       ║
║  12 pozycji · 3 zrobione · 2 oczekują na zatwierdzenie║
║                                         [+ Dodaj pozycję] ║
╚═══════════════════════════════════════════════════════╝
```

### 10.2 Pojedyncza pozycja (zwinięta)

```
┌───────────────────────────────────────────────────┐
│ ▲  │ ☐ Brak walidacji emaila przy rejestracji     │
│ 14 │ 🏷️ Bug  🔴 Krytyczne   📅 2 dni temu         │
│    │ 💬 7 komentarzy                  Status: NEW  │
└───────────────────────────────────────────────────┘
```

### 10.3 Pojedyncza pozycja (rozwinięte komentarze)

```
┌───────────────────────────────────────────────────┐
│ ▲  │ ☑ Brak walidacji emaila przy rejestracji     │
│ 14 │ 🏷️ Bug  🔴 Krytyczne                         │
│    │ Opis: Mogę wpisać "asdf" jako email...       │
│    │                           Status: DONE ✅     │
│    ├───────────────────────────────────────────────│
│    │ 💬 Komentarze (7)                             │
│    │                                               │
│    │  👤 jankowalski · 2 dni temu                  │
│    │  U mnie to samo, emaile bez @ przechodzą.    │
│    │                                               │
│    │  👤 admin · 1 dzień temu                      │
│    │  Naprawione w commit abc123, wychodzi w v0.2. │
│    │                                               │
│    │  [Pokaż więcej...]                            │
│    │  [Napisz komentarz...]                        │
└───────────────────────────────────────────────────┘
```

---

## 11. Edge cases i reguły biznesowe

1. **Duplikat** — przy oznaczeniu jako `DUPLICATE` wymagane wskazanie oryginalnej pozycji. Upvote'y z duplikatu **nie** są przenoszone (prostota), ale widoczny jest link „Zobacz oryginał".

2. **Usunięcie kategorii** — pozycje z tą kategorią przechodzą do specjalnej kategorii `Inne` (tworzonej automatycznie, nieedytowalnej).

3. **Zamknięcie checklisty** — twórca może zamknąć wątek-checklistę, co blokuje: nowe propozycje, nowe upvote'y, nowe komentarze. Istniejące dane pozostają widoczne.

4. **Limity antyspamowe**:
   - Max propozycji per user per wątek: **10 oczekujących** jednocześnie.
   - Max komentarzy per user per pozycja: **20**.
   - Cooldown między propozycjami: **60 sekund**.

5. **Cofanie statusu** — status `DONE` można cofnąć do `NEW` lub `IN_PROGRESS` (regresja buga). Historia zmian statusu jest logowana.

6. **Zmiana statusu REJECTED → NEW** — możliwa (twórca zmienił zdanie po dodatkowych komentarzach).

---

## 12. Przyszłe rozszerzenia (poza MVP)

- **Powiadomienia** — alert o zmianie statusu zgłoszenia usera.
- **Załączniki** — screenshoty przy pozycji.
- **Progress bar** — wizualny pasek postępu w liście wątków.
- **Eksport** — CSV/JSON z listą pozycji i statusami.
- **Przypisanie osoby** (assignee) — kto realizuje daną pozycję.
- **Integracja z Git** — automatyczne zamykanie pozycji po commicie z `fixes #ID`.
- **Merge pozycji** — łączenie duplikatów z sumowaniem upvote'ów.
- **Szablony kategorii** — gotowe zestawy kategorii do wyboru przy tworzeniu wątku.
