# Ankiety

Ten dokument opisuje dwie już działające warstwy ankiet:
- import i wyświetlanie starych ankiet archiwalnych
- tworzenie oraz głosowanie w nowych ankietach zakładanych razem z wątkiem

## Model danych

W aplikacji są modele:
- `Poll`
- `PollOption`
- `PollVote`

Założenia:
- jedna ankieta na jeden wątek
- ankieta jest pokazywana na początku widoku tematu
- głosy są zapisywane per użytkownik i per opcja

## Import starych ankiet

Stare ankiety ze Sfinii są importowane z SQLite jako ankiety archiwalne.

Cechy:
- są read-only
- wyniki są od razu odkryte
- `PollVote` dla nich pozostaje puste
- import ładuje pytanie, opcje i zliczone wyniki

Powiązanie:
- docelowo importer korzysta z `archive_topic_id`
- to eliminuje zgadywanie po samym tytule wątku

Komenda:

```bash
python manage.py import_polls /home/andrzej/wazne/gitmy/phpbb-archiver/sfiniabb.db
```

Przy pełnym reimporcie ankiety wchodzą też przez `./reimport_sfinia.sh`.

## Tworzenie nowej ankiety

Ankietę tworzy się podczas zakładania nowego wątku, w pełnym edytorze.

Panel ankiety zawiera:
- pytanie
- dynamiczną listę opcji
- przycisk `+ dodaj opcję`
- `Czas trwania: [14] dni`
- checkbox `Można zmienić głos`
- checkbox `Wielokrotny wybór`

UX:
- panel jest zwijany
- główny przycisk formularza pozostaje `Dodaj wątek`
- pod panelem jest osobny submit `Dodaj wątek i ankietę`

## Głosowanie i widoczność wyników

Reguły widoczności wyników są następujące:
- gdy ankieta ma `0` głosów, wszyscy widzą opcje i zera
- gdy ankieta jest otwarta i user jeszcze nie głosował, widzi formularz głosowania, ale nie wyniki
- po własnym głosie user widzi wyniki
- po zamknięciu ankiety wyniki widzą wszyscy
- dla ankiet archiwalnych wyniki są zawsze widoczne

Zmiana głosu:
- jeśli ankieta ma włączone `Można zmienić głos`, user może przepisać swoje głosy
- w przeciwnym razie może zagłosować tylko raz

Wielokrotny wybór:
- jeśli ankieta go dopuszcza, user może zaznaczyć więcej niż jedną odpowiedź
- jeśli nie, wolno wybrać tylko jedną opcję

## Limity opcji

Przygotowane są dwa limity:
- soft limit: `32`
- hard limit: `64`

Założenie:
- normalna ankieta nie powinna mieć absurdalnej liczby odpowiedzi
- ale silnik ma pozwalać na większe ankiety niż tylko kilka opcji

## Ograniczenia edycji

Przyjęta zasada projektowa:
- do pierwszego głosu ankietę można będzie jeszcze edytować
- po pierwszym głosie ankieta ma być merytorycznie nieedytowalna
- ewentualnie pozostanie tylko zmiana czasu zakończenia albo ręczne zamknięcie

Na razie najważniejsze jest to, że:
- tworzenie nowej ankiety działa
- głosowanie działa
- stare ankiety archiwalne są widoczne

## Wyszukiwanie a ankiety

Wyszukiwarka ma już tryb `Wątki` z filtrem:
- `Z ankietami`

To pozwala szybko znaleźć wątki zawierające ankietę bez przeszukiwania treści postów.
