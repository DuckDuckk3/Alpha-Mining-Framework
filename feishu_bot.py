"""
Feishu Bot Command Control System

Remotely control miner status, query alpha database, execute correlation 
checks, and submit alphas via Feishu group messages.

Commands:
    /summary           - View alpha database statistics
    /start [workers]   - Start the miner (default: 2 workers)
    /stop              - Stop the miner
    /check             - Run correlation check
    /submit <id> [...] - Submit specified alpha(s)

Usage:
    python feishu_bot.py              # Start bot (default port 9000)
    python feishu_bot.py --port 8080  # Specify custom port
"""

import os
import sys
import signal
import subprocess
import argparse
import logging
from datetime import datetime
from collections import OrderedDict

# Add project root directory to system path to ensure core modules can be imported
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify
from core.alpha_db import get_alpha_db
from core.feishu_client import get_feishu_client
from core.notifier import get_notifier
from core.log_manager import setup_logger

logger = setup_logger(__name__)

app = Flask(__name__)

# File path for storing the miner process ID (PID)
PID_FILE = ".miner.pid"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Message deduplication cache: Records processed message_ids to prevent duplicate execution caused by Feishu retries.
# Uses OrderedDict to implement LRU (Least Recently Used) cache, limiting memory usage.
_processed_messages = OrderedDict()
_processed_messages_max_size = 1000
_processed_messages_ttl_seconds = 300  # Expiration time: 5 minutes


def _is_message_processed(message_id: str) -> bool:
    """Check if a message has already been processed and clean up expired entries."""
    now = datetime.now().timestamp()

    # Clean up expired entries beyond the TTL window
    expired_keys = [
        k for k, v in _processed_messages.items()
        if now - v > _processed_messages_ttl_seconds
    ]
    for k in expired_keys:
        del _processed_messages[k]

    # Check if the current message ID is already processed
    return message_id in _processed_messages


def _mark_message_processed(message_id: str):
    """Mark a message ID as processed."""
    _processed_messages[message_id] = datetime.now().timestamp()

    # Maintain maximum cache size limit by evicting oldest items
    while len(_processed_messages) > _processed_messages_max_size:
        _processed_messages.popitem(last=False)


# ── Process Management ──────────────────────────────────────────────────

def is_miner_running() -> tuple:
    """Check if the mining process is currently running. Returns (is_running, pid)."""
    if not os.path.exists(PID_FILE):
        return False, 0

    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())

        # Signal 0 sends no signal, but performs error checking to verify if process exists
        os.kill(pid, 0)
        return True, pid
    except (ProcessLookupError, ValueError, PermissionError):
        # Clean up stale PID file if process does not exist or lacks permission
        try:
            os.remove(PID_FILE)
        except:
            pass
        return False, 0


def start_miner(workers: int = 2) -> tuple:
    """Start the mining background process. Returns (success, message)."""
    running, pid = is_miner_running()
    if running:
        return False, f"Miner is already running (PID={pid})"

    try:
        log_dir = os.path.join(PROJECT_DIR, "log")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"{datetime.now().strftime('%Y-%m-%d')}.log")

        # Spawn sub-process asynchronously and detach it from current session
        with open(log_file, "w") as lf:
            proc = subprocess.Popen(
                [sys.executable, "run_alpha_miner.py", "--workers", str(workers)],
                cwd=PROJECT_DIR,
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # Ensures process keeps running even if bot restarts
            )

        # Record PID to tracking file
        with open(PID_FILE, "w") as f:
            f.write(str(proc.pid))

        logger.info(f"Mining process started: PID={proc.pid}, workers={workers}")
        return True, f"Miner started (PID={proc.pid}, workers={workers})"

    except Exception as e:
        logger.error(f"Failed to start miner: {e}")
        return False, f"Start failed: {e}"


def stop_miner() -> tuple:
    """Stop the mining process gracefully, fallback to SIGKILL if timeout. Returns (success, message)."""
    running, pid = is_miner_running()
    if not running:
        return False, "Miner is not running"

    try:
        # Graceful shutdown via SIGTERM
        os.kill(pid, signal.SIGTERM)
        logger.info(f"Sent SIGTERM to process {pid}")

        # Polling to wait for process termination
        import time
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(1)
            except ProcessLookupError:
                break
        else:
            # Force kill if process does not stop within 10 seconds
            try:
                os.kill(pid, signal.SIGKILL)
                logger.info(f"Sent SIGKILL to process {pid}")
            except ProcessLookupError:
                pass

        # Remove lock file after process termination
        try:
            os.remove(PID_FILE)
        except:
            pass

        return True, "Miner stopped"

    except ProcessLookupError:
        try:
            os.remove(PID_FILE)
        except:
            pass
        return False, "Mining process already exited"
    except Exception as e:
        logger.error(f"Failed to stop miner: {e}")
        return False, f"Stop failed: {e}"


