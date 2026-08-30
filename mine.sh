#!/bin/bash
# One-click start for Alpha Miner
# Usage: ./mine.sh

cd "$(dirname "$0")" || exit

echo "=========================================="
echo "  WorldQuant Alpha Miner"
echo "=========================================="
echo ""
echo "Starting..."
echo "LLM: DeepSeek"
echo "Member: nachuan"
echo "Concurrency: 2 workers"
echo ""
echo "Press Ctrl+C to stop"
echo "=========================================="
echo ""

python run_alpha_miner.py --llm deepseek --member-id nachuan --workers 2
