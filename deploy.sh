#!/usr/bin/env bash
set -euo pipefail

# ── Deployment script for lanis_api ───────────────────────────────────────────
# Run this on the deploy VM to pull latest changes from GitHub and restart.
# Usage: ./deploy.sh
# Assumes: Python venv at ./venv, server managed by systemd or direct process.
# ──────────────────────────────────────────────────────────────────────────────

# ── Config ───────────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/venv"
SERVICE_NAME="lanis-api"
HOST="${LANIS_API_HOST:-0.0.0.0}"
PORT="${LANIS_API_PORT:-8000}"

echo "=== Deploying lanis_api ==="
echo "  Project: ${PROJECT_DIR}"
echo "  Time:    $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

# ── 1. Pull latest changes ───────────────────────────────────────────────────
cd "${PROJECT_DIR}"
echo ""
echo ">>> git pull origin main"
git pull origin main

# ── 2. Install/update dependencies ───────────────────────────────────────────
if [ -d "${VENV_DIR}" ]; then
    echo ""
    echo ">>> pip install -r requirements.txt"
    "${VENV_DIR}/bin/pip" install -r requirements.txt
else
    echo ""
    echo "# No venv found at ${VENV_DIR}, using system python"
    pip install -r requirements.txt
fi

# ── 3. Restart the server ────────────────────────────────────────────────────
if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    echo ""
    echo ">>> systemctl restart ${SERVICE_NAME}"
    sudo systemctl restart "${SERVICE_NAME}"
    echo ""
    echo "=== Service restarted ==="
    sudo systemctl status --no-pager "${SERVICE_NAME}"
else
    echo ""
    echo "# systemd service '${SERVICE_NAME}' not found — restarting directly"

    # Kill any existing uvicorn on our port
    EXISTING_PID=$(lsof -ti "tcp:${PORT}" 2>/dev/null || true)
    if [ -n "${EXISTING_PID}" ]; then
        echo ">>> Killing existing process on port ${PORT} (pid ${EXISTING_PID})"
        kill "${EXISTING_PID}" 2>/dev/null || true
        sleep 1
    fi

    # Start without --reload (production mode)
    PYTHON_BIN="${VENV_DIR}/bin/python3"
    [ -f "${PYTHON_BIN}" ] || PYTHON_BIN="python3"

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
