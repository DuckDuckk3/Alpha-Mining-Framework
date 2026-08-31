#!/bin/bash
# Set up scheduled correlation check
# Usage:
#   ./setup_cron.sh          # Add cron job (Every day at 9:00 Beijing Time)
#   ./setup_cron.sh remove   # Remove cron job
#   ./setup_cron.sh status   # Check cron job status

cd "$(dirname "$0")" || exit

SCRIPT_DIR="$(pwd)"
CRON_ENTRY="0 1 * * * cd $SCRIPT_DIR && python check_correlation.py --delete-fail >> log/correlation_check.log 2>&1"

add_cron() {
    echo "=========================================="
    echo "  Setting up scheduled correlation check"
    echo "=========================================="
    echo ""
    echo "The following crontab entry will be added:"
    echo "  Run correlation check daily at 9:00 Beijing Time (1:00 UTC)"
    echo "  Automatically delete alphas with SELF_CORRELATION FAIL"
    echo ""

    # Check if cron entry already exists
    if crontab -l 2>/dev/null | grep -q "check_correlation.py"; then
        echo "Cron job already exists:"
        crontab -l | grep "check_correlation.py"
        echo ""
        read -p "Do you want to update it? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Cancelled"
            return
        fi
        # Remove old entry
        crontab -l 2>/dev/null | grep -v "check_correlation.py" | crontab -
    fi

    # Add new cron entry
    (crontab -l 2>/dev/null; echo "$CRON_ENTRY") | crontab -

    echo "Cron job added successfully!"
    echo ""
    echo "Current crontab:"
    crontab -l | grep "check_correlation.py"
    echo ""
    echo "Log file: log/correlation_check.log"
    echo ""
    echo "Run check manually:"
    echo "  python check_correlation.py --dry-run    # Check only"
    echo "  python check_correlation.py --delete-fail # Check and delete"
    echo "=========================================="
}

remove_cron() {
    echo "Removing scheduled correlation check..."
    if crontab -l 2>/dev/null | grep -q "check_correlation.py"; then
        crontab -l 2>/dev/null | grep -v "check_correlation.py" | crontab -
        echo "Cron job removed"
    else
        echo "No cron job found"
    fi
}

status() {
    echo "=========================================="
    echo "  Scheduled Correlation Check Status"
    echo "=========================================="
    echo ""
    if crontab -l 2>/dev/null | grep -q "check_correlation.py"; then
        echo "  Status: Enabled"
        echo "  Job: $(crontab -l | grep 'check_correlation.py')"
        echo ""
        echo "  Log file: log/correlation_check.log"
        if [ -f "log/correlation_check.log" ]; then
            echo "  Recent logs:"
            tail -5 "log/correlation_check.log" | sed 's/^/    /'
        fi
    else
        echo "  Status: Disabled"
        echo ""
        echo "  Use './setup_cron.sh' to add the cron job"
    fi
    echo ""
    echo "=========================================="
}

# Main
case "${1:-add}" in
    add)
        add_cron
        ;;
    remove)
        remove_cron
        ;;
    status)
        status
        ;;
    *)
        echo "Usage: $0 {add|remove|status}"
        echo ""
        echo "  add     Add cron job (Every day at 9:00 Beijing Time)"
        echo "  remove  Remove cron job"
        echo "  status  Check cron job status"
        exit 1
        ;;
esac
