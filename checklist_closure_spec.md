# eudaBB — Zamknięcie checklisty i odłączenie od użytkowników

## 1. Kontekst

W fazie beta testerzy zakładają konta, zgłaszają pozycje, głosują i komentują. Po zakończeniu beta testów baza może zostać odtworzona z archiwum wersji produkcyjnej — konta beta testerów znikają. Checklista powinna **przetrwać** jako archiwalna tablica kanban, czytelna bez powiązań z konkretnymi użytkownikami.

---

## 2. Dwa etapy: zamknięcie → archiwizacja

### 2.1 Zamknięcie (soft close)

Twórca wątku zamyka checklistę — blokuje interakcje, ale dane wciąż powiązane z userami.

**Co się blokuje:**
- Nowe propozycje pozycji
- Nowe komentarze
- Nowe upvote'y / cofanie upvote'ów
- Zmiana statusu i priorytetu (chyba że twórca/mod odblokuje pojedynczą pozycję)
- Edycja istniejących komentarzy

**Co pozostaje:**
- Pełny widok checklisty z komentarzami
- Sortowanie i filtrowanie
- Dane autorów widoczne (linki do profili działają)

Zamknięcie jest **odwracalne** — twórca może ponownie otworzyć checklistę.

### 2.2 Archiwizacja (detach users)

Nieodwracalna operacja przygotowująca checklistę do życia bez bazy użytkowników. Uruchamiana ręcznie przez admina/twórcę **przed** migracją do produkcji.

---

## 3. Proces archiwizacji — krok po kroku

### 3.1 Warunek wstępny

- Checklista musi być **zamknięta** (etap 2.1).
- System wymaga potwierdzenia: „Ta operacja jest nieodwracalna. Dane autorstwa zostaną trwale zastąpione pseudonimami."

### 3.2 Co robi archiwizacja

#### A) Zastąpienie autorów pseudonimami

Każdy unikalny user, który brał udział w checkliście, otrzymuje **deterministyczny pseudonim** w obrębie tego wątku:

```
jankowalski    → Tester #1
annaprogramuje → Tester #2
bugfinder99    → Tester #3
(twórca wątku) → Organizator
(moderator)    → Moderator #1, Moderator #2, ...
```

Mapowanie `user_id → pseudonim` jest **jednorazowe i niszczone** po operacji — nie da się odtworzyć, kto był kim.

Pseudonimy zastępują FK do User we wszystkich powiązanych tabelach:

| Model | Pole | Przed | Po |
|-------|------|-------|-----|
| `ChecklistItem` | `author` | FK → User | NULL |
| `ChecklistItem` | `author_label` | — | „Tester #3" |
| `ChecklistItem` | `status_changed_by` | FK → User | NULL |
| `ChecklistItem` | `status_changed_by_label` | — | „Organizator" |
| `ChecklistComment` | `author` | FK → User | NULL |
| `ChecklistComment` | `author_label` | — | „Tester #1" |
| `ChecklistUpvote` | `user` | FK → User | — (wiersz usuwany) |

#### B) Upvote'y — zachowanie licznika, usunięcie głosów

