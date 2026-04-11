#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_ACTIVATE="${SCRIPT_DIR}/venv/bin/activate"
if [[ ! -f "${VENV_ACTIVATE}" ]]; then
  echo "Brak virtualenv: ${VENV_ACTIVATE}" >&2
  exit 1
fi
RUNSERVER_BIND="${1:-127.0.0.1:8000}"
source "${VENV_ACTIVATE}"
cd "${SCRIPT_DIR}"
echo "==> Start serwera: ${RUNSERVER_BIND}"
python manage.py runserver "${RUNSERVER_BIND}"
