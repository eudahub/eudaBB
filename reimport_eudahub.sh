#!/usr/bin/env bash
# Reimport eudaHub — struktura forów i użytkownicy.
# Słownik morfologiczny jest zachowany (flush_except_morph).
# Aby przebudować morfologię: ./rebuild_morph.sh --forum eudahub
#
# Użycie: ./reimport_eudahub.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVER_DIR="${SCRIPT_DIR}/../phpbb-archiver"
VENV_ACTIVATE="${SCRIPT_DIR}/venv/bin/activate"

EUDAHUB_DB="${ARCHIVER_DIR}/eudaHub1.db"
AVATARS_DIR="${ARCHIVER_DIR}/avatars"

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

echo "==> Czyszczenie bazy (morfologia zostaje)"
python manage.py flush_except_morph --no-input

echo "==> Czyszczenie avatarów"
rm -f media/avatars/*

echo "==> Migracje"
python manage.py migrate

echo "==> Import użytkowników"
python manage.py import_from_sfinia "${EUDAHUB_DB}" --avatars-dir "${AVATARS_DIR}"

echo "==> Import struktury forum"
python manage.py import_forums "${EUDAHUB_DB}" --clear

echo "==> Import postów"
python manage.py import_posts "${EUDAHUB_DB}" --import-db "${EUDAHUB_DB}"

echo "==> Import ankiet i checklist"
python manage.py import_eudabb_features "${EUDAHUB_DB}"

echo "==> Tworzenie konta root"
python manage.py create_root

echo "==> Podsumowanie:"
python manage.py shell -c "from board.models import Topic, Post, Poll, Checklist; print(f'  Topiki:     {Topic.objects.count()}'); print(f'  Posty:      {Post.objects.count()}'); print(f'  Ankiety:    {Poll.objects.count()}'); print(f'  Checklisty: {Checklist.objects.count()}')"
echo "==> Gotowe."
