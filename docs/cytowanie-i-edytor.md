# Cytowanie i pełny edytor

Ten dokument opisuje aktualny workflow cytowania, pełny edytor odpowiedzi i pełny edytor nowego wątku.

## Cytowanie w widoku wątku

Przy każdym poście jest przycisk `Cytuj`.

Zasady:
- bez zaznaczenia bierze cały post
- z zaznaczeniem próbuje odtworzyć możliwie dokładny fragment BBCode
- jeśli zaznaczenie obejmuje zagnieżdżony cytat, system stara się zachować `[quote="..." post_id=...]`
- jeśli zaznaczenie przecina cytat w połowie, system może zbudować skróconą wersję z `(...)`

Dodatkowe zabezpieczenie UX:
- gdy nic nie jest zaznaczone, aktywne są wszystkie przyciski `Cytuj`
- gdy zaznaczony jest fragment jednego posta, aktywny pozostaje tylko przycisk `Cytuj` tego posta

## Cytowanie w pełnym edytorze odpowiedzi

Przycisk `quote` w toolbarze nie wstawia pustego `[quote][/quote]`.

Jego rola:
- uruchomić tryb wyboru cytatu z listy postów
- podpowiedzieć użytkownikowi, że zwykłe cytowanie forumowe robi się przez wybór posta, a nie przez ręczne pisanie tagów

Po kliknięciu `quote`:
- edytor jest chwilowo ukrywany
- lista postów pod edytorem dostaje więcej miejsca
- pojawia się baner z instrukcją i przyciskami `OK` / `Anuluj`

W tym trybie działają:
- selekcja fragmentu i `OK`
- zwykłe przyciski `Cytuj` przy postach
- paginacja listy postów wątku
- filtrowanie po tekście w obrębie bieżącego wątku
- filtrowanie po autorze z `combo` zbudowanego tylko z użytkowników tego tematu

## `Przypięte`

W pickerze cytowania jest też osobna sekcja `Przypięte`.

Zasady:
- obejmuje globalnie tematy typu `Przyklejony` i `Ogłoszenie`
- lista jest grupowana i sortowana po forum
- wpisy są zwijane do jednej linii
- po rozwinięciu widać treść pierwszego posta i przycisk `Cytuj`

To ma służyć głównie do cytowania regulaminów, FAQ, definicji i innych treści kanonicznych.

## Walidacja zwykłego `quote`

Zwykły `[quote]` z `post_id` jest walidowany przy zapisie.

Reguły:
- `post_id` jest obowiązkowe
- `post_id` musi wskazywać istniejący post
- autor w tagu musi zgadzać się z autorem źródłowego posta
- treść cytatu musi pasować do źródła
- dopuszczone są skróty typu `...`, `…`, `/.../`, `(...)`

Indeks cytowań:
- forum utrzymuje tabelę `forum_quote_refs`
- trzyma ona metadane cytatu, głębokość zagnieżdżenia i `ellipsis_count`
- może być przebudowana komendą `python manage.py rebuild_quote_refs`

## `fquote`

`fquote` pozostaje tagiem do cytatów zewnętrznych.

Zwykłe cytowanie forumowe ma być promowane przez workflow `Cytuj` + `quote`, a nie przez ręczne wpisywanie tagów.

## `spoiler`

Tag `spoiler` jest już obsługiwany.

Warianty:
- `[spoiler]treść[/spoiler]`
- `[spoiler=Etykieta]treść[/spoiler]`

Render:
- używa HTML `<details><summary>...</summary>...</details>`
- domyślnie pokazuje tylko nagłówek
- po kliknięciu odkrywa treść

Stan:
- nie jest pamiętany po ponownym wejściu na post
- to celowe zachowanie
- w tym `spoiler` różni się od ankiety

## Pełny edytor nowego wątku

Tworzenie nowego wątku używa już pełnego edytora, a nie prostego pola tekstowego.

Obecne elementy:
- toolbar BBCode
- liczniki linii i znaków
- `Podgląd`
- `Walidacja`
- `quote`
- sekcja `Przypięte`
- przycisk `ankieta`

Różnica względem odpowiedzi w wątku:
- nie ma listy ostatnich postów z bieżącego tematu
- `quote` działa przez globalne `Przypięte`
