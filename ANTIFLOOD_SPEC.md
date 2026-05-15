# Anti-Flood System — Dokumentacja implementacji

## Cel

System anty-flood chroni forum przed spamem i nadmiernym postowaniem, stosując **dynamiczny, progresywny cooldown** zamiast sztywnych limitów (np. "max 30 na godzinę").

Problem ze sztywnymi limitami: limit "30 postów / 5 godzin" pozwala napisać 30 w 30 minut, a potem użytkownik czeka 4.5h na reset. Nasz system wymusza **rosnące odstępy** między kolejnymi postami — im więcej piszesz w krótkim czasie, tym dłużej musisz czekać na następny.

---

## Mechanizm działania

### Sliding window (okno przesuwne)

System patrzy na ostatnie **5 godzin** (konfigurowalny parametr `window_seconds`). Liczy ile postów użytkownik napisał w tym oknie. Gdy stary post "wypada" z okna (minęło 5h od jego publikacji), licznik maleje — cooldown automatycznie się zmniejsza.

### Formuła cooldownu

```
cooldown(n) = floor(A × √(n-1) + B × (n-1))   [minuty]
```

Gdzie:
- `n` = ile postów jest już w oknie (1-based)
- `A = 1.5` — kontroluje łagodny wzrost na początku (√ rośnie wolno)
- `B = 0.18` — kontroluje liniowy wzrost na dalszych pozycjach

Dla `n = 1` (pierwszy post) cooldown = 0 — zawsze wolny.

### Tabela cooldownów (domyślna konfiguracja)

| Post # | Cooldown (min) | Czas skumulowany |
|--------|---------------|-----------------|
| 1→2    | 0             | 0 min           |
| 2→3    | 1             | 1 min           |
| 3→4    | 2             | 3 min           |
| 4→5    | 3             | 6 min           |
| 5→6    | 3             | 9 min           |
| 6→7    | 4             | 13 min          |
| 7→8    | 4             | 17 min          |
| 8→9    | 5             | 22 min          |
| 9→10   | 5             | 27 min          |
| 10→11  | 6             | 33 min          |
| 11→12  | 6             | 39 min          |
| 12→13  | 6             | 45 min          |
| 13→14  | 7             | 52 min          |
| 14→15  | 7             | 59 min          |
| 15→16  | 8             | 1h 07 min       |
| 16→17  | 8             | 1h 15 min       |
| 17→18  | 8             | 1h 23 min       |
| 18→19  | 9             | 1h 32 min       |
| 19→20  | 9             | 1h 41 min       |
| 20→21  | 9             | 1h 50 min       |
| 21→22  | 10            | 2h 00 min       |
| 22→23  | 10            | 2h 10 min       |
| 23→24  | 10            | 2h 20 min       |
| 24→25  | 11            | 2h 31 min       |
| 25→26  | 11            | 2h 42 min       |
| 26→27  | 12            | 2h 54 min       |
| 27→28  | 12            | 3h 06 min       |
| 28→29  | 12            | 3h 18 min       |
| 29→30  | 12            | 3h 30 min       |

**30 postów = minimum 3h 30min** przy ciągłym postowaniu na granicy limitu.

---

## Kalibracja — dane z rzeczywistych użytkowników

System był kalibrowany na dwóch rzeczywistych profilach aktywności:

### Andy72 (użytkownik średnio aktywny)
- 810 postów, 95 dni aktywnych, średnio 8.5 postów/dzień, max 43/dzień
- **Mieści się komfortowo** — jego realne odstępy ZAWSZE przekraczają wymagane cooldowny
- Na żadnym poziomie nie jest ograniczany

### Krystyna (użytkownik bardzo aktywny)
- 1643 posty, 73 dni aktywnych, średnio 22.5 postów/dzień, max 60/dzień
- **Jest ograniczana w zakresie 11–19 postów** — musiałaby zwolnić o 3-13 minut vs. jej dotychczasowe minimum
- Od 20. postu znów się mieści (jej naturalny wzorzec: seria → przerwa → seria)

Szczegóły ograniczenia Krystyny:

| Post # | Wymagany czas (limit) | Jej minimum | Różnica |
|--------|----------------------|-------------|---------|
| 11     | 33 min               | 30 min      | +3 min  |
| 12     | 39 min               | 33 min      | +6 min  |
| 13     | 45 min               | 39 min      | +6 min  |
| 14     | 52 min               | 49 min      | +3 min  |
| 15     | 59 min               | 53 min      | +6 min  |
| 16     | 1h 07 min            | 56 min      | +11 min |
| 17     | 1h 15 min            | 1h 02 min   | +13 min |
| 18     | 1h 23 min            | 1h 18 min   | +5 min  |
| 19     | 1h 32 min            | 1h 28 min   | +4 min  |

Maksymalny narzut to 13 minut (przy 17. poście). To oznacza, że Krystyna mogłaby nadal pisać 30 postów dziennie, ale musiałaby robić minimalnie dłuższe przerwy w środku sesji.

### Gdyby Krystyna miała się zmieścić w 100%

Trzeba by zmienić współczynniki na:
- `A = 1.0, B = 0.10` — wtedy czas na 30 postów spada do ~2h 10min (jej minimum to 4h 18min)
- Ale wtedy spam-boty też byłyby mniej ograniczane

