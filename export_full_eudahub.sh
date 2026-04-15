SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_ACTIVATE="${SCRIPT_DIR}/venv/bin/activate"

if [[ ! -f "${VENV_ACTIVATE}" ]]; then
  echo "Brak virtualenv: ${VENV_ACTIVATE}" >&2
  exit 1
fi

source "${VENV_ACTIVATE}"
FORUM=eudahub python manage.py export_full ../phpbb-archiver/eudaHub1.db
