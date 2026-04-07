# Aktywność i listy globalne

Ten dokument opisuje widoki aktywności już dostępne w forum oraz granicę między tym, co jest gotowe, a tym, co nadal pozostaje w `TODO.md`.

## `Nowe posty`

Widok `Nowe posty` jest już wdrożony jako globalna lista postów sortowana od najnowszych.

Założenia i obecne zachowanie:
- lista nie jest ograniczona do ostatniego logowania
- działa także dla niezalogowanych
- ma paginację
- każdy wynik pokazuje:
  - forum
  - autora
  - klikalny tytuł wątku prowadzący do konkretnego posta
  - datę
  - snippet z własnego tekstu autora

Snippet:
- jest budowany z treści autora bez bloków specjalnych
- nie pokazuje `[quote]`, `[fquote]`, `[Bible]`, `[AI]`, `[code]`
- jest wycinany wokół trafienia, nie od początku posta

## `Nowe wątki`

Widok `Nowe wątki` jest już wdrożony jako osobna globalna lista tematów.

Każdy wynik pokazuje:
- forum
- tytuł wątku
- autora pierwszego postu
- autora ostatniego postu jako link do ostatniego posta
- liczbę postów w wątku

Tytuł wątku prowadzi na początek tematu.

## Co jeszcze zostało

Te elementy nadal są w `TODO.md`:
- `Nieprzeczytane posty`
- `Twoje posty`
- `Tematy bez odpowiedzi`
- model per-user dla stanu przeczytania i przycisk `Oznacz wszystko jako przeczytane`
