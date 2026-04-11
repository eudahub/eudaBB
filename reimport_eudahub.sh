#!/usr/bin/env bash
# Reimport eudaHub — na razie tylko struktura forów (bez postów i użytkowników).
# Słownik morfologiczny jest zachowany (flush_except_morph).
# Aby przebudować morfologię: ./rebuild_morph.sh --forum eudahub
#
# Użycie: ./reimport_eudahub.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVER_DIR="${SCRIPT_DIR}/../phpbb-archiver"
VENV_ACTIVATE="${SCRIPT_DIR}/venv/bin/activate"

EUDAHUB_DB="${ARCHIVER_DIR}/eudaHub.db"

if [[ ! -f "${VENV_ACTIVATE}" ]]; then
  echo "Brak virtualenv: ${VENV_ACTIVATE}" >&2
  exit 1
fi

if [[ ! -f "${EUDAHUB_DB}" ]]; then
  echo "Brak pliku: ${EUDAHUB_DB}" >&2
  exit 1
fi

source "${VENV_ACTIVATE}"
cd "${SCRIPT_DIR}"
export FORUM=eudahub

echo "==> Forum: eudahub  |  DB: ${EUDAHUB_DB}"

echo "==> Migracje"
python manage.py migrate

echo "==> Czyszczenie bazy (morfologia zostaje)"
python manage.py flush_except_morph --no-input

echo "==> Import struktury forum"
python manage.py import_forums "${EUDAHUB_DB}" --clear

echo "==> Tworzenie konta root"
python manage.py create_root

echo "==> Gotowe."
