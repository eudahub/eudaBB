from pathlib import Path
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("SECRET_KEY", default="dev-secret-key-change-in-production")
DEBUG = config("DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1").split(",")

# TEST_MODE: skip email sending, activate ghost accounts immediately after email check.
# Also shows "Przełącz" button for all logged-in users.
# Set TEST_MODE=true in .env for development, never on production.
TEST_MODE = config("TEST_MODE", default=False, cast=bool)

# Optional banner shown on every page (empty = no banner).
# Example: SITE_NOTICE="WERSJA TESTOWA — baza danych zostanie zresetowana, wszelkie zmiany będą utracone."
SITE_NOTICE = config("SITE_NOTICE", default="")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "board",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "board.middleware.MaintenanceModeMiddleware",
    "board.middleware.SessionTrackingMiddleware",
    "board.middleware.TorBlockMiddleware",
]

# YouTube embeds require an HTTP Referer for playback. Django's stricter
# same-origin policy breaks cross-origin iframe loads and causes YouTube
# player error 153, so allow origin-only referers on cross-origin requests.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "board.context_processors.test_mode",
                "board.context_processors.pm_unread_count",
                "board.context_processors.user_session_info",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# FORUM selects which PostgreSQL database to use.
# Set via environment variable FORUM=sfinia|eudahub, or pass --forum to runserver.sh.
# DB_NAME_SFINIA / DB_NAME_EUDAHUB override the default names per forum.
FORUM = config("FORUM", default="sfinia")
_FORUM_DB_DEFAULTS = {
    "sfinia":  config("DB_NAME_SFINIA",  default="forum_db"),
    "eudahub": config("DB_NAME_EUDAHUB", default="eudahub_db"),
}
_DB_NAME = _FORUM_DB_DEFAULTS.get(FORUM) or config("DB_NAME", default="forum_db")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _DB_NAME,
        "USER": config("DB_USER", default="postgres"),
        "PASSWORD": config("DB_PASSWORD", default=""),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="5432"),
    }
}

AUTH_USER_MODEL = "board.User"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",  # fallback for old hashes
]

AUTHENTICATION_BACKENDS = ["board.backends.ClientArgon2Backend"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "pl"
TIME_ZONE = "Europe/Warsaw"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# Posts per page
POSTS_PER_PAGE = 20
TOPICS_PER_PAGE = 30

# Post / PM content size limits (characters)
POST_CONTENT_HARD_MAX_CHARS = config("POST_CONTENT_HARD_MAX_CHARS", default=65_535, cast=int)
POST_CONTENT_SOFT_MAX_CHARS = config("POST_CONTENT_SOFT_MAX_CHARS", default=20_000, cast=int)
PM_CONTENT_HARD_MAX_CHARS = config("PM_CONTENT_HARD_MAX_CHARS", default=65_535, cast=int)
PM_CONTENT_SOFT_MAX_CHARS = config("PM_CONTENT_SOFT_MAX_CHARS", default=20_000, cast=int)

# Contact form — jedyny plaintext email w systemie (email admina)
CONTACT_FORM_RECIPIENT = config("CONTACT_FORM_RECIPIENT", default="")
CONTACT_FORM_RATE_LIMIT = 3   # max wiadomości z jednego IP na godzinę

# TOR exit node blocking — refreshed hourly by: python manage.py refresh_tor_list
# Add to cron: 0 * * * * /path/to/venv/bin/python /path/to/manage.py refresh_tor_list
TOR_BLOCK_ENABLED = config("TOR_BLOCK_ENABLED", default=True, cast=bool)

# IP retention for law enforcement — how long author_ip is kept on posts.
# Normal posts: 30 days. Posts flagged dangerous by moderator: 90 days.
# After expiry the ip is nulled by: manage.py purge_expired_ips
IP_RETAIN_NORMAL_DAYS    = config("IP_RETAIN_NORMAL_DAYS",    default=30, cast=int)
IP_RETAIN_DANGEROUS_DAYS = config("IP_RETAIN_DANGEROUS_DAYS", default=90, cast=int)

# Snapshot directory for pg_dump backups (snapshot_create / snapshot_restore)
SNAPSHOT_DIR = config("SNAPSHOT_DIR", default=str(BASE_DIR / "snapshots"))

# Private Messages limits
PM_INBOX_LIMIT  = config("PM_INBOX_LIMIT",  default=300, cast=int)
PM_SENT_LIMIT   = config("PM_SENT_LIMIT",   default=300, cast=int)
PM_OUTBOX_LIMIT = config("PM_OUTBOX_LIMIT", default=50,  cast=int)  # anti-spam: max in-flight

# Poll option count limits
POLL_OPTIONS_HARD_MAX = config("POLL_OPTIONS_HARD_MAX", default=100, cast=int)
POLL_OPTIONS_SOFT_MAX = config("POLL_OPTIONS_SOFT_MAX", default=50, cast=int)

# Search results snippet length (characters around the best hit)
SEARCH_SNIPPET_CHARS = config("SEARCH_SNIPPET_CHARS", default=800, cast=int)

# Email / SendGrid
EMAIL_BACKEND   = config("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
EMAIL_HOST      = config("EMAIL_HOST",    default="smtp.sendgrid.net")
EMAIL_PORT      = config("EMAIL_PORT",    default=587, cast=int)
EMAIL_USE_TLS   = config("EMAIL_USE_TLS", default=True, cast=bool)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="apikey")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL  = config("DEFAULT_FROM_EMAIL",  default="noreply@eudahub.pl")
EMAIL_FROM_NAME     = config("EMAIL_FROM_NAME",     default="Forum eudaHub")
