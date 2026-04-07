# Forum Django — minimalna wersja startowa

Klasyczne forum w stylu phpBB: sekcje → fora → wątki → posty, BBCode, paginacja.

## Stack

- Python 3.12+ / Django 5.x
- PostgreSQL
- Pico CSS (CDN, bez node_modules)
- biblioteka `bbcode` do renderowania

## Struktura projektu

```
forum/
├── config/
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── board/
│   ├── models.py       # User, Section, Forum, Topic, Post
│   ├── views.py        # widoki + helpery statystyk
│   ├── urls.py
│   ├── forms.py
│   ├── bbcode.py       # wrapper renderera BBCode
│   ├── admin.py
│   └── migrations/
├── templates/
│   ├── base.html
│   ├── board/
│   │   ├── index.html
│   │   ├── forum_detail.html
│   │   ├── topic_detail.html
│   │   ├── new_topic.html
│   │   ├── reply.html
│   │   └── _pagination.html
│   └── registration/
│       ├── login.html
│       └── register.html
├── manage.py
├── requirements.txt
└── .env.example
```

## Uruchomienie (lokalnie)

### 0. Instalacja PostgreSQL (Ubuntu/Debian)

```bash
sudo apt install postgresql postgresql-contrib
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

### 1. Środowisko wirtualne i zależności

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Konfiguracja środowiska

```bash
cp .env.example .env
# Edytuj .env — ustaw dane do PostgreSQL i SECRET_KEY
```

Minimalne `.env` dla devu:

```
DEBUG=True
SECRET_KEY=cokolwiek-dlugie-i-losowe
DB_NAME=forum_db
DB_USER=postgres
DB_PASSWORD=twoje_haslo
DB_HOST=localhost
DB_PORT=5432
```

### 3. Baza danych

```bash
# Utwórz rolę PostgreSQL dla swojego użytkownika systemowego (tylko za pierwszym razem):
sudo -u postgres createuser --superuser $USER

# Utwórz bazę:
createdb forum_db

# Zastosuj migracje:
python manage.py migrate
```

### 4. Root i start

```bash
source venv/bin/activate
python manage.py create_root
python manage.py set_root_password
python manage.py runserver
```

Forum dostępne na: http://127.0.0.1:8000/
Panel admina: http://127.0.0.1:8000/admin/

Jeśli chcesz udostępnić forum innym urządzeniom w sieci:

```bash
python manage.py runserver 0.0.0.0:8000
```

Możesz też podać inny port:

```bash
python manage.py runserver 8080
```

## Pierwsze kroki po uruchomieniu

1. Zaloguj się do `/admin/`
2. Utwórz **Section** (np. "Ogólne")
3. Utwórz **Forum** przypisane do tej sekcji (np. "Rozmowy")
4. Wejdź na stronę główną — powinno być widoczne forum
5. Zarejestruj zwykłego użytkownika przez `/register/`

## Pełny reimport bazy Sfinia

Pełna sekwencja reimportu:

```bash
source venv/bin/activate

# 1. Wyczyść bazę danych (zachowuje strukturę tabel)
python manage.py flush --no-input

# 2. Zastosuj wszystkie migracje
python manage.py migrate

# 3. Zbuduj bazę użytkowników do importu
python manage.py build_import_db \
  /home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_users_admin.db \
  /home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_users_real.db \
  /home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_import.db

# 4. Importuj użytkowników i klasy spamu
python manage.py import_from_sfinia /home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_import.db
python manage.py import_spam_classes /home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_users_real.db

# 5. Importuj strukturę forum
python manage.py import_forums /home/andrzej/wazne/gitmy/phpbb-archiver/sfiniabb.db

# 6. Importuj posty
python manage.py import_posts /home/andrzej/wazne/gitmy/phpbb-archiver/sfiniabb.db