- Pole `upvote_count` w `ChecklistItem` **zostaje** (zachowujemy informację „14 głosów").
- Tabela `ChecklistUpvote` — wiersze powiązane z tą checklistą **usuwane** (nie da się powiązać głosów z nieistniejącymi userami, toggle i tak zablokowany).

#### C) Komentarze — zachowanie treści z pseudonimami

Komentarze pozostają z treścią i pseudonimem autora. Są częścią merytorycznej wartości checklisty (opis buga, workaround, potwierdzenie naprawy).

#### D) Wątek-checklista — oznaczenie jako archiwalny

```python
ChecklistThread:
    is_archived: bool = True     # nieodwracalne
    archived_at: datetime
    archived_by_label: str       # np. "Organizator"
```

### 3.3 Nowe pole `author_label`

Dodajemy pole `author_label` (CharField, max 30, nullable) do modeli `ChecklistItem` i `ChecklistComment`. W normalnym trybie jest puste — UI czyta z FK `author`. Po archiwizacji FK jest NULL, a UI czyta z `author_label`.

```python
# Logika wyświetlania w serializerze / szablonie:
def get_display_author(obj):
    if obj.author is not None:
        return obj.author.username
    return obj.author_label or "Anonim"
```

---

## 4. Widok archiwalnej checklisty

Po archiwizacji checklista wygląda prawie tak samo, z różnicami:

- Baner u góry: `📦 Archiwum beta testów · Zamknięta 12.04.2026`
- Autorzy wyświetlani jako pseudonimy (nie-klikalne, bez linku do profilu).
- Upvote'y widoczne jako liczba, ale przycisk „▲" nieaktywny.
- Komentarze widoczne, ale bez możliwości dodawania.
- Filtry i sortowanie wciąż działają.

```
┌───────────────────────────────────────────────────┐
│    │ ☑ Brak walidacji emaila przy rejestracji     │
│ 14 │ 🏷️ Bug  🔴 Krytyczne                         │
│    │ Zgłosił: Tester #3 · 12.03.2026              │
│    │                           Status: DONE ✅     │
│    ├───────────────────────────────────────────────│
│    │ 💬 Komentarze (7)                             │
│    │                                               │
│    │  🧑 Tester #5 · 13.03.2026                   │
│    │  U mnie to samo, emaile bez @ przechodzą.    │
│    │                                               │
│    │  🧑 Organizator · 14.03.2026                  │
│    │  Naprawione, wychodzi w v0.2.                 │
└───────────────────────────────────────────────────┘
```

---

## 5. Migracja bazy do produkcji

Scenariusz: przywracasz bazę produkcyjną z archiwum, konta beta testerów znikają.

### 5.1 Jeśli archiwizacja BYŁA zrobiona

Bezpieczne — checklista nie ma FK do User, działa samodzielnie. Po przywróceniu bazy produkcyjnej checklista jest widoczna jako archiwum.

### 5.2 Jeśli archiwizacja NIE BYŁA zrobiona

FK do User wskazują na nieistniejące rekordy → kaskadowe usunięcie lub integrity error (zależy od `on_delete`).

**Zabezpieczenie**: ustawienie `on_delete=SET_NULL` na polach FK w modelach checklisty:

```python
class ChecklistItem(models.Model):
    author = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True
    )
    status_changed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True
    )

class ChecklistComment(models.Model):
    author = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True
    )
```

W tym wariancie pseudonimy nie będą wypełnione — UI pokaże „Anonim". Dane merytoryczne (tytuły, opisy, komentarze, statusy, upvote count) przetrwają.

---

## 6. Komenda administracyjna

```bash
python manage.py archive_checklist <thread_id> [--dry-run] [--confirm]
```

| Flaga | Działanie |
|-------|-----------|
| `--dry-run` | Pokazuje co zostanie zrobione, bez zmian w bazie |
| `--confirm` | Pomija interaktywne pytanie o potwierdzenie |

Logi operacji:
```
Archiving checklist thread #42 "Beta testy eudaBB v0.1"
  - 5 unique users → pseudonymized
  - 23 items: author FK nullified, labels assigned
  - 87 comments: author FK nullified, labels assigned
  - 156 upvote records: deleted (counters preserved)
  - Thread marked as archived
Done. This operation is irreversible.
```

---

## 7. Podsumowanie przepływu

```
Beta trwa          Koniec beta       Przed migracją      Po migracji
     │                  │                  │                   │
     ▼                  ▼                  ▼                   ▼
  [Otwarta]  ──►  [Zamknięta]  ──►  [Zarchiwizowana]  ──►  [Działa
  pełna              brak nowych       FK → NULL              bez
  interakcja]        interakcji        pseudonimy             userów]
                     odwracalne        NIEODWRACALNE
```
