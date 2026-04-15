#!/usr/bin/env bash
# Partial reimport — pełna struktura i userzy, ale tylko pierwsze 40 000 postów.
# Słownik morfologiczny jest zachowany (flush_except_morph).
# Aby przebudować morfologię: ./rebuild_morph.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVER_DIR="${SCRIPT_DIR}/../phpbb-archiver"
VENV_ACTIVATE="${SCRIPT_DIR}/venv/bin/activate"

SFINIA_FULL="${ARCHIVER_DIR}/sfinia_full.db"

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

echo "==> Migracje"
python manage.py migrate

echo "==> Import użytkowników"
python manage.py import_from_sfinia "${SFINIA_FULL}"

echo "==> Import spam_class"
python manage.py import_spam_classes "${SFINIA_FULL}"

echo "==> Import struktury forum (pełna)"
python manage.py import_forums "${SFINIA_FULL}"

echo "==> Import postów (pierwsze 40 000)"
python manage.py import_posts "${SFINIA_FULL}" --first 40000 --import-db "${SFINIA_FULL}"

echo "==> Import ankiet"
python manage.py import_polls "${SFINIA_FULL}"

echo "==> Tworzenie konta root"
python manage.py create_root

echo "==> Podsumowanie:"
python manage.py shell -c "from board.models import Topic, Post, Poll, Checklist; print(f'  Topiki:     {Topic.objects.count()}'); print(f'  Posty:      {Post.objects.count()}'); print(f'  Ankiety:    {Poll.objects.count()}'); print(f'  Checklisty: {Checklist.objects.count()}')"
echo "==> Gotowe."
