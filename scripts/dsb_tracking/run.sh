#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="/tmp/substitution_fetcher.log"

cd "$PROJECT_DIR" || exit 1

run_once() {
    python3 "$SCRIPT_DIR/fetch_substitution_plan.py" \
        --school-id 5201 \
        --username 282822 \
        --password berlin \
        --run-once >> "$LOG_FILE" 2>&1
}

run_daemon() {
    echo "Setting up cron job for daily fetch at 1 PM..."
    SCRIPT_PATH="$SCRIPT_DIR/fetch_substitution_plan.py"
    cron_entry="0 13 * * * cd $PROJECT_DIR && python3 $SCRIPT_PATH --school-id 5201 --username 282822 --password berlin >> $LOG_FILE 2>&1"
    
    (crontab -l 2>/dev/null | grep -v "fetch_substitution_plan.py"; echo "$cron_entry") | crontab -
    echo "Cron job added. Use 'crontab -l' to verify."
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