# ── Command Handlers ────────────────────────────────────────────────────

def cmd_summary(args: list) -> tuple:
    """Execute /summary command. Returns (title, content)."""
    db = get_alpha_db()
    summary = db.get_alpha_summary()

    lines = [
        "## Alpha Database Summary",
        f"- Total Alphas: **{summary['total']}**",
        f"- Submitted: **{summary['submitted']}**",
        f"- Pending Check: **{summary['pending']}**",
        f"- Submittable: **{summary['unsubmitted']}**",
        "",
        "## All-time Statistics",
        f"- All-time Mined: **{summary['new_all_time']}**",
        f"- All-time Submittable: **{summary['submittable_all_time']}**",
    ]

    # Append current mining process state
    running, pid = is_miner_running()
    lines.append("")
    lines.append("## Mining Status")
    if running:
        lines.append(f"- Status: **Running** (PID={pid})")
    else:
        lines.append("- Status: **Stopped**")

    return "Alpha Database Summary", "\n".join(lines)


def cmd_start(args: list) -> tuple:
    """Execute /start command. Returns (title, content)."""
    workers = 2
    if args:
        try:
            workers = int(args[0])
            if workers < 1 or workers > 10:
                return "Start Failed", "Workers count must be between 1 and 10"
        except ValueError:
            return "Start Failed", f"Invalid workers parameter: {args[0]}"

    success, message = start_miner(workers)
    return "Start Miner" if success else "Start Failed", message


def cmd_stop(args: list) -> tuple:
    """Execute /stop command. Returns (title, content)."""
    success, message = stop_miner()
    return "Stop Miner" if success else "Stop Failed", message


def cmd_check(args: list) -> tuple:
    """Execute /check command in batches (5 items per batch, max 300s/batch). Returns (title, content)."""
    import json as _json

    BATCH_SIZE = 5
    BATCH_TIMEOUT = 300  # Timeout threshold in seconds per batch

    try:
        # Retrieve pending list upfront to freeze alpha IDs and avoid duplicate checks across batches
        from core.alpha_db import get_alpha_db
        db = get_alpha_db()
        all_alphas = db.get_all_alphas(limit=10000)
        pending = [a for a in all_alphas if a.get("status") == "pending"]
        pending_ids = [a["alpha_id"] for a in pending if a.get("alpha_id")]
        total_pending = len(pending_ids)

        if total_pending == 0:
            return "Correlation Check", "No pending alphas found."

        num_batches = (total_pending + BATCH_SIZE - 1) // BATCH_SIZE

        lines = ["## Correlation Check Results", "", f"Found {total_pending} pending alphas, splitting into {num_batches} batches ({BATCH_SIZE} per batch)", ""]

        for batch_idx in range(num_batches):
            batch_ids = pending_ids[batch_idx * BATCH_SIZE : (batch_idx + 1) * BATCH_SIZE]
            lines.append(f"**Batch {batch_idx + 1}/{num_batches}** (Items {batch_idx * BATCH_SIZE + 1}-{batch_idx * BATCH_SIZE + len(batch_ids)})")

            try:
                # Invoke external script to execute correlation logic
                result = subprocess.run(
                    [sys.executable, "check_correlation.py", "--no-notify", "--batch-mode",
                     "--alpha-ids", ",".join(batch_ids)],
                    cwd=PROJECT_DIR,
                    capture_output=True,
                    text=True,
                    timeout=BATCH_TIMEOUT,
                )

                output = result.stdout
                marker = "__BATCH_RESULT__"
                if marker in output:
                    json_str = output.split(marker, 1)[1].strip()
                    batch = _json.loads(json_str)
                    lines.append(f"  - PASS: {batch.get('pass', 0)}, FAIL: {batch.get('fail', 0)}, PENDING: {batch.get('pending', 0)}")
                else:
                    lines.append(f"  - ⚠️ Could not parse results")

            except subprocess.TimeoutExpired:
                lines.append(f"  - ⏰ Timed out (>{BATCH_TIMEOUT}s)")
            except Exception as e:
                lines.append(f"  - ❌ Error: {e}")

            lines.append("")  # Empty line separator between batches

        # Fetch updated final stats directly from database
        summary = db.get_alpha_summary()
        after_pending = len([a for a in db.get_all_alphas(limit=10000) if a.get("status") == "pending"])

        lines.append("### Final Summary")
        lines.append(f"- Total Alphas: {summary['total']}")
        lines.append(f"- Submitted: {summary['submitted']}")
        lines.append(f"- Pending Check: {after_pending}")
        lines.append(f"- Submittable: {summary['unsubmitted']}")
        lines.append(f"- All-time Mined: {summary['new_all_time']}")

        return "Correlation Check", "\n".join(lines)

    except Exception as e:
        return "Check Failed", f"Execution error: {e}"


