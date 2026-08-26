#!/usr/bin/env bash
set -euo pipefail

# ── Deployment script for lanis_api ───────────────────────────────────────────
# Run this on the deploy VM to pull latest changes from GitHub and restart
# the containerized service.
# Usage:
#   ./deploy.sh                deploy now
#   ./deploy.sh --install-cron install nightly cron job (3:00 AM)
# -----------------------------------------------------------------------------

# ── Config ───────────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRON_SCHEDULE="0 3 * * *"  # every night at 3:00 AM
CRON_LOG="/var/log/lanis-api-deploy.log"

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

# ── 2. Rebuild the image and restart the container ────────────────────────────
echo ""
echo ">>> docker compose build"
docker compose build

echo ""
echo ">>> docker compose up -d"
docker compose up -d

echo ""
echo "=== Deploy complete ==="
docker compose ps
