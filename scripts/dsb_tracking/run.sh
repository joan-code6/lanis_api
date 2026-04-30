#!/bin/bash

# Substitution Plan Fetcher - Startup Script
# Run this on Raspberry Pi: bash scripts/dsb_tracking/run.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="/tmp/substitution_fetcher.log"

cd "$PROJECT_DIR" || exit 1

# Run once (for testing)
run_once() {
    python3 scripts/dsb_tracking/fetch_substitution_plan.py \
        --school-id 5201 \
        --username 282822 \
        --password berlin \
        --run-once >> "$LOG_FILE" 2>&1
}

# Run as daemon (daily at 1 PM via cron)
run_daemon() {
    echo "Setting up cron job for daily fetch at 1 PM..."
    cron_entry="0 13 * * * cd $PROJECT_DIR && python3 scripts/dsb_tracking/fetch_substitution_plan.py --school-id 5201 --username 282822 --password berlin >> $LOG_FILE 2>&1"
    
    (crontab -l 2>/dev/null | grep -v "fetch_substitution_plan.py"; echo "$cron_entry") | crontab -
    echo "Cron job added. Use 'crontab -l' to verify."
    echo "Use 'sudo service cron restart' if needed."
}

# Check status
status() {
    echo "=== Process Status ==="
    ps aux | grep -v grep | grep fetch_substitution_plan.py || echo "No process running"
    
    echo ""
    echo "=== Recent Logs ==="
    tail -20 "$LOG_FILE"
}

# Run now
case "${1:-run}" in
    run)
        run_once
        ;;
    daemon)
        run_daemon
        ;;
    status)
        status
        ;;
    logs)
        tail -f "$LOG_FILE"
        ;;
    *)
        echo "Usage: $0 [run|daemon|status|logs]"
        echo "  run    - Run once now"
        echo "  daemon - Setup daily cron job at 1 PM"
        echo "  status - Check if running and show recent logs"
        echo "  logs   - Follow logs in real-time"
        ;;
esac