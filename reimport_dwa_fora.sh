#!/usr/bin/env bash
# Partial reimport — only two subforums:
#   "Rozbieranie irracjonalizmu"  (forum_id 29)
#   "Apologia kontra krytyka teizmu"  (forum_id 5)
#
# Użycie: ./reimport_dwa_fora.sh [BIND]
#   BIND  — adres:port serwera (domyślnie 127.0.0.1:8000)
#
# Aby ograniczyć się do jednego podforum, zakomentuj drugie
# w zmiennej FORUMS poniżej.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVER_DIR="${SCRIPT_DIR}/../phpbb-archiver"
VENV_ACTIVATE="${SCRIPT_DIR}/venv/bin/activate"

ADMIN_DB="${ARCHIVER_DIR}/sfinia_users_admin.db"
REAL_DB="${ARCHIVER_DIR}/sfinia_users_real.db"
IMPORT_DB="${ARCHIVER_DIR}/sfinia_import.db"
ARCHIVE_DB="${ARCHIVER_DIR}/sfiniabb.db"
AVATARS_DIR="${ARCHIVER_DIR}/admin_avatars"

# Dwa podfora — usuń jedno jeśli chcesz tylko jedno
FORUMS="Rozbieranie irracjonalizmu,Apologia kontra krytyka teizmu"

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

echo "==> Import struktury forum (pełna)"
python manage.py import_forums "${ARCHIVE_DB}"

echo "==> Import postów (tylko: ${FORUMS})"
python manage.py import_posts "${ARCHIVE_DB}" --only-forums "${FORUMS}" --import-db "${IMPORT_DB}"

echo "==> Import ankiet (tylko wybrane fora)"
# import_polls nie obsługuje --only-forums, więc importuje wszystkie ankiety
# ale powiązane tematy i tak istnieją tylko dla wybranych forów
python manage.py import_polls "${ARCHIVE_DB}"

echo "==> Tworzenie konta root"
python manage.py create_root

echo "==> Gotowe."
