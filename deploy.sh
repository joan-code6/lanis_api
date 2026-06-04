#!/usr/bin/env bash
set -euo pipefail

# ── Deployment script for lanis_api ───────────────────────────────────────────
# Run this on the deploy VM to pull latest changes from GitHub and restart.
# Usage:
#   ./deploy.sh                deploy now
#   ./deploy.sh --install-cron install nightly cron job (3:00 AM)
# -----------------------------------------------------------------------------

# ── Config ───────────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRON_SCHEDULE="0 3 * * *"  # every night at 3:00 AM
CRON_LOG="/var/log/lanis-api-deploy.log"
VENV_DIR="${PROJECT_DIR}/venv"
SERVICE_NAME="lanis-api"
HOST="${LANIS_API_HOST:-0.0.0.0}"
PORT="${LANIS_API_PORT:-8000}"

# ── Cron install mode ──────────────────────────────────────────────────────────
if [ "${1:-}" = "--install-cron" ]; then
    CRON_LINE="${CRON_SCHEDULE} ${PROJECT_DIR}/deploy.sh >> ${CRON_LOG} 2>&1"
    if crontab -l 2>/dev/null | grep -Fq "${PROJECT_DIR}/deploy.sh"; then
        echo "Cron job already installed:"
        crontab -l 2>/dev/null | grep -F "${PROJECT_DIR}/deploy.sh"
    else
        (crontab -l 2>/dev/null; echo "${CRON_LINE}") | crontab -
        echo "Installed cron job: ${CRON_LINE}"
    fi
    exit 0
fi

echo "=== Deploying lanis_api ==="
echo "  Project: ${PROJECT_DIR}"
echo "  Time:    $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

# ── 1. Pull latest changes ───────────────────────────────────────────────────
cd "${PROJECT_DIR}"
echo ""
echo ">>> git pull origin main"
git pull origin main

# ── 2. Create venv if missing ─────────────────────────────────────────────────
if [ ! -d "${VENV_DIR}" ]; then
    echo ""
    echo ">>> Creating venv at ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
fi

echo ""
echo ">>> pip install -r requirements.txt"
"${VENV_DIR}/bin/pip" install -r requirements.txt

# ── 3. Resolve python binary ─────────────────────────────────────────────────
PYTHON_BIN="${VENV_DIR}/bin/python3"
[ -f "${PYTHON_BIN}" ] || PYTHON_BIN="python3"
echo ""
echo "  Python:  ${PYTHON_BIN}"

# ── 4. Install systemd service (auto-generate from template) ─────────────────
SERVICE_SRC="${PROJECT_DIR}/lanis-api.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_TMP="/tmp/${SERVICE_NAME}.service"

if [ -f "${SERVICE_SRC}" ]; then
    echo ""
    echo ">>> Installing systemd service from template"
    sed -e "s|{{PROJECT_DIR}}|${PROJECT_DIR}|g" \
        -e "s|{{PYTHON_BIN}}|${PYTHON_BIN}|g" \
        "${SERVICE_SRC}" > "${SERVICE_TMP}"
    cp "${SERVICE_TMP}" "${SERVICE_DST}"
    systemctl daemon-reload
fi

# ── 5. Restart the server ────────────────────────────────────────────────────
if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    echo ""
    echo ">>> systemctl restart ${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
    echo ""
    echo "=== Service restarted ==="
    systemctl status --no-pager "${SERVICE_NAME}"
elif [ -f "${SERVICE_DST}" ]; then
    echo ""
    echo ">>> systemctl start ${SERVICE_NAME}"
    systemctl start "${SERVICE_NAME}"
    echo ""
    echo "=== Service started ==="
    systemctl status --no-pager "${SERVICE_NAME}"
else
    echo ""
    echo "# No systemd — starting directly"

    EXISTING_PID=$(lsof -ti "tcp:${PORT}" 2>/dev/null || true)
    if [ -n "${EXISTING_PID}" ]; then
        echo ">>> Killing existing process on port ${PORT} (pid ${EXISTING_PID})"
        kill "${EXISTING_PID}" 2>/dev/null || true
        sleep 1
    fi

    echo ">>> Starting uvicorn (host=${HOST}, port=${PORT})"
    nohup "${PYTHON_BIN}" -m uvicorn api.api:app \
        --host "${HOST}" \
        --port "${PORT}" \
        --no-access-log \
        > /tmp/lanis-api.log 2>&1 &

    sleep 1
    if lsof -ti "tcp:${PORT}" >/dev/null 2>&1; then
        echo "=== Server running on ${HOST}:${PORT} ==="
    else
        echo "!!! Server may have failed to start — check /tmp/lanis-api.log"
    fi
fi

echo ""
echo "=== Deploy complete ==="
