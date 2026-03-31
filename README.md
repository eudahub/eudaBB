# Django Forum — minimal starter version

Classic phpBB-style forum: sections → forums → threads → posts, BBCode, pagination.

## Stack

- Python 3.12+ / Django 5.x
- PostgreSQL
- Pico CSS (CDN, no node_modules)
- `bbcode` library for rendering

## Project structure

```
forum/
├── config/
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── board/
│   ├── models.py       # User, Section, Forum, Topic, Post
│   ├── views.py        # views + stats helpers
│   ├── urls.py
│   ├── forms.py
│   ├── bbcode.py       # BBCode renderer wrapper
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

## Running locally

### 0. Install PostgreSQL (Ubuntu/Debian)

```bash
sudo apt install postgresql postgresql-contrib
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

### 1. Virtual environment and dependencies

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment configuration

```bash
cp .env.example .env
# Edit .env — set PostgreSQL credentials and SECRET_KEY
```

Minimal `.env` for development:

```
DEBUG=True
SECRET_KEY=something-long-and-random
DB_NAME=forum_db
DB_USER=postgres
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=5432
```

### 3. Database

```bash
# Create a PostgreSQL role for your system user (first time only):
sudo -u postgres createuser --superuser $USER

# Create database:
createdb forum_db

# Generate and apply migrations:
python manage.py makemigrations board
python manage.py migrate

# Superuser (admin panel):
python manage.py createsuperuser
```

### 4. Start

```bash
python manage.py runserver
```

Forum available at: http://127.0.0.1:8000/
Admin panel: http://127.0.0.1:8000/admin/

## First steps after launch

1. Log in to `/admin/`
2. Create a **Section** (e.g. "General")
3. Create a **Forum** assigned to that section (e.g. "Discussion")
4. Go to the home page — the forum should be visible
5. Register a regular user via `/register/`

## Production (nginx + gunicorn)

```bash
pip install gunicorn
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3
```

nginx as a reverse proxy + serving `/media/` and `/static/` (after `collectstatic`).

```bash
python manage.py collectstatic
```

## What is done (v0.1)

- [x] Hierarchy: Section → Forum → Thread → Post
- [x] Registration and login
- [x] Creating threads and replies
- [x] BBCode → HTML (render cache in `content_html`)
- [x] Thread and post pagination
- [x] Cached counters (posts, threads, last post)
- [x] Admin panel
- [x] Sticky / Announcement (via admin)
- [x] Thread locking (via admin)

## Troubleshooting

**`fe_sendauth: no password supplied`**
Django is connecting via TCP. Set `DB_HOST=` (empty) in `.env` to use a Unix socket instead — no password required.

**`Peer authentication failed for user "postgres"`**
Set `DB_USER` in `.env` to your system username (the one you used with `createuser`), not `postgres`.

**`Dependency on app with no migrations: board`**
Run `python manage.py makemigrations board` before `migrate`.

## To be extended (next steps)

- [ ] Quotes (`[quote]`)
- [ ] Post editing
- [ ] User profile
- [ ] Moderation (deleting/moving threads)
- [ ] Polls
- [ ] Search
- [ ] Notifications
- [ ] Avatars