# 7. Odtwórz konto root po flush
python manage.py create_root
python manage.py set_root_password
```

Uwagi:
- `flush` usuwa też konto `root`.
- `build_import_db` bierze emaile z `sfinia_users_admin.db`, a resztę użytkowników z `sfinia_users_real.db`.
- klasyfikacja spamu jest importowana osobno z `sfinia_users_real.db`.
- po pełnym imporcie można od razu uruchomić `runserver`.

## Import użytkowników z Sfinia

Aktualna ścieżka importu użytkowników z plaintext emailami:

```bash
python manage.py build_import_db /path/to/sfinia_users_admin.db /path/to/sfinia_users_real.db /path/to/sfinia_import.db
python manage.py import_from_sfinia /path/to/sfinia_import.db
python manage.py import_spam_classes /path/to/sfinia_users_real.db
python manage.py apply_username_aliases --db /path/to/sfinia_users_real.db
```

Uwagi:
- `build_import_db` buduje tabelę `users` z kolumną `email` w plaintext; stary format `email_hash/email_mask` nie jest już używany do importu.
- `build_import_db` kopiuje też tabelę `username_aliases` z `sfinia_users_real.db` do `sfinia_import.db`.
- obecna komenda `apply_username_aliases` nadal czyta aliasy bezpośrednio z `sfinia_users_real.db`; kopiowanie do `sfinia_import.db` przygotowuje kolejne kroki importu.
- import postów buduje też indeks cytowań `forum_quote_refs` (post cytujący, post źródłowy, głębokość zagnieżdżenia).
- root może potem ręcznie zmienić nick użytkownika w `/root/config/`; rename korzysta z `forum_quote_refs`, więc nie musi skanować całej tabeli postów.
- dla już istniejącej bazy Django można odbudować indeks cytowań komendą:

```bash
python manage.py rebuild_quote_refs
```

## Indeksy i odbudowa po imporcie

### Indeks cytowań

Dla już istniejącej bazy:

```bash
python manage.py migrate
python manage.py rebuild_quote_refs
```

### Indeks wyszukiwania

Najpierw dla jednego forum testowego:

```bash
python manage.py build_search_index --forum-title "Filozofia"
python manage.py inspect_search_index --forum-title "Filozofia" --limit 20
```

Pełny rebuild dla wszystkich forów:

```bash
python manage.py build_search_index
```

Komenda wypisuje też czas wykonania na końcu.

### Rozmiar tabeli wyszukiwania

```bash
./venv/bin/python manage.py shell -c "from django.db import connection; c=connection.cursor(); c.execute(\"SELECT pg_size_pretty(pg_total_relation_size('forum_post_search')), pg_total_relation_size('forum_post_search')\"); print(c.fetchone())"
```

Rozbicie na dane i indeksy:

```bash
./venv/bin/python manage.py shell -c "from django.db import connection; c=connection.cursor(); c.execute(\"SELECT pg_size_pretty(pg_relation_size('forum_post_search')), pg_size_pretty(pg_indexes_size('forum_post_search')), pg_size_pretty(pg_total_relation_size('forum_post_search'))\"); print(c.fetchone())"
```

## Komendy serwisowe

### Usuń wszystkie kody resetowania hasła

Przez SQL:

```bash
psql -U andrzej forum_db -c "DELETE FROM forum_password_reset_codes;"
```

Albo z WWW:
- root ma osobny przycisk w `/root/config/`

### Uruchomienie testowe / szybki podgląd konkretnego wątku

Po starcie serwera możesz wejść np. na:

```text
http://127.0.0.1:8000/topic/7784/?page=1
```

## Dokumentacja funkcji

Szczegóły wdrożonych funkcji są wyniesione do osobnych plików:

- [Aktywność i listy globalne](docs/aktywnosc.md)
- [Cytowanie i pełny edytor](docs/cytowanie-i-edytor.md)
- [Wyszukiwarka](docs/wyszukiwarka.md)
- [Ankiety](docs/ankiety.md)

## Produkcja (nginx + gunicorn)

```bash
pip install gunicorn
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3
```

nginx jako reverse proxy + serwowanie `/media/` i `/static/` (po `collectstatic`).

```bash
python manage.py collectstatic
```

## Co jest gotowe (v0.1)

- [x] Hierarchia: Sekcja → Forum → Wątek → Post
- [x] Rejestracja i logowanie
- [x] Tworzenie wątków i odpowiedzi
- [x] BBCode → HTML
- [x] Paginacja wątków i postów
- [x] Cached countery (posty, wątki, ostatni post)
- [x] Panel admina
- [x] Sticky / Announcement (przez admina)
- [x] Blokowanie wątków (przez admina)
- [x] Cytowanie z selekcją i walidacja `quote`
- [x] Pełny edytor odpowiedzi i nowego wątku
- [x] `spoiler`
- [x] Wyszukiwarka postów i wątków
- [x] Ankiety: import archiwalny, tworzenie i głosowanie
- [x] `Nowe posty` i `Nowe wątki`
- [x] Polubienia postów

## Rozwiązywanie problemów

**`fe_sendauth: no password supplied`**
Django łączy się przez TCP. Ustaw `DB_HOST=` (puste) w `.env`, żeby używać Unix socketa — bez hasła.

**`Peer authentication failed for user "postgres"`**
Ustaw `DB_USER` w `.env` na swoją nazwę użytkownika systemowego (tę, której użyłeś przy `createuser`), nie `postgres`.

## Do rozbudowania (kolejne kroki)

- [ ] Edycja postów
- [ ] Profil użytkownika
- [ ] Moderacja (usuwanie/przenoszenie wątków)
- [ ] Powiadomienia
- [ ] Avatary
