#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVER_DIR="${SCRIPT_DIR}/../phpbb-archiver"
VENV_ACTIVATE="${SCRIPT_DIR}/venv/bin/activate"

ADMIN_DB="${ARCHIVER_DIR}/sfinia_users_admin.db"
REAL_DB="${ARCHIVER_DIR}/sfinia_users_real.db"
IMPORT_DB="${ARCHIVER_DIR}/sfinia_import.db"
ARCHIVE_DB="${ARCHIVER_DIR}/sfiniabb.db"
AVATARS_DIR="${ARCHIVER_DIR}/admin_avatars"

RUNSERVER_BIND="${1:-127.0.0.1:8000}"

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo "Brak pliku: $path" >&2
    exit 1
  fi
}

if [[ ! -f "${VENV_ACTIVATE}" ]]; then
  echo "Brak virtualenv: ${VENV_ACTIVATE}" >&2
  exit 1
fi

require_file "${ADMIN_DB}"
require_file "${REAL_DB}"
require_file "${ARCHIVE_DB}"

# Safe even if the venv is already active.
source "${VENV_ACTIVATE}"

cd "${SCRIPT_DIR}"

echo "==> Flush bazy"
python manage.py flush --no-input

echo "==> Migracje"
python manage.py migrate

echo "==> Budowa bazy importowej userów"
python manage.py build_import_db \
  "${ADMIN_DB}" \
  "${REAL_DB}" \
  "${IMPORT_DB}"

echo "==> Import userów"
if [[ -d "${AVATARS_DIR}" ]]; then
  python manage.py import_from_sfinia "${IMPORT_DB}" --avatars-dir "${AVATARS_DIR}"
else
  echo "Uwaga: brak katalogu awatarów ${AVATARS_DIR}, import bez awatarów." >&2
  python manage.py import_from_sfinia "${IMPORT_DB}"
fi

echo "==> Import spam_class"
python manage.py import_spam_classes "${REAL_DB}"

echo "==> Import struktury forum"
python manage.py import_forums "${ARCHIVE_DB}"

echo "==> Import postów"
python manage.py import_posts "${ARCHIVE_DB}"

echo "==> Import ankiet"
python manage.py import_polls "${ARCHIVE_DB}"

echo "==> Tworzenie konta root"
python manage.py create_root

echo "==> Start serwera: ${RUNSERVER_BIND}"
#python manage.py runserver "${RUNSERVER_BIND}"
