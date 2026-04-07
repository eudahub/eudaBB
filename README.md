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

## Quoting in the full editor

- In thread view each post has a `Quote` button; without a selection it uses the whole post, and with a selection it tries to recover the closest exact BBCode fragment.
- In the full editor, the `quote` toolbar button does not insert an empty `[quote][/quote]`.
- Its role is mainly to teach the workflow: a normal forum quote should come from choosing an existing post, not from manually typing quote tags.
- Clicking `quote` switches the page into quote-picking mode using the recent-post list below the editor:
  - the editor is temporarily hidden,
  - the recent-post list is expanded,
  - a large instruction banner with `OK` / `Cancel` is shown.
- In practice, quoting through the per-post `Quote` buttons is the most convenient path:
  - select a fragment from one of the posts and press `OK`,
  - or press the per-post `Quote` button in the recent-post list.
- The `quote` toolbar button exists mainly so users discover this workflow; the actual quote is built from post actions, not from an empty manual tag.
- The resulting quote is appended to the end of the editor without leaving the page, so multiple quotes can be added one after another.
- When a selection includes a nested quote, the system tries to preserve the nested `[quote ... post_id=...]`; if the selection cuts through it, it may build a shortened version using `(...)`.
- Regular `quote` requires `post_id` and is validated on submit. `fquote` remains for outside sources.

## Search — stop-word candidates

Based on `content_user` analysis from `sfiniabb.db`:
- the main metric is `df` (`document frequency`), meaning in how many posts a token appears
- analysis normalization:
  - case-insensitive
  - diacritic-insensitive
  - no stemming
- stop-words should apply only to plain `AND` tokens, not to quoted phrases

### Safe starter list

- `nie`
- `to`
- `w`
- `i`
- `sie`
- `ze`
- `na`
- `z`
- `a`
- `do`
- `o`
- `ale`

### Test list

- `co`
- `jak`
- `tak`
- `bo`
- `tym`
- `tego`
- `ma`
- `czy`
- `od`
- `po`
- `ja`
- `sa`
- `za`
- `dla`
- `juz`
- `sobie`
- `byc`
- `jesli`
- `tu`

### Keep for now

- `jest`
- `tylko`
- `moze`
- `mozna`
- `bardzo`
- `albo`

Notes:
- artifacts such as `b`, `http`, `www`, `pl` should not become stop-words; they should be handled by better tokenization / cleanup
- if the user searches for a single stop-word such as `do`, the system may skip it and show a short notice
- if the user searches for a phrase such as `"do rzeczy"`, then `do` must stay inside the phrase

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
- [ ] Quotes and `post_id` integrity
  - A post should reference its author via `User.id` / FK, not a textual username.
  - That keeps username changes fast as long as the new name does not collide after normalization.
  - Renaming a user must still update author names embedded in quotes and nested quotes.
  - Working assumption: if a quote contains `post_id=...`, the quoted username is expected to be correct and can be updated.
  - The same applies to user deletion: quotes and nested quotes pointing at that user's posts must be handled.
- [ ] Quote validation with `post_id`
  - A user must not be able to submit an arbitrary invalid `post_id` in a quote.
  - `post_id` must point to an existing post.
  - The quoted text must match the content of the post referenced by `post_id`.
- [ ] Post editing
- [ ] User profile
- [ ] Moderation (deleting/moving threads)
- [ ] Polls
- [ ] Search
- [ ] Notifications
- [ ] Avatars
