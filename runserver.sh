#!/usr/bin/env bash
# Użycie:
#   ./runserver.sh                          # sfinia, 127.0.0.1:8000
#   ./runserver.sh --forum eudahub          # eudahub, 127.0.0.1:8000
#   ./runserver.sh --forum sfinia 0.0.0.0:8001
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_ACTIVATE="${SCRIPT_DIR}/venv/bin/activate"
if [[ ! -f "${VENV_ACTIVATE}" ]]; then
  echo "Brak virtualenv: ${VENV_ACTIVATE}" >&2
  exit 1
fi

FORUM="sfinia"
BIND="127.0.0.1:8000"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --forum) FORUM="$2"; shift 2 ;;
    *)       BIND="$1";  shift   ;;
  esac
done

source "${VENV_ACTIVATE}"
cd "${SCRIPT_DIR}"
export FORUM

PORT="${BIND##*:}"
LISTENING=$(lsof -ti:"${PORT}" -sTCP:LISTEN 2>/dev/null)
if [[ -n "${LISTENING}" ]]; then
  echo "==> Zwalnianie portu ${PORT} (PID: ${LISTENING})"
  kill -9 ${LISTENING}
fi

echo "==> Forum: ${FORUM}  |  Serwer: ${BIND}"
python manage.py runserver "${BIND}"
