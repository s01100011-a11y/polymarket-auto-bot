#!/data/data/com.termux/files/usr/bin/bash
set -u
set -o pipefail

ROOT="${HOME}/polymarket-auto-bot"
VENV="${ROOT}/.venv"
LOG_DIR="${HOME}/.config/polymarket-termux"
LOG_FILE="${LOG_DIR}/executor-supervisor.log"
RESTART_DELAY="${EXECUTOR_RESTART_DELAY_SECONDS:-5}"
AUTO_UPDATE="${EXECUTOR_AUTO_UPDATE:-true}"

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

if [ "${AUTO_UPDATE}" = "true" ] && [ -d "${ROOT}/.git" ]; then
  branch="$(git branch --show-current 2>/dev/null || true)"
  dirty="$(git status --porcelain 2>/dev/null || true)"
  if [ "${branch}" != "main" ]; then
    echo "$(date -Is) auto-update skipped: current branch=${branch:-unknown}, expected main" | tee -a "${LOG_FILE}"
  elif [ -n "${dirty}" ]; then
    echo "$(date -Is) auto-update skipped: local working tree has uncommitted changes" | tee -a "${LOG_FILE}"
  else
    before="$(git rev-parse HEAD 2>/dev/null || true)"
    if git fetch --quiet origin main && git merge --ff-only --quiet origin/main; then
      after="$(git rev-parse HEAD 2>/dev/null || true)"
      if [ -n "${after}" ] && [ "${after}" != "${before}" ]; then
        echo "$(date -Is) auto-update advanced ${before:0:12} -> ${after:0:12}" | tee -a "${LOG_FILE}"
        if git diff --name-only "${before}" "${after}" -- requirements.txt 2>/dev/null | grep -q '^requirements.txt$'; then
          echo "$(date -Is) requirements changed; syncing virtualenv" | tee -a "${LOG_FILE}"
          "${VENV}/bin/pip" install --quiet -r requirements.txt || {
            echo "$(date -Is) dependency sync failed; refusing to start worker" | tee -a "${LOG_FILE}"
            exit 4
          }
        fi
      else
        echo "$(date -Is) auto-update already current at ${after:0:12}" | tee -a "${LOG_FILE}"
      fi
    else
      echo "$(date -Is) auto-update failed; continuing with existing checked-out code" | tee -a "${LOG_FILE}"
    fi
  fi
fi

# Kill stale direct workers before starting the supervised v2 wrapper.
pkill -f '[t]ermux_executor_v2.py' 2>/dev/null || true
pkill -f '[t]ermux_executor.py' 2>/dev/null || true

echo "$(date -Is) supervisor starting queue executor" | tee -a "${LOG_FILE}"
echo "$(date -Is) worker=${ROOT}/scripts/termux_executor_v2.py bridge=${EXECUTOR_BRIDGE_URL:-default}" | tee -a "${LOG_FILE}"

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
      echo "$(date -Is) executor token rejected; automatic restart stopped" | tee -a "${LOG_FILE}"
      echo "Delete ~/.config/polymarket-termux/executor_token and pair again." | tee -a "${LOG_FILE}"
      exit 3
      ;;
    4)
      echo "$(date -Is) another executor already owns the worker lock; duplicate supervisor exiting" | tee -a "${LOG_FILE}"
      exit 4
      ;;
    *)
      echo "$(date -Is) executor exited code=${code}; restarting in ${RESTART_DELAY}s" | tee -a "${LOG_FILE}"
      sleep "${RESTART_DELAY}"
      ;;
  esac
done
