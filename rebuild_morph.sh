#!/usr/bin/env bash
# Przebudowa słownika morfologicznego z PoliMorf.
# Uruchamiaj po zmianach w build_morph_csv.py lub po pobraniu nowego PoliMorf.
# Nie dotyka danych forum (postów, userów itp.).
#
# Użycie:
#   ./rebuild_morph.sh                  # sfinia (domyślnie)
#   ./rebuild_morph.sh --forum eudahub  # eudahub
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_ACTIVATE="${SCRIPT_DIR}/venv/bin/activate"

FORUM="sfinia"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --forum) FORUM="$2"; shift 2 ;;
    *) echo "Nieznany parametr: $1" >&2; exit 1 ;;
  esac
done

if [[ ! -f "${VENV_ACTIVATE}" ]]; then
  echo "Brak virtualenv: ${VENV_ACTIVATE}" >&2
  exit 1
fi

source "${VENV_ACTIVATE}"
cd "${SCRIPT_DIR}"
export FORUM

echo "==> Forum: ${FORUM}"
echo "==> Budowa CSV z PoliMorf"
python3 build_morph_csv.py

echo "==> Import słownika morfologicznego"
python manage.py import_morph_csv morph_families.csv --clear

echo "==> Import sufiksów morfologicznych"
python manage.py import_morph_suffix morph_suffixes.csv --clear

echo "==> Gotowe."
