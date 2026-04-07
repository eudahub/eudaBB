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

# Apply migrations:
python manage.py migrate
```

### 4. Root account and start

```bash
source venv/bin/activate
python manage.py create_root
python manage.py set_root_password
python manage.py runserver
```

Forum available at: http://127.0.0.1:8000/
Admin panel: http://127.0.0.1:8000/admin/

If you want the dev server to be reachable from other devices on your LAN:

```bash
python manage.py runserver 0.0.0.0:8000
```

You can also choose a different port:

```bash
python manage.py runserver 8080
```

## First steps after launch

1. Log in to `/admin/`
2. Create a **Section** (e.g. "General")
3. Create a **Forum** assigned to that section (e.g. "Discussion")
4. Go to the home page — the forum should be visible
5. Register a regular user via `/register/`

## Full Sfinia re-import

Complete re-import sequence:

```bash
source venv/bin/activate

# 1. Clear the database contents (keeps table structure)
python manage.py flush --no-input

# 2. Apply all migrations
python manage.py migrate

# 3. Build the user import database
python manage.py build_import_db \
  /home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_users_admin.db \
  /home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_users_real.db \
  /home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_import.db

# 4. Import users and spam classes
python manage.py import_from_sfinia /home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_import.db
python manage.py import_spam_classes /home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_users_real.db

# 5. Import forum structure
python manage.py import_forums /home/andrzej/wazne/gitmy/phpbb-archiver/sfiniabb.db

# 6. Import posts
python manage.py import_posts /home/andrzej/wazne/gitmy/phpbb-archiver/sfiniabb.db

# 7. Recreate the root account after flush
python manage.py create_root
python manage.py set_root_password
```

Notes:
- `flush` also removes the `root` account.
- `build_import_db` takes emails from `sfinia_users_admin.db` and user/profile data from `sfinia_users_real.db`.
- spam classification is imported separately from `sfinia_users_real.db`.
- after the full import you can start `runserver` immediately.

## Importing users from Sfinia

Current import flow for users with plaintext emails:

```bash
python manage.py build_import_db /path/to/sfinia_users_admin.db /path/to/sfinia_users_real.db /path/to/sfinia_import.db
python manage.py import_from_sfinia /path/to/sfinia_import.db
python manage.py import_spam_classes /path/to/sfinia_users_real.db
python manage.py apply_username_aliases --db /path/to/sfinia_users_real.db
```

Notes:
- `build_import_db` now expects plaintext `email` in the generated `users` table; the old `email_hash/email_mask` import format is legacy-only.
- `build_import_db` also copies the `username_aliases` table from `sfinia_users_real.db` into `sfinia_import.db`.
- the current `apply_username_aliases` command still reads aliases directly from `sfinia_users_real.db`; copying them into `sfinia_import.db` prepares the next import steps.
- post import also builds the `forum_quote_refs` quote index (citing post, source post, nesting depth).
- root can later rename a user from `/root/config/`; the rename uses `forum_quote_refs`, so it does not have to scan the whole posts table.
- for an already imported Django database, you can rebuild the quote index with:

```bash
python manage.py rebuild_quote_refs
```

## Rebuilds and indexes

### Quote index

For an already imported database:

```bash
python manage.py migrate
python manage.py rebuild_quote_refs
```

### Search index

First on one test forum:

```bash
python manage.py build_search_index --forum-title "Filozofia"
python manage.py inspect_search_index --forum-title "Filozofia" --limit 20
```

Full rebuild for all forums:

```bash
python manage.py build_search_index
```

The command also prints the rebuild time at the end.

### Search table size

```bash
./venv/bin/python manage.py shell -c "from django.db import connection; c=connection.cursor(); c.execute(\"SELECT pg_size_pretty(pg_total_relation_size('forum_post_search')), pg_total_relation_size('forum_post_search')\"); print(c.fetchone())"
```

Breakdown into table data and indexes:

```bash
./venv/bin/python manage.py shell -c "from django.db import connection; c=connection.cursor(); c.execute(\"SELECT pg_size_pretty(pg_relation_size('forum_post_search')), pg_size_pretty(pg_indexes_size('forum_post_search')), pg_size_pretty(pg_total_relation_size('forum_post_search'))\"); print(c.fetchone())"
```

## Maintenance commands

### Delete all password reset codes

Via SQL:

```bash
psql -U andrzej forum_db -c "DELETE FROM forum_password_reset_codes;"
```

Or from the web UI:
- root has a dedicated button in `/root/config/`

### Quick manual check after startup

Example direct thread URL:

```text
http://127.0.0.1:8000/topic/7784/?page=1
```

## Feature documentation

Detailed feature docs now live under `docs/`:

- [Activity and global lists](docs/aktywnosc.md)
- [Quoting and full editor](docs/cytowanie-i-edytor.md)
- [Search](docs/wyszukiwarka.md)
- [Polls](docs/ankiety.md)

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
- [x] BBCode → HTML
- [x] Thread and post pagination
- [x] Cached counters (posts, threads, last post)
- [x] Admin panel
- [x] Sticky / Announcement (via admin)
- [x] Thread locking (via admin)
- [x] Quote workflow with selection and `quote` validation
- [x] Full reply editor and full new-topic editor
- [x] `spoiler`
- [x] Post and topic search
- [x] Polls: archived import, creation, voting
- [x] `New posts` and `New topics`
- [x] Post likes

## Troubleshooting

**`fe_sendauth: no password supplied`**
Django is connecting via TCP. Set `DB_HOST=` (empty) in `.env` to use a Unix socket instead — no password required.

**`Peer authentication failed for user "postgres"`**
Set `DB_USER` in `.env` to your system username (the one you used with `createuser`), not `postgres`.

## To be extended (next steps)

- [ ] Post editing
- [ ] User profile
- [ ] Moderation (deleting/moving threads)
- [ ] Notifications
- [ ] Avatars
