#!/bin/bash
# Submit unsubmitted factors
# Usage:
#   ./submit.sh          # Submit all unsubmitted factors
#   ./submit.sh 3        # Submit only the top 3
#   ./submit.sh 1        # Submit only 1 factor

cd "$(dirname "$0")" || exit

LIMIT=${1:-""}  # First argument, empty if not provided

echo "=========================================="
echo "  WorldQuant Alpha Submission Tool"
echo "=========================================="
echo ""

# Get unsubmitted factors from database
if [ -z "$LIMIT" ]; then
    UNSUBMITTED=$(sqlite3 data/alphas.db "SELECT alpha_id FROM alphas WHERE status = 'unsubmitted';")
    MODE="All"
else
    UNSUBMITTED=$(sqlite3 data/alphas.db "SELECT alpha_id FROM alphas WHERE status = 'unsubmitted' LIMIT $LIMIT;")
    MODE="Top $LIMIT"
fi

if [ -z "$UNSUBMITTED" ]; then
    echo "No unsubmitted factors found"
    exit 0
fi

COUNT=$(echo "$UNSUBMITTED" | wc -l | tr -d ' ')
echo "Found $COUNT unsubmitted factor(s) ($MODE):"
echo "$UNSUBMITTED"
echo ""
echo "Starting submission..."
echo "=========================================="
echo ""

# Convert to argument list and submit
python submit_alpha.py "$UNSUBMITTED"

echo ""
echo "=========================================="
echo "Submission completed!"
echo "=========================================="