Obecne ustawienie (`A=1.5, B=0.18`) to dobry kompromis: Andy72 nie odczuwa limitów, Krystyna musi lekko zwolnić w szczycie aktywności.

---

## Integracja z Django

### Wymagania

Moduł zakłada istnienie modelu `Post` z polami:
- `author` — ForeignKey do User
- `created_at` — DateTimeField z auto_now_add=True

Import modelu jest w `count_posts_in_window()` i `_time_until_window_slot_frees()`:
```python
from posts.models import Post
```
**Dostosuj ścieżkę importu** do swojej struktury projektu.

### Gdzie sprawdzać (warstwa widoków / API)

Sprawdzenie `check_can_post()` powinno nastąpić **przed zapisem posta**. Typowe miejsca:

#### W widoku API (Django REST Framework):

```python
from antiflood import check_can_post

class PostCreateView(CreateAPIView):
    def create(self, request, *args, **kwargs):
        flood_check = check_can_post(request.user)
        if not flood_check["allowed"]:
            return Response(
                {
                    "error": "rate_limited",
                    "message": flood_check["message"],
                    "wait_seconds": flood_check["wait_seconds"],
                },
                status=429,
            )
        return super().create(request, *args, **kwargs)
```

#### Jako dekorator / mixin (opcjonalnie):

```python
from functools import wraps
from django.http import JsonResponse
from antiflood import check_can_post

def antiflood_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        result = check_can_post(request.user)
        if not result["allowed"]:
            return JsonResponse(
                {
                    "error": "rate_limited",
                    "message": result["message"],
                    "wait_seconds": result["wait_seconds"],
                },
                status=429,
            )
        return view_func(request, *args, **kwargs)
    return wrapper
```

### Konfiguracja w settings.py

```python
# Domyślne wartości — nie trzeba dodawać jeśli OK
ANTIFLOOD_CONFIG = {
    "window_seconds": 5 * 3600,       # 5h sliding window
    "coeff_sqrt": 1.5,                # A coefficient
    "coeff_linear": 0.18,             # B coefficient
    "max_posts_in_window": 30,        # hard cap
    "min_cooldown_seconds": 0,        # floor
    "max_cooldown_seconds": 1800,     # ceiling (30 min)
    "exempt_groups": ["moderators", "admins"],
}
```

### Grupowe wyłączenia

Użytkownicy w grupach z `exempt_groups` (domyślnie: `moderators`, `admins`) omijają limit. Sprawdzenie odbywa się przez `user.groups.filter(name__in=...)`.

---

## Kluczowe cechy systemu

1. **Progresywny cooldown** — nie "tyle na godzinę", ale rosnący odstęp
2. **Samo-regenerujący** — stare posty wypadają z okna, cooldown maleje bez interwencji
3. **Odporny na burst** — ktoś nie może "wystrzelić" 30 postów w 5 minut i czekać do resetu
4. **Łagodny start** — 1-2 szybkie posty to normalna konwersacja, limit tego nie blokuje
5. **Konfigurowalny** — zmiana A/B pozwala dostroić od łagodnego do restrykcyjnego
6. **Hard cap** — nawet przy idealnym timingu, max 30 postów / 5h

---

## Struktura pliku antiflood.py

| Funkcja | Opis |
|---------|------|
| `get_config()` | Łączy domyślny config z `settings.ANTIFLOOD_CONFIG` |
| `compute_cooldown_minutes(n)` | Czysta formuła: n → minuty cooldownu |
| `compute_cooldown_seconds(n)` | j.w. ale w sekundach |
| `count_posts_in_window(user)` | Queryset: ile postów w oknie 5h |
| `check_can_post(user)` | **Główna funkcja** — zwraca dict z allowed/wait/message |
| `is_user_exempt(user, config)` | Sprawdza grupy exempt |
| `_clamp_cooldown()` | Min/max bounds na cooldown |
| `_time_until_window_slot_frees()` | Przy hard cap — kiedy najstarszy post wypadnie |
| `_build_result()` | Konstruktor ustandaryzowanego dicta wyniku |
| `_format_wait_message()` | Formatuje czas oczekiwania po polsku |
| `print_cooldown_table()` | Debug — drukuje pełną tabelę cooldownów |

---

## Odpowiedź z check_can_post()

```python
{
    "allowed": True/False,        # czy może postować
    "wait_seconds": 0,            # ile sekund czekać (0 = może)
    "posts_in_window": 12,        # ile postów w oknie
    "cooldown_seconds": 360,      # aktualny wymagany cooldown
    "message": "Poczekaj 3 min przed kolejnym postem."
}
```

HTTP status dla odmowy: **429 Too Many Requests**.

---

## Strona kliencka (Android / przeglądarka)

Gdy API zwraca 429:
1. Wyświetl `message` użytkownikowi
2. Użyj `wait_seconds` do odliczania (countdown timer)
3. Zablokuj przycisk "Wyślij" do wygaśnięcia timera
4. Opcjonalnie: pokaż pasek postępu

Nagłówek odpowiedzi (opcjonalnie dodaj w widoku):
```
Retry-After: <wait_seconds>
```
