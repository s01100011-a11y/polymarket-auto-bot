#!/data/data/com.termux/files/usr/bin/bash
set -u
set -o pipefail

ROOT="${HOME}/polymarket-auto-bot"
VENV="${ROOT}/.venv"
LOG_DIR="${HOME}/.config/polymarket-termux"
LOG_FILE="${LOG_DIR}/executor-supervisor.log"
RESTART_DELAY="${EXECUTOR_RESTART_DELAY_SECONDS:-5}"

mkdir -p "${LOG_DIR}"
chmod 700 "${LOG_DIR}"

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock || true
fi

cleanup() {
  if command -v termux-wake-unlock >/dev/null 2>&1; then
    termux-wake-unlock || true
  fi
}
trap cleanup EXIT INT TERM

cd "${ROOT}" || exit 1
if [ ! -x "${VENV}/bin/python" ]; then
  echo "Missing virtualenv at ${VENV}" | tee -a "${LOG_FILE}"
  exit 1
fi

echo "$(date -Is) supervisor starting" | tee -a "${LOG_FILE}"

while true; do
  echo "$(date -Is) launching termux_executor_v2.py" | tee -a "${LOG_FILE}"
  "${VENV}/bin/python" scripts/termux_executor_v2.py 2>&1 | tee -a "${LOG_FILE}"
  code=${PIPESTATUS[0]}

  case "${code}" in
    0|130)
      echo "$(date -Is) executor stopped normally (code=${code})" | tee -a "${LOG_FILE}"
      exit "${code}"
      ;;
    3)
      echo "$(date -Is) executor token rejected; automatic restart stopped to avoid a tight auth loop" | tee -a "${LOG_FILE}"
      echo "Delete ~/.config/polymarket-termux/executor_token and pair again from the dashboard." | tee -a "${LOG_FILE}"
      exit 3
      ;;
    *)
      echo "$(date -Is) executor exited code=${code}; restarting in ${RESTART_DELAY}s" | tee -a "${LOG_FILE}"
      sleep "${RESTART_DELAY}"
      ;;
  esac
done
