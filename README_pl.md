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
# Utwórz bazę w PostgreSQL:
createdb forum_db

# Migracje:
python manage.py migrate

# Superuser (admin panelu):
python manage.py createsuperuser
```

### 4. Start

```bash
python manage.py runserver
```

Forum dostępne na: http://127.0.0.1:8000/
Panel admina: http://127.0.0.1:8000/admin/

## Pierwsze kroki po uruchomieniu

1. Zaloguj się do `/admin/`
2. Utwórz **Section** (np. "Ogólne")
3. Utwórz **Forum** przypisane do tej sekcji (np. "Rozmowy")
4. Wejdź na stronę główną — powinno być widoczne forum
5. Zarejestruj zwykłego użytkownika przez `/register/`

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
- [x] BBCode → HTML (render cache w `content_html`)
- [x] Paginacja wątków i postów
- [x] Cached countery (posty, wątki, ostatni post)
- [x] Panel admina
- [x] Sticky / Announcement (przez admina)
- [x] Blokowanie wątków (przez admina)

## Do rozbudowania (kolejne kroki)

- [ ] Cytowania (`[quote]`)
- [ ] Edycja postów
- [ ] Profil użytkownika
- [ ] Moderacja (usuwanie/przenoszenie wątków)
- [ ] Ankiety (Poll)
- [ ] Wyszukiwanie
- [ ] Powiadomienia
- [ ] Avatary