def cmd_submit(args: list) -> tuple:
    """Execute /submit command. Returns (title, content)."""
    if not args:
        return "Submission Failed", "Please specify alpha ID(s), e.g.: /submit akornja9 QPnwOqKr"

    try:
        result = subprocess.run(
            [sys.executable, "submit_alpha.py"] + args,
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )

        output = result.stdout
        lines = ["## Submission Results", ""]

        # Parse individual submission output from command stdout
        for line in output.split("\n"):
            if "Submitted successfully" in line:
                parts = line.split()
                alpha_id = parts[2] if len(parts) > 2 else "unknown"
                lines.append(f"- {alpha_id}: ✅ Success")
            elif "Submission failed" in line:
                parts = line.split()
                alpha_id = parts[2] if len(parts) > 2 else "unknown"
                lines.append(f"- {alpha_id}: ❌ Failed")
            elif "Alpha deleted from database" in line:
                lines.append(f"  - Deleted from database")

        if not lines[2:]:  # Output was not standard, display raw log preview
            lines.append(f"```\n{output[:500]}\n```")

        return "Alpha Submission", "\n".join(lines)

    except subprocess.TimeoutExpired:
        return "Submission Timeout", "Alpha submission execution timed out (>60s)"
    except Exception as e:
        return "Submission Failed", f"Execution error: {e}"


