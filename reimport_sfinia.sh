#!/usr/bin/env bash
# Pełny reimport z sfinia_full.db — wszystkie posty.
# Słownik morfologiczny jest zachowany (flush_except_morph).
# Aby przebudować morfologię: ./rebuild_morph.sh
#
# Użycie: ./reimport_sfinia.sh [BIND]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVER_DIR="${SCRIPT_DIR}/../phpbb-archiver"
VENV_ACTIVATE="${SCRIPT_DIR}/venv/bin/activate"

SFINIA_FULL="${ARCHIVER_DIR}/sfinia_full.db"
AVATARS_DIR="${ARCHIVER_DIR}/avatars"

RUNSERVER_BIND="${1:-127.0.0.1:8000}"

if [[ ! -f "${VENV_ACTIVATE}" ]]; then
  echo "Brak virtualenv: ${VENV_ACTIVATE}" >&2
  exit 1
fi

if [[ ! -f "${SFINIA_FULL}" ]]; then
  echo "Brak pliku: ${SFINIA_FULL}" >&2
  exit 1
fi

source "${VENV_ACTIVATE}"
cd "${SCRIPT_DIR}"

echo "==> Czyszczenie bazy (morfologia zostaje)"
python manage.py flush_except_morph --no-input

echo "==> Czyszczenie avatarów"
rm -f media/avatars/*

echo "==> Migracje"
python manage.py migrate

echo "==> Import użytkowników"
if [[ -d "${AVATARS_DIR}" ]]; then
  python manage.py import_from_sfinia "${SFINIA_FULL}" --avatars-dir "${AVATARS_DIR}"
else
  echo "Uwaga: brak katalogu awatarów ${AVATARS_DIR}, import bez awatarów." >&2
  python manage.py import_from_sfinia "${SFINIA_FULL}"
fi

echo "==> Import spam_class"
python manage.py import_spam_classes "${SFINIA_FULL}"

echo "==> Import struktury forum"
python manage.py import_forums "${SFINIA_FULL}"

echo "==> Import postów (wszystkie)"
python manage.py import_posts "${SFINIA_FULL}" --import-db "${SFINIA_FULL}"

echo "==> Import ankiet"
python manage.py import_polls "${SFINIA_FULL}"

echo "==> Tworzenie konta root"
python manage.py create_root

echo "==> Podsumowanie:"
python manage.py shell -c "from board.models import Topic, Post, Poll, Checklist; print(f'  Topiki:     {Topic.objects.count()}'); print(f'  Posty:      {Post.objects.count()}'); print(f'  Ankiety:    {Poll.objects.count()}'); print(f'  Checklisty: {Checklist.objects.count()}')"
echo "==> Gotowe."
#python manage.py runserver "${RUNSERVER_BIND}"
