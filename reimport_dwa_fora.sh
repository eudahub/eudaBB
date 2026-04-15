#!/usr/bin/env bash
# Partial reimport — only two subforums:
#   "Rozbieranie irracjonalizmu"  (forum_id 29)
#   "Apologia kontra krytyka teizmu"  (forum_id 5)
#
# Słownik morfologiczny jest zachowany (flush_except_morph).
# Aby przebudować morfologię: ./rebuild_morph.sh
#
# Aby ograniczyć się do jednego podforum, zakomentuj drugie
# w zmiennej FORUMS poniżej.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVER_DIR="${SCRIPT_DIR}/../phpbb-archiver"
VENV_ACTIVATE="${SCRIPT_DIR}/venv/bin/activate"

SFINIA_FULL="${ARCHIVER_DIR}/sfinia_full.db"

# Dwa podfora — usuń jedno jeśli chcesz tylko jedno
FORUMS="Rozbieranie irracjonalizmu,Apologia kontra krytyka teizmu"

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

echo "==> Import postów (tylko: ${FORUMS})"
python manage.py import_posts "${SFINIA_FULL}" --only-forums "${FORUMS}" --import-db "${SFINIA_FULL}"

echo "==> Import ankiet (tylko wybrane fora)"
# import_polls nie obsługuje --only-forums, więc importuje wszystkie ankiety
# ale powiązane tematy i tak istnieją tylko dla wybranych forów
python manage.py import_polls "${SFINIA_FULL}"

echo "==> Tworzenie konta root"
python manage.py create_root

echo "==> Podsumowanie:"
python manage.py shell -c "from board.models import Topic, Post, Poll, Checklist; print(f'  Topiki:     {Topic.objects.count()}'); print(f'  Posty:      {Post.objects.count()}'); print(f'  Ankiety:    {Poll.objects.count()}'); print(f'  Checklisty: {Checklist.objects.count()}')"
echo "==> Gotowe."