def cmd_list(args: list) -> tuple:
    """Execute /list command. Returns (title, content)."""
    db = get_alpha_db()

    # Parse command arguments
    limit = 20  # Default limit count
    status_filter = None

    for arg in args:
        if arg.isdigit():
            limit = min(int(arg), 100)  # Max threshold capped at 100
        elif arg in ["submitted", "unsubmitted", "pending", "tested"]:
            status_filter = arg

    # Query records from SQLite database
    with db._cursor() as cur:
        if status_filter:
            cur.execute(
                "SELECT alpha_id, status, grade, fitness, sharpe FROM alphas WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status_filter, limit)
            )
        else:
            cur.execute(
                "SELECT alpha_id, status, grade, fitness, sharpe FROM alphas ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
        rows = cur.fetchall()

    if not rows:
        return "Alpha List", "No data available."

    # Format Markdown output table
    lines = [f"## Alpha List (Total {len(rows)} items)", ""]
    lines.append("| Alpha ID | Status | Grade | Fitness | Sharpe |")
    lines.append("|----------|--------|-------|---------|--------|")

    for row in rows:
        alpha_id = row["alpha_id"] or "-"
        status = row["status"] or "-"
        grade = row["grade"] or "-"
        fitness = f"{row['fitness']:.4f}" if row["fitness"] else "-"
        sharpe = f"{row['sharpe']:.4f}" if row["sharpe"] else "-"
        lines.append(f"| {alpha_id} | {status} | {grade} | {fitness} | {sharpe} |")

    # Display usage hints
    lines.append("")
    lines.append("*Usage: /list [limit] [status], e.g., /list 50 submitted*")

    return "Alpha List", "\n".join(lines)


def cmd_help(args: list) -> tuple:
    """Execute /help command. Returns (title, content)."""
    lines = [
        "## Available Commands",
        "",
        "| Command | Description | Example |",
        "|---------|-------------|---------|",
        "| `/summary` | View alpha DB statistics | `/summary` |",
        "| `/start [workers]` | Start miner process | `/start 2` |",
        "| `/stop` | Stop miner process | `/stop` |",
        "| `/check` | Run correlation check | `/check` |",
        "| `/submit <id> [id2...]` | Submit specified alpha(s) | `/submit akornja9` |",
        "| `/list [limit] [status]` | View alpha list | `/list 20 submitted` |",
        "| `/help` | Show this help message | `/help` |",
        "",
        "**Status filters:** submitted, unsubmitted, pending, tested",
    ]
    return "Help Information", "\n".join(lines)


# ── Command Routing Dictionary ──────────────────────────────────────────

COMMANDS = {
    "/summary": cmd_summary,
    "/start": cmd_start,
    "/stop": cmd_stop,
    "/check": cmd_check,
    "/submit": cmd_submit,
    "/list": cmd_list,
    "/help": cmd_help,
}


def parse_command(text: str) -> tuple:
    """Parse string text into command and arguments tuple."""
    parts = text.strip().split()
    if not parts:
        return None, []

    cmd = parts[0].lower()
    args = parts[1:]
    return cmd, args


# ── Webhook Routes ──────────────────────────────────────────────────────

@app.route("/feishu/webhook", methods=["POST"])
def webhook():
    """Receive and process incoming callback events from Feishu."""
    body = request.get_json(force=True)

    # Handle Feishu URL challenge verification handshake
    if "challenge" in body:
        logger.info("Received challenge verification request")
        client = get_feishu_client()
        return jsonify(client.verify_challenge(body))

    # Log incoming event header info
    header = body.get("header", {})
    event_type = header.get("event_type", body.get("type", "unknown"))
    event_id = header.get("event_id", "")
    logger.info(f"Received Feishu event: type={event_type}, event_id={event_id}")

    # Message deduplication: Prevents repeated triggers caused by network retry policies
    if event_id and _is_message_processed(event_id):
        logger.warning(f"Event already processed, skipping duplicate execution: event_id={event_id}")
        return jsonify({"code": 0})

    # Parse message content
    client = get_feishu_client()
    message_id, chat_id, text = client.parse_message(body)

    if not message_id or not text:
        logger.debug(f"Message parsing returned empty results (message_id={message_id}, text={text})")
        return jsonify({"code": 0})

    # Mark message as processed early to prevent race conditions during execution
    if event_id:
        _mark_message_processed(event_id)

    logger.info(f"Received message: {text}")

    # Route and parse command
    cmd, args = parse_command(text)

    if cmd not in COMMANDS:
        # Fallback for unrecognized commands
        client.reply_message(
            message_id,
            "Unknown Command",
            f"Supported commands:\n" + "\n".join(f"- `{k}`" for k in COMMANDS.keys())
        )
        return jsonify({"code": 0})

    # Execute matched command
    try:
        title, content = COMMANDS[cmd](args)
        client.reply_message(message_id, title, content)
    except Exception as e:
        logger.error(f"Command execution failed: {e}")
        client.reply_message(message_id, "Execution Failed", f"Error: {e}")

    return jsonify({"code": 0})


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    running, pid = is_miner_running()
    return jsonify({
        "status": "ok",
        "miner_running": running,
        "miner_pid": pid if running else None,
    })


@app.route("/test", methods=["POST"])
def test_command():
    """Local debugging route to test command execution directly without Feishu webhook."""
    body = request.get_json(force=True)
    text = body.get("text", "").strip()

    if not text:
        return jsonify({"error": "Please provide the 'text' parameter"}), 400

    logger.info(f"[Test] Received command: {text}")

    # Parse command
    cmd, args = parse_command(text)

    if cmd not in COMMANDS:
        return jsonify({
            "command": text,
            "error": f"Unknown command. Supported commands: {', '.join(COMMANDS.keys())}"
        })

    # Execute command directly
    try:
        title, content = COMMANDS[cmd](args)
        return jsonify({
            "command": text,
            "title": title,
            "content": content,
        })
    except Exception as e:
        logger.error(f"[Test] Command execution failed: {e}")
        return jsonify({
            "command": text,
            "error": str(e),
        }), 500


# ── Main Entry Point ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Feishu Bot Command Control System")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("FEISHU_BOT_PORT", "9000")),
        help="HTTP service port (default: 9000)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Listening address (default: 0.0.0.0)",
    )
    args = parser.parse_args()

    # Validate credential configurations
    client = get_feishu_client()
    if not client.enabled:
        logger.error("FEISHU_APP_ID or FEISHU_APP_SECRET is not configured")
        print("Error: Please set FEISHU_APP_ID and FEISHU_APP_SECRET in your .env file")
        sys.exit(1)

    print("=" * 50)
    print("  Feishu Bot Command Control System")
    print("=" * 50)
    print(f"  Listening on: {args.host}:{args.port}")
    print(f"  Webhook: http://<Server_IP>:{args.port}/feishu/webhook")
    print(f"  Health Check: http://localhost:{args.port}/health")
    print("-" * 50)
    print("  Supported Commands: /summary, /start, /stop, /check, /submit, /list, /help")
    print("=" * 50)

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
