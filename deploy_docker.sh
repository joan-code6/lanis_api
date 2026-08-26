#!/usr/bin/env bash
set -euo pipefail

# ── Deployment script for lanis_api ───────────────────────────────────────────
# Run this on the deploy VM to pull latest changes from GitHub and restart
# the containerized service.
# Usage:
#   ./deploy_docker.sh                deploy now
#   ./deploy_docker.sh --install-cron install nightly cron job (3:00 AM)
# -----------------------------------------------------------------------------

# ── Config ───────────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRON_SCHEDULE="0 3 * * *"  # every night at 3:00 AM
CRON_LOG="${PROJECT_DIR}/deploy_docker.log"
LOCK_FILE="${PROJECT_DIR}/.deploy_docker.lock"

# ── Cron install mode ──────────────────────────────────────────────────────────
if [ "${1:-}" = "--install-cron" ]; then
    CRON_LINE="${CRON_SCHEDULE} \"${PROJECT_DIR}/deploy_docker.sh\" >> \"${CRON_LOG}\" 2>&1"
    CURRENT_CRONTAB="$(crontab -l 2>/dev/null || true)"
    CLEANED_CRONTAB="$(printf '%s\n' "${CURRENT_CRONTAB}" \
        | grep -Fv "${PROJECT_DIR}/deploy.sh" \
        | grep -Fv "${PROJECT_DIR}/deploy_docker.sh" || true)"
    {
        printf '%s\n' "${CLEANED_CRONTAB}"
        printf '%s\n' "${CRON_LINE}"
    } | sed '/^[[:space:]]*$/d' | crontab -
    echo "Installed cron job: ${CRON_LINE}"
    exit 0
fi

if ! command -v flock >/dev/null 2>&1; then
    echo "ERROR: flock is required to prevent overlapping deployments." >&2
    exit 1
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "Another lanis_api Docker deployment is already running."
    exit 0
fi

echo "=== Deploying lanis_api ==="
echo "  Project: ${PROJECT_DIR}"
echo "  Time:    $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

# ── 1. Pull latest changes ───────────────────────────────────────────────────
cd "${PROJECT_DIR}"
echo ""
echo ">>> git pull origin main"
git pull --ff-only origin main

if [ ! -f .env ]; then
    echo "ERROR: ${PROJECT_DIR}/.env is required for runtime secrets." >&2
    exit 1
fi

if ! grep -Eq '^[[:space:]]*JWT_SECRET[[:space:]]*=' .env; then
    echo "ERROR: JWT_SECRET must be set in ${PROJECT_DIR}/.env." >&2
    exit 1
fi

echo ""
echo ">>> docker compose config --quiet"
docker compose config --quiet

# ── 2. Rebuild the image and restart the container ────────────────────────────
echo ""
echo ">>> docker compose build --pull"
docker compose build --pull

echo ""
echo ">>> docker compose up -d --wait"
if ! docker compose up -d --wait --wait-timeout 60; then
    echo "ERROR: Container failed to become healthy." >&2
    docker compose ps
    docker compose logs --tail=100 lanis-api
    exit 1
fi

echo ""
echo "=== Deploy complete ==="
docker compose ps
