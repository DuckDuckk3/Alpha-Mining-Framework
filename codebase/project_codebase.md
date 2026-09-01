# PROJECT CODEBASE OVERVIEW

## File: `add_alpha.py`

```python
"""
Add alpha to database by ID.

Usage:
    python add_alpha.py <alpha_id>
    python add_alpha.py pwnbR9Gq akNmojM1
"""

import os
import sys
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import requests
from requests.auth import HTTPBasicAuth
from core.config import load_credentials
from core.alpha_db import get_alpha_db


def fetch_alpha_data(alpha_id: str) -> dict:
    """Fetch alpha data from WQ Brain API."""
    username, password = load_credentials()
    session = requests.Session()
    session.auth = HTTPBasicAuth(username, password)
    session.post('https://api.worldquantbrain.com/authentication', verify=False, timeout=15)

    resp = session.get(f'https://api.worldquantbrain.com/alphas/{alpha_id}', verify=False, timeout=30)
    if resp.status_code != 200:
        print(f"Error fetching alpha {alpha_id}: {resp.status_code}")
        return None

    data = resp.json()
    is_data = data.get('is', {})
    settings = data.get('settings', {})
    expression_data = data.get('regular', {})
    expression = expression_data.get('code', '') if isinstance(expression_data, dict) else str(expression_data)

    return {
        "expression": expression,
        "alpha_id": data.get("id", alpha_id),
        "sharpe": is_data.get("sharpe", 0),
        "fitness": is_data.get("fitness", 0),
        "turnover": is_data.get("turnover", 0),
        "margin": is_data.get("margin", 0),
        "returns": is_data.get("returns", 0),
        "long_count": is_data.get("longCount", 0),
        "short_count": is_data.get("shortCount", 0),
        "drawdown": is_data.get("drawdown", 0),
        "grade": data.get("grade", ""),
        "checks": is_data.get("checks", []),
        "region": settings.get("region", "USA"),
        "universe": settings.get("universe", "TOP3000"),
        "delay": settings.get("delay", 1),
        "decay": settings.get("decay", 0),
        "neutralization": settings.get("neutralization", "NONE"),
        "truncation": settings.get("truncation", 0.08),
    }


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ['-h', '--help']:
        print(__doc__)
        sys.exit(0)

    db = get_alpha_db()
    alpha_ids = sys.argv[1:]

    for alpha_id in alpha_ids:
        print(f"\nFetching alpha {alpha_id}...")
        alpha_data = fetch_alpha_data(alpha_id)

        if alpha_data:
            print(f"  Expression: {alpha_data['expression'][:60]}...")
            print(f"  Sharpe: {alpha_data['sharpe']:.2f}")
            print(f"  Fitness: {alpha_data['fitness']:.2f}")

            db.add_alpha(**alpha_data, source="manual", status="submitted")
            print(f"  ✓ Added to database")
        else:
            print(f"  ✗ Failed to fetch alpha {alpha_id}")

    # Summary
    total = db.count_alphas()
    print(f"\nTotal alphas in database: {total}")


if __name__ == "__main__":
    main()

```

----------------------------------------

## File: `check_correlation.py`

```python
"""
检查 pending alpha 的相关性状态

通过 GET /alphas/{alpha_id}/check 端点查询真实的 check 状态，
不需要提交 alpha。检查结果和汇总统计发送到飞书。

新入库的因子 status 为 "pending"，通过相关性检测后变为 "unsubmitted"。
默认删除未通过相关性检查的因子。

Usage:
    python check_correlation.py              # 检查所有 pending alpha（失败自动删除）
    python check_correlation.py --dry-run    # 只检查，不更新数据库
    python check_correlation.py --keep-fail  # 保留失败的 alpha 不删除
    python check_correlation.py --no-notify  # 不发送飞书通知
"""

import os
import sys
import time
import json
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import requests
from requests.auth import HTTPBasicAuth
from core.config import load_credentials
from core.alpha_db import get_alpha_db
from core.notifier import get_notifier
from core.log_manager import setup_logger

logger = setup_logger(__name__)

BASE_URL = "https://api.worldquantbrain.com"
ACCEPT_V2 = "application/json;version=2.0"


def check_alpha(session: requests.Session, alpha_id: str, max_wait: int = 120) -> dict:
    """
    通过 /check 端点获取 alpha 的真实检查状态（不提交）。
    返回: {"success": True, "checks": [...]} 或 {"success": False, "error": "..."}
    """
    url = f"{BASE_URL}/alphas/{alpha_id}/check"
    deadline = time.time() + max_wait

    while time.time() < deadline:
        try:
            resp = session.get(
                url,
                headers={"Accept": ACCEPT_V2},
                verify=False,
                timeout=30
            )

            # 处理 429 限流：有 Retry-After 用它，没有则默认等 10 秒
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else 10
                time.sleep(wait if wait > 0 else 10)
                continue

            # 检查 Retry-After 头（非 429 状态码）
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait = float(retry_after)
                time.sleep(wait if wait > 0 else 3)
                continue

            if resp.status_code == 200 and resp.text:
                data = resp.json()
                checks = data.get("is", {}).get("checks", [])
                return {"success": True, "checks": checks}

            time.sleep(3)

        except Exception as e:
            return {"success": False, "error": str(e)}

    return {"success": False, "error": "Timeout"}


def get_self_correlation(checks: list) -> dict:
    """从 checks 列表中提取 SELF_CORRELATION 状态"""
    for check in checks:
        if check.get("name") == "SELF_CORRELATION":
            return {
                "status": check.get("result", "UNKNOWN"),
                "value": check.get("value"),
                "limit": check.get("limit"),
            }
    return {"status": "NOT_FOUND", "value": None, "limit": None}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="检查 alpha 相关性状态")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不更新数据库")
    parser.add_argument("--keep-fail", action="store_true", help="保留 SELF_CORRELATION FAIL 的 alpha（默认删除）")
    parser.add_argument("--no-notify", action="store_true", help="不发送飞书通知")
    parser.add_argument("--limit", type=int, default=0, help="最多检查 N 个 alpha（0=全部）")
    parser.add_argument("--batch-mode", action="store_true", help="批次模式：输出 JSON 格式结果供外部脚本聚合")
    parser.add_argument("--alpha-ids", type=str, default="", help="指定要检查的 alpha ID 列表（逗号分隔），跳过数据库查询")
    args = parser.parse_args()

    # Authenticate
    username, password = load_credentials()
    session = requests.Session()
    session.auth = HTTPBasicAuth(username, password)
    resp = session.post(f"{BASE_URL}/authentication", verify=False, timeout=15)

    if resp.status_code != 201:
        print(f"Authentication failed: {resp.text}")
        sys.exit(1)

    print("Authentication successful\n")

    db = get_alpha_db()
    notifier = get_notifier()

    # 支持 --alpha-ids 参数：直接使用指定的 alpha ID 列表，不查数据库
    if args.alpha_ids:
        target_ids = [aid.strip() for aid in args.alpha_ids.split(',') if aid.strip()]
        all_alphas = db.get_all_alphas(limit=10000)
        alpha_map = {a.get('alpha_id'): a for a in all_alphas}
        unsubmitted = [alpha_map[aid] for aid in target_ids if aid in alpha_map]
        print(f"使用指定的 {len(unsubmitted)} 个 alpha ID")
    else:
        # Get all pending alphas (waiting for correlation check)
        alphas = db.get_all_alphas(limit=10000)
        unsubmitted = [a for a in alphas if a.get("status") == "pending"]

    if not unsubmitted:
        print("没有 pending 的 alpha")
        return

    # 支持 --limit 参数，只检查前 N 个
    if args.limit > 0:
        unsubmitted = unsubmitted[:args.limit]

    print(f"找到 {len(unsubmitted)} 个 pending alpha\n")

    stats = {
        "total": len(unsubmitted),
        "pass": 0,
        "fail": 0,
        "pending": 0,
        "updated": 0,
        "deleted": 0,
        "error": 0,
    }

    failed_alphas = []

    for i, alpha in enumerate(unsubmitted, 1):
        alpha_id = alpha.get("alpha_id")
        if not alpha_id:
            continue

        expression = alpha.get("expression", "")[:50]
        print(f"[{i}/{len(unsubmitted)}] {alpha_id}", end=" ", flush=True)

        result = check_alpha(session, alpha_id)

        if not result["success"]:
            print(f"错误: {result['error']}")
            stats["error"] += 1
            continue

        checks = result["checks"]
        sc = get_self_correlation(checks)

        if sc["status"] == "PASS":
            print(f"✓ PASS (value={sc['value']:.4f}, limit={sc['limit']})")
            stats["pass"] += 1
            if not args.dry_run:
                db.update_alpha_checks(alpha_id, checks)
                db.update_alpha_status(alpha_id, "unsubmitted")
                stats["updated"] += 1

        elif sc["status"] == "FAIL":
            print(f"✗ FAIL (value={sc['value']:.4f}, limit={sc['limit']})")
            stats["fail"] += 1
            failed_alphas.append({
                "alpha_id": alpha_id,
                "value": sc["value"],
                "limit": sc["limit"],
            })
            if not args.dry_run and not args.keep_fail:
                db.delete_alpha_by_alpha_id(alpha_id)
                stats["deleted"] += 1
                print(f"  -> 已删除")

        elif sc["status"] == "PENDING":
            print("⏳ PENDING")
            stats["pending"] += 1

        elif sc["status"] == "ERROR":
            # SELF_CORRELATION ERROR：标记为 error 状态，可手动提交
            print(f"⚠ ERROR")
            stats["error"] += 1
            if not args.dry_run:
                db.update_alpha_status(alpha_id, "error")

        else:
            print(f"? {sc['status']}")
            stats["error"] += 1

        # 避免 API 限流
        time.sleep(2)

    # Get summary statistics
    summary = db.get_alpha_summary()

    # Print summary
    print("\n" + "=" * 50)
    print("检查完成")
    print("=" * 50)
    print(f"总数: {stats['total']}")
    print(f"PASS: {stats['pass']}")
    print(f"FAIL: {stats['fail']}")
    print(f"PENDING: {stats['pending']}")
    if not args.dry_run:
        print(f"已更新: {stats['updated']}")
        if not args.keep_fail:
            print(f"已删除: {stats['deleted']}")
    print(f"错误: {stats['error']}")
    print()
    print("因子库汇总:")
    print(f"  总数: {summary['total']}")
    print(f"  已提交: {summary['submitted']}")
    print(f"  未提交: {summary['unsubmitted']}")
    print(f"  累计因子: {summary['new_all_time']}")
    print(f"  累计可提交: {summary['submittable_all_time']}")

    # 批次模式：输出 JSON 给外部脚本聚合，不发通知
    if args.batch_mode:
        import json
        batch_result = {
            "total": stats["total"],
            "pass": stats["pass"],
            "fail": stats["fail"],
            "pending": stats["pending"],
            "updated": stats["updated"],
            "deleted": stats["deleted"],
            "error": stats["error"],
            "failed_alphas": failed_alphas,
            "summary": summary,
        }
        print("\n__BATCH_RESULT__")
        print(json.dumps(batch_result, ensure_ascii=False))
        return

    # Send Feishu notification
    if not args.no_notify and notifier.enabled:
        print("\n发送飞书通知...")
        notifier.notify_correlation_check(
            total=stats["total"],
            passed=stats["pass"],
            failed=stats["fail"],
            failed_alphas=failed_alphas,
            summary=summary,
        )
        print("飞书通知已发送")


if __name__ == "__main__":
    main()

```

----------------------------------------

## File: `feishu_bot.py`

```python
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

```

----------------------------------------

## File: `fetch_fields.py`

```python
"""
Fetch operators from WorldQuant Brain API.

Saves results to:
- data/fields_delay{delay}/{dataset}.csv — Data fields grouped by dataset
- data/operators/operators.csv — All operators

Usage:
    python fetch_fields.py              # Fetch delay=1 fields (default)
    python fetch_fields.py --delay 0    # Fetch delay=0 fields
    python fetch_fields.py --delay 1    # Fetch delay=1 fields
"""

import os
import sys
import logging
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from dotenv import load_dotenv
load_dotenv()

from core.data_fetcher import DataFetcher
from core.api_session import get_session


def main():
    parser = argparse.ArgumentParser(description="Fetch data fields from WorldQuant Brain")
    parser.add_argument(
        "--delay",
        type=int,
        default=1,
        choices=[0, 1],
        help="Delay type: 0 or 1 (default: 1)"
    )
    args = parser.parse_args()

    # Get authenticated session
    print("Authenticating with WorldQuant Brain...")
    session = get_session()

    fetcher = DataFetcher(session=session)

    # Fetch and save fields
    print(f"Fetching delay={args.delay} fields...")
    fields = fetcher.fetch_all_fields(delay=args.delay)
    field_files = fetcher.save_fields_to_csv(fields, delay=args.delay)

    print(f"\n=== Fields (delay={args.delay}) ===")
    print(f"Saved {len(field_files)} field files")
    for file in field_files:
        print(f"  {file}")

    # Fetch and save operators
    print("\nFetching operators...")
    operators = fetcher.fetch_operators()
    csv_path = fetcher.save_operators_to_csv(operators)

    print(f"\n=== Operators ===")
    print(f"Saved {len(operators)} operators to {csv_path}")
    for category, count in fetcher.get_operator_summary().items():
        print(f"  {category}: {count} operators")

    print("\nDone!")


if __name__ == "__main__":
    main()

```

----------------------------------------

## File: `merge_code.py`

```python
import os


def merge_python_files(source_dir: str, output_file: str):
    """
    Scans the specified source directory for all .py files and merges their contents
    into a single Markdown document suitable for NotebookLM ingestion.
    """
    # Directory names to exclude from traversal
    ignore_dirs = {
        '.git',
        '.venv',
        'venv',
        '__pycache__',
        'build',
        'dist',
        '.idea',
        '.vscode',
    }

    # Resolve absolute paths for reliable file handling
    abs_source_dir = os.path.abspath(source_dir)
    abs_output_file = os.path.abspath(output_file)

    print(f"🔍 Scanning directory: {abs_source_dir}\n")

    file_count = 0
    with open(abs_output_file, 'w', encoding='utf-8') as outfile:
        outfile.write("# PROJECT CODEBASE OVERVIEW\n\n")

        for root, dirs, files in os.walk(abs_source_dir):
            # Prune ignored directories in-place
            dirs[:] = [d for d in dirs if d not in ignore_dirs]

            for file in files:
                file_path = os.path.join(root, file)

                # Skip output file if it happens to share a .py extension
                if file.endswith('.py') and (
                    os.path.abspath(file_path) != abs_output_file
                ):
                    rel_path = os.path.relpath(file_path, abs_source_dir)

                    print(f"  [+] Found: {rel_path}")
                    outfile.write(f"## File: `{rel_path}`\n\n")
                    outfile.write("```python\n")
                    try:
                        with open(file_path, 'r', encoding='utf-8') as infile:
                            outfile.write(infile.read())
                    except Exception as e:
                        outfile.write(f"# Error reading file: {e}\n")
                    outfile.write("\n```\n\n" + "-" * 40 + "\n\n")
                    file_count += 1

    print("\n" + "=" * 50)
    if file_count > 0:
        print(f"✅ Successfully merged {file_count} .py file(s)!")
        print(f"📁 Output file saved to: {abs_output_file}")
    else:
        print("⚠️ No .py files found! Please check your PROJECT_DIR path.")
    print("=" * 50)


if __name__ == "__main__":
    # Specify your target project directory:
    # Use "." if the script is placed inside the project root directory,
    # or provide an absolute path, e.g., r"C:\Projects\MyQuantProject"
    PROJECT_DIR = "C:/Users/Lenovo/Downloads/Alpha-Mining-Framework-main/Alpha-Mining-Framework-main"
    OUTPUT_FILE = "C:/Users/Lenovo/Downloads/Alpha-Mining-Framework-main/Alpha-Mining-Framework-main/project_codebase.md"

    merge_python_files(PROJECT_DIR, OUTPUT_FILE)

```

----------------------------------------

## File: `run_alpha_miner.py`

```python
"""
WorldQuant Brain Alpha Miner

Simplified alpha mining pipeline using direct API calls.
Supports both Ollama and DeepSeek for LLM-based alpha generation.
"""

import os
import sys
import time
import json
import re
import glob
import queue
import random
import warnings
import threading
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict

# Suppress SSL verification warnings for WorldQuant Brain API
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from core.config import load_credentials
from core.log_manager import setup_logger
from core.alpha_db import get_alpha_db
from core.submission_quota import get_submission_quota
from core.llm_client import get_llm_client, DEFAULT_SYSTEM_PROMPT
from core.notifier import get_notifier

logger = setup_logger(__name__)

# Constants
BASE_URL = "https://api.worldquantbrain.com"
MIN_SHARPE = 1.25
MIN_FITNESS = 1.0
RESCUE_THRESHOLD = 1.7

# Parameter sweep settings for check failures
SETTINGS_SWEEP = [
    {"neutralization": "INDUSTRY", "truncation": 0.1, "decay": 10, "delay": 1},
    {"neutralization": "INDUSTRY", "truncation": 0.1, "decay": 10, "delay": 0},
    {"neutralization": "SUBINDUSTRY", "truncation": 0.15, "decay": 20, "delay": 1},
    {"neutralization": "SUBINDUSTRY", "truncation": 0.15, "decay": 20, "delay": 0},
    {"neutralization": "SECTOR", "truncation": 0.08, "decay": 30, "delay": 0},
    {"neutralization": "SECTOR", "truncation": 0.08, "decay": 30, "delay": 1},
    {"neutralization": "MARKET", "truncation": 0.2, "decay": 50, "delay": 1},
    {"neutralization": "MARKET", "truncation": 0.2, "decay": 50, "delay": 0},
]

# Check failure strategies
CHECK_STRATEGIES = {
    "TURNOVER": {
        "description": "Turnover rate too high",
        "suggestions": [
            "Increase time-series smoothing window: ts_mean(x, 10) → ts_mean(x, 40)",
            "Increase decay: ts_decay_linear(x, 10) → ts_decay_linear(x, 30)",
            "Use ts_rank instead of zscore to reduce turnover",
            "Double outer shell decay: ts_decay_linear(zscore(...), 10) → ts_decay_linear(zscore(...), 20)"
        ]
    },
    "SELF_CORRELATION": {
        "description": "Self-correlation too high",
        "suggestions": [
            "Change neutralization in settings: INDUSTRY → SUBINDUSTRY/SECTOR/MARKET",
            "Increase truncation: truncation=0.08 → truncation=0.15",
            "Use ts_corr(x, adv20, 20) to introduce volume factor"
        ]
    },
    "DRAWDOWN": {
        "description": "Drawdown too large",
        "suggestions": [
            "Use ts_max(x, 60) to limit maximum drawdown",
            "Increase decay smoothing: ts_decay_linear(x, 20)",
            "Use -1 * x to flip signal direction"
        ]
    }
}

# Rescue decision logic - check types
RESCUABLE_CHECKS = ["TURNOVER", "DRAWDOWN", "TURNOVER_RATE"]
NON_RESCUABLE_CHECKS = ["SELF_CORRELATION", "LOW_SUBMISSION_CORRELATION"]

# Data directories
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
FIELDS_DIR_DELAY1 = os.path.join(DATA_DIR, "fields_delay1")
FIELDS_DIR_DELAY0 = os.path.join(DATA_DIR, "fields_delay0")
OPERATORS_DIR = os.path.join(DATA_DIR, "operators")
SHARED_POOL_DIR = os.path.join(DATA_DIR, "shared_pool")

# Failed pattern blacklist file path
FAILED_PATTERNS_FILE = os.path.join(DATA_DIR, "failed_patterns.json")

# Blacklist threshold: skip after the same pattern fails more than this number of times
FAILED_PATTERN_THRESHOLD = 5

# Upper limit for the number of expression operators
MAX_OPERATORS_COUNT = 64


def clean_expression(expr: str) -> str:
    """Fix common LLM mistakes in expressions."""
    import re

    # Fix scientific notation: 1e-6 → 0.000001 (FASTEXPR does not support e notation)
    def _fix_scientific(m: re.Match) -> str:
        try:
            return format(float(m.group(0)), 'f').rstrip('0').rstrip('.')
        except (ValueError, OverflowError):
            return m.group(0)

    expr = re.sub(r'\b\d+\.?\d*e[+-]?\d+\b', _fix_scientific, expr)

    # WorldQuant Brain uses functional logical operators: and(x,y), or(x,y), not(x)
    # Not infix forms: x and y, x & y
    # Keep as-is for now, as simple regex replacement cannot correctly handle nested logic
    # TODO: Add more complex parsing logic to convert infix forms to functional forms
    return expr

# Target field files to load (API-fetched dataset files)
TARGET_FIELD_FILES = [
    "analyst4.csv",
    "fundamental2.csv",
    "fundamental6.csv",
    "model16.csv",
    "model51.csv",
    "model77.csv",
    "news12.csv",
    "news18.csv",
    "option8.csv",
    "option9.csv",
    "pv1.csv",
    "pv13.csv",
    "sentiment1.csv",
    "socialmedia12.csv",
    "socialmedia8.csv",
    "univ1.csv",
]


class AlphaMiner:
    """Main alpha mining engine with direct API approach."""

    def __init__(self, llm_provider: str = "auto", member_id: str = "default",
                 username: str = None, password: str = None,
                 delay0_prob: float = 0.5):
        self.llm_client = get_llm_client(llm_provider)
        self.alpha_db = get_alpha_db()
        self.quota = get_submission_quota()
        self.member_id = member_id
        self.notifier = get_notifier()
        self._username = username
        self._password = password
        self.delay0_prob = delay0_prob

        # Session state
        self.session = None
        self.tested_expressions = set()
        self._token_expires_at = 0  # Token expiration timestamp
        self._auth_retry_count = 0  # Auth retry count

        # Failed pattern blacklist (persisted to file)
        # Format: {pattern_prefix: {"count": N, "last_seen": timestamp, "example": "..."}}
        self._failed_patterns = self._load_failed_patterns()

        # Queues
        self.llm_task_queue = queue.Queue()
        self.test_queue = queue.Queue()

        # Stats
        self.stats = {
            "tested": 0,
            "passed": 0,
            "failed": 0,
            "rescued": 0,
            "flipped": 0,
            "rescue_pool": 0,
            "best_sharpe": -99.0
        }

        # Dynamic module weights (reinforcement learning style)
        self.module_stats = {
            "ANALYST4": {"tried": 0, "success": 0},
            "FUNDAMENTAL2": {"tried": 0, "success": 0},
            "FUNDAMENTAL6": {"tried": 0, "success": 0},
            "MODEL16": {"tried": 0, "success": 0},
            "MODEL51": {"tried": 0, "success": 0},
            "MODEL77": {"tried": 0, "success": 0},
            "NEWS12": {"tried": 0, "success": 0},
            "NEWS18": {"tried": 0, "success": 0},
            "OPTION8": {"tried": 0, "success": 0},
            "OPTION9": {"tried": 0, "success": 0},
            "PV1": {"tried": 0, "success": 0},
            "PV13": {"tried": 0, "success": 0},
            "SENTIMENT1": {"tried": 0, "success": 0},
            "SOCIALMEDIA12": {"tried": 0, "success": 0},
            "SOCIALMEDIA8": {"tried": 0, "success": 0},
            "UNIV1": {"tried": 0, "success": 0},
        }

        # Operator knowledge base
        self.operator_arity = self._load_operator_knowledge()

        # Create directories
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(FIELDS_DIR_DELAY1, exist_ok=True)
        os.makedirs(FIELDS_DIR_DELAY0, exist_ok=True)
        os.makedirs(SHARED_POOL_DIR, exist_ok=True)

    def _load_failed_patterns(self) -> Dict:
        """
        Load failed pattern blacklist from file.
        The blacklist is used to skip expression patterns known to fail, avoiding repeated attempts.
        """
        if not os.path.exists(FAILED_PATTERNS_FILE):
            return {}

        try:
            with open(FAILED_PATTERNS_FILE, "r", encoding="utf-8") as f:
                patterns = json.load(f)
                logger.info(f"Loaded {len(patterns)} failed patterns from blacklist")
                return patterns
        except Exception as e:
            logger.warning(f"Failed to load failed patterns: {e}")
            return {}

    def _save_failed_patterns(self):
        """
        Save failed pattern blacklist to file.
        Persists the blacklist so it remains valid after restart.
        """
        try:
            with open(FAILED_PATTERNS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._failed_patterns, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save failed patterns: {e}")

    def _get_pattern_fingerprint(self, expression: str) -> str:
        """
        Get pattern fingerprint of an expression.
        Uses the first 60 characters of the expression as fingerprint to match similar failed patterns.
        """
        # Remove numerical parameters, simplify expression
        import re
        simplified = re.sub(r'\d+', 'N', expression)
        return simplified[:60]

    def is_pattern_blacklisted(self, expression: str) -> bool:
        """
        Check whether expression is in blacklist.
        Returns True if the same pattern has failed more than threshold times, indicating it should be skipped.
        """
        fingerprint = self._get_pattern_fingerprint(expression)
        pattern_info = self._failed_patterns.get(fingerprint)

        if pattern_info and pattern_info.get("count", 0) >= FAILED_PATTERN_THRESHOLD:
            logger.debug(f"Expression in blacklist (failed {pattern_info['count']} times): {fingerprint}...")
            return True

        return False

    def _load_operator_knowledge(self) -> Dict[str, int]:
        """Load operator arity from operators.csv."""
        import pandas as pd
        operators_file = os.path.join(OPERATORS_DIR, "operators.csv")

        if not os.path.exists(operators_file):
            logger.warning(f"Operators file not found: {operators_file}")
            return {}

        try:
            df = pd.read_csv(operators_file, encoding='utf-8-sig')
            # Extract arity from Definition column (count parameters)
            arity_dict = {}
            for _, row in df.iterrows():
                name = str(row.get('Name', '')).strip()
                definition = str(row.get('Definition', ''))
                # Count parameters: look for pattern like func(x, y, z)
                if '(' in definition:
                    params = definition.split('(')[1].split(')')[0]
                    arity = len([p.strip() for p in params.split(',') if p.strip()])
                    arity_dict[name] = arity
            logger.info(f"Loaded {len(arity_dict)} operators")
            return arity_dict
        except Exception as e:
            logger.warning(f"Failed to load operators: {e}")
            return {}

    def validate_expression_variables(self, expression: str, available_fields: set) -> bool:
        """Verify whether all variables in the expression exist in the list of available fields"""
        # Known operators and built-in variables
        known_operators = set(self.operator_arity.keys()) if self.operator_arity else set()
        builtin_vars = {
            'returns', 'volume', 'close', 'open', 'high', 'low', 'vwap',
            'adv20', 'adv50', 'adv120', 'adv240',
            'market_cap', 'sector', 'industry', 'subindustry',
            'liabilities', 'assets', 'equity', 'debt_lt', 'debt_st',
        }

        # Extract all identifiers (variable names) in expression
        # Match identifiers starting with letters and containing letters, digits, underscores
        identifiers = set(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', expression))

        # Filter out operators and built-in variables
        potential_fields = identifiers - known_operators - builtin_vars

        # Check if each potential field is in the available fields list
        unknown_fields = []
        for field in potential_fields:
            # Skip identifiers starting with digits (possibly constants)
            if field[0].isdigit():
                continue
            # Skip common non-field identifiers
            if field in {'true', 'false', 'null', 'nan', 'inf', 'e', 'pi'}:
                continue
            if field not in available_fields:
                unknown_fields.append(field)

        if unknown_fields:
            logger.warning(f"Expression contains unknown fields: {unknown_fields}")
            return False

        return True

    def authenticate(self) -> bool:
        """Authenticate with WorldQuant Brain API."""
        import requests
        from requests.auth import HTTPBasicAuth
        import time

        username, password = load_credentials()

        logger.info("Authenticating with WorldQuant Brain...")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        })
        self.session.auth = HTTPBasicAuth(username, password)

        try:
            resp = self.session.post(
                f"{BASE_URL}/authentication",
                verify=False,
                timeout=15
            )
            if resp.status_code == 201:
                logger.info("Authentication successful")
                # Set token expiration time (assuming 2 hours, may actually be shorter)
                self._token_expires_at = time.time() + 7200  # 2 hours
                self._auth_retry_count = 0  # Reset retry count
                return True
            else:
                logger.error(f"Authentication failed: {resp.text}")
                self._auth_retry_count += 1
                return False
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            self._auth_retry_count += 1
            return False

    def _is_token_expired(self) -> bool:
        """Check if token is about to expire (5 minutes in advance)"""
        import time
        return time.time() > (self._token_expires_at - 300)  # 5 minutes in advance

    def _ensure_authenticated(self) -> bool:
        """Ensure authentication is valid; re-authenticate if about to expire"""
        if self._is_token_expired():
            logger.info("Token about to expire, re-authenticating...")
            return self.authenticate()
        return True

    # ==========================================
    # Field Loading (from CSV files)
    # ==========================================

    def load_fields_from_csvs(self, fields_dir: str = None) -> Dict[str, List[Dict]]:
        """Load ALL field data from specified CSV files (full pool for sampling)."""
        import pandas as pd

        # Default to delay1 directory
        if fields_dir is None:
            fields_dir = FIELDS_DIR_DELAY1

        all_fields = {}

        if not os.path.exists(fields_dir):
            logger.info(f"Fields directory not found: {fields_dir}")
            return self._get_default_fields()

        # Get all CSV files in directory
        csv_files = [f for f in os.listdir(fields_dir) if f.endswith('.csv')]

        for file in csv_files:
            filepath = os.path.join(fields_dir, file)
            try:
                df = pd.read_csv(filepath)
                if 'Field' in df.columns and 'Description' in df.columns:
                    # Select required columns
                    columns_to_keep = ['Field', 'Description']
                    if 'Type' in df.columns:
                        columns_to_keep.append('Type')
                    if 'Alphas' in df.columns:
                        columns_to_keep.append('Alphas')

                    fields = df[columns_to_keep].to_dict(orient='records')

                    # Fill default values
                    for field in fields:
                        if 'Type' not in field:
                            field['Type'] = 'MATRIX'
                        if 'Alphas' not in field:
                            field['Alphas'] = 0

                    category = file.replace(".csv", "").upper()
                    all_fields[category] = fields
                    logger.info(f"Loaded {len(fields)} fields from {file}")
            except Exception as e:
                logger.warning(f"Failed to load {file}: {e}")

        if not all_fields:
            logger.info("No target CSV files found, using default fields")
            all_fields = self._get_default_fields()

        return all_fields

    def _get_default_fields(self) -> Dict[str, List[Dict]]:
        """Get default field data for alpha generation."""
        return {
            "PV13": [
                {"Field": "close", "Description": "Closing price"},
                {"Field": "open", "Description": "Opening price"},
                {"Field": "high", "Description": "Highest price"},
                {"Field": "low", "Description": "Lowest price"},
                {"Field": "volume", "Description": "Volume"},
                {"Field": "returns", "Description": "Returns"},
                {"Field": "vwap", "Description": "Volume-weighted average price"}
            ],
            "FUNDAMENTAL6": [
                {"Field": "market_cap", "Description": "Market capitalization"},
                {"Field": "pe_ratio", "Description": "P/E ratio"},
                {"Field": "pb_ratio", "Description": "P/B ratio"},
                {"Field": "roe", "Description": "Return on equity"}
            ]
        }

    # ==========================================
    # Dynamic Module Weights
    # ==========================================

    @staticmethod
    def log_minmax_softmax(values: list, temperature: float = 0.12) -> list:
        """Log + MinMax + Softmax weight transformation.

        1. Log(1+x) compresses extreme values
        2. MinMax normalizes to [0, 1]
        3. Softmax with temperature
        """
        import math
        log_values = [math.log1p(v) for v in values]
        min_val = min(log_values)
        max_val = max(log_values)
        if max_val - min_val == 0:
            return [1.0 / len(values)] * len(values)
        normalized = [(x - min_val) / (max_val - min_val) for x in log_values]
        scaled = [x / temperature for x in normalized]
        max_scaled = max(scaled)
        exp_values = [math.exp(x - max_scaled) for x in scaled]
        sum_exp = sum(exp_values)
        return [v / sum_exp for v in exp_values]

    def get_dynamic_modules(self, fields_pool: Dict, sample_size: int = 15) -> tuple:
        """
        Select 1-2 modules using Log+MinMax+Softmax weighting,
        then sample fields using the same method.
        Returns (selected_fields, modules_used)
        """
        # Calculate total alpha count for each dataset
        module_alpha_counts = {}
        for mod, fields in fields_pool.items():
            total_alphas = sum(f.get('Alphas', 0) for f in fields)
            module_alpha_counts[mod] = total_alphas

        # Calculate dataset weights using Log+MinMax+Softmax
        modules = list(fields_pool.keys())
        raw_counts = [module_alpha_counts.get(mod, 0) for mod in modules]

        if sum(raw_counts) == 0:
            weights = [1.0] * len(modules)
        else:
            weights = self.log_minmax_softmax(raw_counts, temperature=0.12)

        # Low success rate module weight adjustment: lower selection probability for modules with success rate remaining 0 for N consecutive times
        # Maintain exploration, but avoid wasting resources on modules with long-term failure
        LOW_SUCCESS_THRESHOLD = 30  # Consecutive attempt threshold
        LOW_SUCCESS_PENALTY = 0.1   # Weight reduced to 10% of original

        for i, mod in enumerate(modules):
            if mod in self.module_stats:
                tried = self.module_stats[mod]['tried']
                success = self.module_stats[mod]['success']

                # Reduce weight if consecutive attempts exceed threshold and success rate is 0
                if tried >= LOW_SUCCESS_THRESHOLD and success == 0:
                    weights[i] *= LOW_SUCCESS_PENALTY
                    logger.debug(f"Module {mod} weight reduced (tried={tried}, success={success})")

        # Select 1 or 2 modules
        num_to_select = random.choice([1, 2])
        selected = random.choices(modules, weights=weights, k=num_to_select)
        selected = list(set(selected))

        # Within selected datasets, select fields using Log+MinMax+Softmax
        selected_fields = {}
        for mod in selected:
            pool = fields_pool.get(mod, [])
            if not pool:
                continue

            raw_weights = [f.get('Alphas', 0) for f in pool]
            if sum(raw_weights) == 0:
                field_weights = [1.0] * len(pool)
            else:
                field_weights = self.log_minmax_softmax(raw_weights, temperature=0.12)

            n = min(sample_size, len(pool))
            selected_fields[mod] = random.choices(pool, weights=field_weights, k=n)

        return selected_fields, selected

    def record_module_stat(self, modules_used: List[str], success: bool):
        """Record success/failure for module weight updates."""
        for mod in modules_used:
            if mod in self.module_stats:
                self.module_stats[mod]['tried'] += 1
                if success:
                    self.module_stats[mod]['success'] += 1

    # ==========================================
    # Shared Pool Management
    # ==========================================

    def load_shared_pool(self) -> List[Dict]:
        """Load and merge all shared pool files."""
        combined_pool = []
        search_pattern = os.path.join(SHARED_POOL_DIR, "shared_pool_*.json")

        for file_path in glob.glob(search_pattern):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    combined_pool.extend(json.load(f))
            except Exception:
                continue

        # Sort by Sharpe and keep top 500
        combined_pool.sort(key=lambda x: x.get('sharpe', 0), reverse=True)
        return combined_pool[:500]

    def add_to_shared_pool(self, expression: str, sharpe: float, fitness: float, logic: str = ""):
        """Add factor to member's shared pool file."""
        my_pool_path = os.path.join(SHARED_POOL_DIR, f"shared_pool_{self.member_id}.json")

        my_pool = []
        if os.path.exists(my_pool_path):
            try:
                with open(my_pool_path, "r", encoding="utf-8") as f:
                    my_pool = json.load(f)
            except Exception:
                pass

        my_pool.append({
            "expression": expression,
            "sharpe": sharpe,
            "fitness": fitness,
            "logic": logic
        })

        # Sort and keep top 500
        my_pool.sort(key=lambda x: x.get('sharpe', 0), reverse=True)
        my_pool = my_pool[:500]

        with open(my_pool_path, "w", encoding="utf-8") as f:
            json.dump(my_pool, f, ensure_ascii=False, indent=2)

    # ==========================================
    # Alpha Generation
    # ==========================================

    def generate_alphas(self, fields_data: Dict = None) -> List[Dict]:
        """Generate alpha expressions using LLM with dynamic module selection."""
        # Select delay0 or delay1 fields based on probability
        if random.random() < self.delay0_prob:
            fields_data = self.fields_delay0
            delay = 0
            logger.info("Selected delay=0 fields")
        else:
            fields_data = self.fields_delay1
            delay = 1
            logger.info("Selected delay=1 fields")

        # Use dynamic module selection
        target_fields, modules_used = self.get_dynamic_modules(fields_data)
        mod_names = "+".join(modules_used)

        logger.info(f"Generating alphas for modules: {mod_names}")

        # Separate MATRIX and VECTOR fields
        matrix_fields = {}
        vector_fields = {}
        for mod, fields in target_fields.items():
            matrix_list = [f for f in fields if f.get('Type', 'MATRIX') == 'MATRIX']
            vector_list = [f for f in fields if f.get('Type', 'MATRIX') == 'VECTOR']
            if matrix_list:
                matrix_fields[mod] = matrix_list
            if vector_list:
                vector_fields[mod] = vector_list

        # Build field descriptions
        field_description = "【MATRIX FIELDS】(Can normally use all operators):\n"
        field_description += json.dumps(matrix_fields, ensure_ascii=False)

        if vector_fields:
            field_description += "\n\n【⚠️ FORBIDDEN VECTOR FIELDS — WQ API rejects all operators】:\n"
            field_description += json.dumps(vector_fields, ensure_ascii=False)
            field_description += """
VECTOR fields are strictly forbidden:
- ❌ == != > < >= <= all error "does not support event inputs"
- ❌ sign(), trade_when(), rank(), zscore() all error
- ❌ Cannot participate in any arithmetic operations (+,-,*,/)
- ❌ Cannot be used in ts_delta, ts_mean, ts_sum, if_else, or anywhere else
- ⚠️ Only use the MATRIX fields above! VECTOR fields are listed only so you know NOT to use them!"""

        prompt = f"""Please use the provided data fields below to generate 5 explosive new factors.

【CRITICAL RULES】
1. You MUST ONLY use the field names listed below, absolutely NO other field names!
2. Do NOT use non-existent fields like total_assets, book_value_per_share, return_on_equity, etc.
3. If total assets are needed, use assets; if equity is needed, use equity; if long-term debt is needed, use debt_lt
4. Logical operators MUST use functional syntax: and(x,y), or(x,y), not(x); infix forms or & | ~ are FORBIDDEN
5. The current data field delay is delay={delay}, delay in settings for all generated factors MUST be set to {delay}

【AVOID COMMON FAILURE PATTERNS】
- High turnover (HIGH_TURNOVER): Use a larger decay window (ts_decay_linear(x, 20) or larger) to avoid overly sensitive signals
- Concentrated weights (CONCENTRATED_WEIGHT): Use zscore or rank for normalization to avoid extreme weights
- Low sub-universe Sharpe (LOW_SUB_UNIVERSE_SHARPE): Use industry or sector neutralization to avoid over-reliance on specific stocks

【RECOMMENDED FACTOR STRUCTURE】
1. Outer shell: ts_decay_linear(zscore(...), 20)  # Use larger decay window to reduce turnover
2. Core logic: Use normalization functions like rank, zscore, ts_mean
3. Neutralization: Set neutralization: "INDUSTRY" or "SUBINDUSTRY" in settings
4. Truncation: Set truncation: 0.08 or 0.1 in settings

{field_description}"""

        results = self.llm_client.generate_alphas(DEFAULT_SYSTEM_PROMPT, prompt)

        if results:
            self.notifier.record_llm_success()
        else:
            self.notifier.record_llm_error()

        # Build available field set (for validation)
        available_fields = set()
        for mod, fields in target_fields.items():
            for field in fields:
                available_fields.add(field['Field'])

        # Clean expressions, validate variables, and tag with modules used
        valid_results = []
        for res in results:
            expression = res.get('expression', '')
            if not expression:
                continue

            # Validate variables
            if not self.validate_expression_variables(expression, available_fields):
                logger.warning(f"Skipping expression with unknown variables: {expression[:60]}...")
                continue

            # Validate expression quality
            if not self._validate_expression_quality(expression):
                logger.warning(f"Skipping low quality expression: {expression[:60]}...")
                continue

            res['expression'] = clean_expression(expression)
            res['modules_used'] = modules_used
            res['delay'] = delay  # Add delay value
            valid_results.append(res)

        return valid_results

    def _count_operators(self, expression: str) -> int:
        """
        Count the number of operators in the expression.
        Operators include: function calls, brackets, commas, etc.
        Uses a simplified counting method: count of left parentheses + count of commas.
        """
        # Count left parentheses (each left parenthesis represents a function call or sub-expression)
        left_parens = expression.count('(')

        # Count commas (each comma represents a parameter delimiter)
        commas = expression.count(',')

        # Operator count roughly equals left parens + commas
        # This is a simplified calculation, but sufficient for filtering overly long expressions
        return left_parens + commas

    def _validate_expression_quality(self, expression: str) -> bool:
        """
        Validate expression quality to filter out low-quality expressions early.
        Returns True if quality is qualified, False if it should be skipped.
        """
        # Check expression length (too short may indicate incomplete logic)
        if len(expression) < 20:
            return False

        # Check if operator count exceeds limit
        operator_count = self._count_operators(expression)
        if operator_count > MAX_OPERATORS_COUNT:
            logger.warning(f"Expression with {operator_count} operators exceeds limit of {MAX_OPERATORS_COUNT}: {expression[:60]}...")
            return False

        # Check if basic normalization functions are included
        has_normalization = any(func in expression for func in ['zscore', 'rank', 'ts_rank', 'ts_zscore'])
        if not has_normalization:
            return False

        # Check if time-series smoothing is included
        has_smoothing = any(func in expression for func in ['ts_decay_linear', 'ts_mean', 'ts_std_dev'])
        if not has_smoothing:
            return False

        # Check for excessive nesting (which may cause high computational complexity)
        nesting_depth = 0
        max_nesting = 0
        for char in expression:
            if char == '(':
                nesting_depth += 1
                max_nesting = max(max_nesting, nesting_depth)
            elif char == ')':
                nesting_depth -= 1

        # If nesting depth exceeds 8 levels, it may be overly complex
        if max_nesting > 8:
            return False

        # Check for common error patterns
        error_patterns = [
            'ts_min',  # Non-existent function
            'ts_max',  # Non-existent function
            'group_neutralize',  # Should not be in expression
            '"',  # Quotes should not exist
            "'",  # Quotes should not exist
        ]

        for pattern in error_patterns:
            if pattern in expression:
                return False

        return True

    def _record_failed_pattern(self, expression: str, failed_checks: list):
        """
        Record failed pattern to blacklist to prevent generating similar expressions repeatedly.
        Once a pattern fails more than the threshold times, it will be skipped and no longer attempted.
        """
        import time

        # Get pattern key using fingerprint method
        pattern_key = self._get_pattern_fingerprint(expression)

        if pattern_key not in self._failed_patterns:
            self._failed_patterns[pattern_key] = {
                "count": 0,
                "checks": [],
                "example": expression[:80],
                "last_seen": time.time()
            }

        self._failed_patterns[pattern_key]["count"] += 1
        self._failed_patterns[pattern_key]["last_seen"] = time.time()

        # Update failure check types (convert to list to support JSON serialization)
        existing_checks = set(self._failed_patterns[pattern_key].get("checks", []))
        existing_checks.update(failed_checks)
        self._failed_patterns[pattern_key]["checks"] = list(existing_checks)

        # Log warning
        count = self._failed_patterns[pattern_key]["count"]
        if count >= FAILED_PATTERN_THRESHOLD:
            logger.warning(f"Pattern added to blacklist (failed {count} times): {pattern_key}... "
                           f"checks: {failed_checks}")
            # Save blacklist to file
            self._save_failed_patterns()
        elif count >= 3:
            logger.warning(f"Repeated failure pattern detected: {pattern_key}... "
                           f"(failed {count} times, checks: {failed_checks})")

    def generate_crossover_alphas(self) -> List[Dict]:
        """Generate alphas by crossing over elite factors from shared pool using non-linear operations."""
        pool = self.load_shared_pool()

        if len(pool) < 2:
            logger.info("Not enough factors in shared pool for crossover")
            return []

        # Select 2 random elite parents
        parents = random.sample(pool, 2)

        logger.info("Generating crossover from elite parents (non-linear)...")

        prompt = f"""Two submitted factors; linear combinations are FORBIDDEN (cannot pass correlation checks).

【STEPS】
1. Extract core logic of parent factors (strip outer shell ts_decay_linear(zscore(...)))
2. Hybridize using the following non-linear methods:
   - ts_corr(core_A, core_B, d): compute time-series correlation
   - ts_cov(core_A, core_B, d): compute time-series covariance
   - rank(core_A) / rank(core_B): rank ratio
   - sign(core_A) * abs(core_B): sign combination
   - Swap data fields: retain Parent A structure, replace with Parent B fields
   - Swap operators: ts_mean↔ts_std_dev, ts_rank↔ts_zscore
3. Re-apply outer shell

【PARENT FACTORS】
Parent A: {parents[0]['expression']}
Parent B: {parents[1]['expression']}

【OUTPUT REQUIREMENTS】
Output 3 variants, outer shell MUST be: ts_decay_linear(zscore(...), 10)
Neutralization is controlled by settings; do NOT include group_neutralize in the expression"""

        results = self.llm_client.generate_alphas(DEFAULT_SYSTEM_PROMPT, prompt)

        if results:
            self.notifier.record_llm_success()
        else:
            self.notifier.record_llm_error()

        # Clean expressions, validate quality, and tag as crossover
        valid_results = []
        for res in results:
            expression = res.get('expression', '')
            if not expression:
                continue

            # Validate expression quality
            if not self._validate_expression_quality(expression):
                logger.warning(f"Skipping low quality crossover expression: {expression[:60]}...")
                continue

            res['expression'] = clean_expression(expression)
            res['modules_used'] = []  # Crossover doesn't count for module stats
            valid_results.append(res)

        return valid_results

    # ==========================================
    # Simulation & Polling
    # ==========================================

    def simulate_factor(self, factor: Dict) -> Dict:
        """Submit factor for backtesting and poll for results."""
        # Ensure authentication is valid
        if not self._ensure_authenticated():
            return {"error": "AUTH_FAILED"}

        if not self.session:
            return {"error": "Not authenticated"}

        expression = factor.get("expression", "")
        if not expression:
            return {"error": "Empty expression"}

        # Build simulation settings
        settings = {
            "instrumentType": "EQUITY",
            "region": "USA",
            "universe": "TOP3000",
            "delay": factor.get("delay", 1),  # Use delay value from factor
            "decay": 0,
            "neutralization": "INDUSTRY",
            "truncation": 0.08,
            "pasteurization": "ON",
            "unitHandling": "VERIFY",
            "nanHandling": "OFF",
            "language": "FASTEXPR",
            "visualization": False
        }

        # Apply custom settings if provided (delay is system controlled, not overridden by LLM)
        actual_delay = factor.get("delay", 1)
        if isinstance(factor.get("settings"), dict):
            for k, v in factor["settings"].items():
                if k in settings and k != "delay":
                    settings[k] = v
        # Ensure again delay uses system-set value, not overridden by LLM output settings
        settings["delay"] = actual_delay

        payload = {
            "type": "REGULAR",
            "s

```

----------------------------------------

## File: `submit_alpha.py`

```python
"""
Submit alpha to WorldQuant Brain by ID.

Usage:
    python submit_alpha.py <alpha_id>
    python submit_alpha.py pwnbR9Gq akNmojM1
"""

import os
import sys
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import requests
from requests.auth import HTTPBasicAuth
from core.config import load_credentials
from core.alpha_db import get_alpha_db


def submit_alpha(session: requests.Session, alpha_id: str) -> dict:
    """Submit alpha to WorldQuant Brain."""
    resp = session.post(
        f'https://api.worldquantbrain.com/alphas/{alpha_id}/submit',
        json={"type": "REGULAR"},
        verify=False,
        timeout=15
    )

    if resp.status_code == 201:
        return {"success": True}
    else:
        # Parse error response to get detailed check failures
        try:
            error_data = resp.json()
            checks = error_data.get("is", {}).get("checks", [])
            failed_checks = [c for c in checks if c.get("result") == "FAIL"]

            if failed_checks:
                details = []
                for check in failed_checks:
                    name = check.get("name", "UNKNOWN")
                    value = check.get("value", "N/A")
                    limit = check.get("limit", "N/A")
                    details.append(f"{name}: value={value}, limit={limit}")
                return {"success": False, "error": "Checks failed", "details": details}
            else:
                error_msg = resp.text[:200] if resp.text else "Unknown error"
                return {"success": False, "error": error_msg}
        except:
            error_msg = resp.text[:200] if resp.text else "Unknown error"
            return {"success": False, "error": error_msg}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ['-h', '--help']:
        print(__doc__)
        sys.exit(0)

    # Authenticate
    username, password = load_credentials()
    session = requests.Session()
    session.auth = HTTPBasicAuth(username, password)
    resp = session.post('https://api.worldquantbrain.com/authentication', verify=False, timeout=15)

    if resp.status_code != 201:
        print(f"Authentication failed: {resp.text}")
        sys.exit(1)

    print("Authentication successful\n")

    db = get_alpha_db()
    alpha_ids = sys.argv[1:]

    for alpha_id in alpha_ids:
        # Check if already submitted
        with db._cursor() as cur:
            cur.execute("SELECT status FROM alphas WHERE alpha_id = ?", (alpha_id,))
            row = cur.fetchone()
            if row and row["status"] == "submitted":
                print(f"Submitting alpha {alpha_id}...")
                print(f"  ✗ Already submitted, skipping")
                continue

        print(f"Submitting alpha {alpha_id}...")
        result = submit_alpha(session, alpha_id)

        if result["success"]:
            print(f"  ✓ Submitted successfully")
            # Update status in database
            rows = db.update_alpha_status(alpha_id, "submitted")
            if rows > 0:
                print(f"  ✓ Database status updated")
            else:
                print(f"  ⚠ Alpha not found in database")
        else:
            print(f"  ✗ Submission failed: {result['error']}")
            # Show detailed check failures
            if "details" in result:
                for detail in result["details"]:
                    print(f"    - {detail}")
            # Delete alpha from database if submission failed
            rows = db.delete_alpha_by_alpha_id(alpha_id)
            if rows > 0:
                print(f"  ✓ Alpha deleted from database")

    # Summary
    print(f"\nDone. Total alphas in database: {db.count_alphas()}")


if __name__ == "__main__":
    main()

```

----------------------------------------

## File: `core\alpha_db.py`

```python
"""
Alpha Database — SQLite-backed storage for all alpha backtesting results.
"""

import sqlite3
import json
import os
import uuid
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.path.join("data", "alphas.db")


class AlphaDB:
    """Thread-safe SQLite storage for alpha results."""

    _local = threading.local()

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, timeout=60.0)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    @contextmanager
    def _cursor(self):
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_db(self):
        with self._cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS alphas (
                    alpha_id TEXT PRIMARY KEY,
                    status TEXT DEFAULT 'tested',
                    grade TEXT,
                    expression TEXT NOT NULL,
                    fitness REAL,
                    sharpe REAL,
                    turnover REAL,
                    returns REAL,
                    margin REAL,
                    long_count INTEGER,
                    short_count INTEGER,
                    drawdown REAL,
                    source TEXT DEFAULT 'pipeline',
                    region TEXT DEFAULT 'USA',
                    universe TEXT DEFAULT 'TOP3000',
                    delay INTEGER DEFAULT 1,
                    decay INTEGER DEFAULT 0,
                    neutralization TEXT DEFAULT 'INDUSTRY',
                    truncation REAL DEFAULT 0.08,
                    checks TEXT DEFAULT '[]',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_alpha_expression ON alphas(expression, region, universe, neutralization)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_alpha_fitness ON alphas(fitness)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_alpha_sharpe ON alphas(sharpe)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_alpha_source ON alphas(source)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_alpha_created ON alphas(created_at)"
            )

            # Rescue pool for borderline alphas
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rescue_pool (
                    alpha_id TEXT PRIMARY KEY,
                    expression TEXT NOT NULL,
                    sharpe REAL,
                    fitness REAL,
                    turnover REAL,
                    failed_checks TEXT DEFAULT '[]',
                    modules_used TEXT DEFAULT '[]',
                    attempt_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    # ── Write operations ─────────────────────────────────────────────

    def add_alpha(
        self,
        expression: str,
        sharpe: float = None,
        fitness: float = None,
        alpha_id: str = "",
        turnover: float = None,
        margin: float = None,
        returns: float = None,
        long_count: int = None,
        short_count: int = None,
        drawdown: float = None,
        grade: str = None,
        checks: list = None,
        source: str = "pipeline",
        region: str = "USA",
        universe: str = "TOP3000",
        delay: int = 1,
        decay: int = 0,
        neutralization: str = "NONE",
        truncation: float = 0.08,
        status: str = "tested",
    ) -> int:
        """Save an alpha result. Returns 1 if successful."""
        if not alpha_id:
            alpha_id = str(uuid.uuid4())[:8]

        checks_json = json.dumps(checks) if checks else "[]"
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO alphas (
                    alpha_id, expression, fitness, sharpe, turnover,
                    margin, returns, long_count, short_count,
                    drawdown, grade, checks, source, region, universe,
                    delay, decay, neutralization, truncation, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(alpha_id) DO UPDATE SET
                    fitness=excluded.fitness,
                    sharpe=excluded.sharpe,
                    turnover=excluded.turnover,
                    margin=excluded.margin,
                    returns=excluded.returns,
                    long_count=excluded.long_count,
                    short_count=excluded.short_count,
                    drawdown=excluded.drawdown,
                    grade=excluded.grade,
                    checks=excluded.checks,
                    status=excluded.status,
                    created_at=CURRENT_TIMESTAMP
                """,
                (
                    alpha_id, expression, fitness, sharpe, turnover,
                    margin, returns, long_count, short_count,
                    drawdown, grade, checks_json, source, region, universe,
                    delay, decay, neutralization, truncation, status
                ),
            )
            return 1

    def save_alpha(
        self,
        expression: str,
        alpha_data: Dict,
        source: str = "pipeline",
        settings: Dict = None,
    ) -> int:
        """Save an alpha result from API response. Returns the row ID."""
        is_data = alpha_data.get("is", {})
        api_settings = alpha_data.get("settings", {})
        if settings is None:
            settings = api_settings or {}

        region = settings.get("region", "USA")

        return self.add_alpha(
            expression=expression,
            sharpe=is_data.get("sharpe"),
            fitness=is_data.get("fitness"),
            alpha_id=alpha_data.get("id", ""),
            turnover=is_data.get("turnover"),
            margin=is_data.get("margin"),
            source=source,
            region=region,
            universe=settings.get("universe", "TOP3000"),
            neutralization=settings.get("neutralization", "INDUSTRY"),
        )

    # ── Read operations ──────────────────────────────────────────────

    def get_successful_alphas(
        self,
        min_fitness: float = 1.0,
        min_sharpe: float = 1.25,
        limit: int = 100,
    ) -> List[Dict]:
        """Get successful alphas sorted by fitness."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT * FROM alphas
                WHERE fitness >= ? AND sharpe >= ?
                ORDER BY fitness DESC
                LIMIT ?
                """,
                (min_fitness, min_sharpe, limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_top_alphas(self, limit: int = 20, days: int = 7) -> List[Dict]:
        """Get top alphas from the last N days."""
        since = (datetime.now() - timedelta(days=days)).isoformat()
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT * FROM alphas
                WHERE created_at >= ? AND fitness IS NOT NULL
                ORDER BY fitness DESC
                LIMIT ?
                """,
                (since, limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_all_alphas(self, limit: int = 10000) -> List[Dict]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM alphas ORDER BY created_at DESC LIMIT ?", (limit,))
            return [dict(row) for row in cur.fetchall()]

    def count_alphas(self, days: int = None) -> int:
        with self._cursor() as cur:
            if days:
                since = (datetime.now() - timedelta(days=days)).isoformat()
                cur.execute("SELECT COUNT(*) FROM alphas WHERE created_at >= ?", (since,))
            else:
                cur.execute("SELECT COUNT(*) FROM alphas")
            return cur.fetchone()[0]

    def expression_exists(self, expression: str, region: str = "USA", universe: str = None, neutralization: str = None) -> bool:
        """Check if an expression has already been tested with specific settings."""
        with self._cursor() as cur:
            query = "SELECT 1 FROM alphas WHERE expression = ? AND region = ?"
            params = [expression, region]
            if universe:
                query += " AND universe = ?"
                params.append(universe)
            if neutralization:
                query += " AND neutralization = ?"
                params.append(neutralization)
            query += " LIMIT 1"
            cur.execute(query, tuple(params))
            return cur.fetchone() is not None

    def delete_alpha_by_expression(
        self,
        expression: str,
        region: str = None,
        universe: str = None,
        neutralization: str = None,
        force: bool = False,
    ) -> int:
        """Delete alpha records matching an expression and optional settings.
        Will not delete submitted alphas unless force=True."""
        with self._cursor() as cur:
            if not force:
                # First check if any matching alphas are submitted
                check_query = "SELECT COUNT(*) as cnt FROM alphas WHERE expression = ? AND status = 'submitted'"
                check_params = [expression]
                if region is not None:
                    check_query += " AND region = ?"
                    check_params.append(region)
                if universe is not None:
                    check_query += " AND universe = ?"
                    check_params.append(universe)
                if neutralization is not None:
                    check_query += " AND neutralization = ?"
                    check_params.append(neutralization)
                cur.execute(check_query, tuple(check_params))
                if cur.fetchone()["cnt"] > 0:
                    logger.warning(f"Cannot delete expression with submitted alphas: {expression}")
                    return 0

            query = "DELETE FROM alphas WHERE expression = ?"
            params = [expression]

            if region is not None:
                query += " AND region = ?"
                params.append(region)
            if universe is not None:
                query += " AND universe = ?"
                params.append(universe)
            if neutralization is not None:
                query += " AND neutralization = ?"
                params.append(neutralization)

            cur.execute(query, tuple(params))
            return cur.rowcount

    def update_alpha_status(self, alpha_id: str, status: str) -> int:
        """Update alpha status by alpha_id. Returns number of rows updated."""
        with self._cursor() as cur:
            cur.execute("UPDATE alphas SET status = ? WHERE alpha_id = ?", (status, alpha_id))
            return cur.rowcount

    def update_alpha_checks(self, alpha_id: str, checks: list) -> int:
        """Update alpha checks by alpha_id. Returns number of rows updated."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE alphas SET checks = ? WHERE alpha_id = ?",
                (json.dumps(checks), alpha_id)
            )
            return cur.rowcount

    def delete_alpha_by_alpha_id(self, alpha_id: str, force: bool = False) -> int:
        """Delete alpha by alpha_id. Returns number of rows deleted.
        Will not delete submitted alphas unless force=True."""
        with self._cursor() as cur:
            if not force:
                # Check if alpha is submitted
                cur.execute("SELECT status FROM alphas WHERE alpha_id = ?", (alpha_id,))
                row = cur.fetchone()
                if row and row["status"] == "submitted":
                    logger.warning(f"Cannot delete submitted alpha: {alpha_id}")
                    return 0
            cur.execute("DELETE FROM alphas WHERE alpha_id = ?", (alpha_id,))
            return cur.rowcount

    # ── Rescue Pool operations ───────────────────────────────────────

    def add_to_rescue_pool(
        self,
        alpha_id: str,
        expression: str,
        sharpe: float = 0,
        fitness: float = 0,
        turnover: float = 0,
        failed_checks: list = None,
        modules_used: list = None,
    ) -> int:
        """Add a borderline alpha to rescue pool. Returns 1 if successful."""
        checks_json = json.dumps(failed_checks) if failed_checks else "[]"
        modules_json = json.dumps(modules_used) if modules_used else "[]"
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO rescue_pool (alpha_id, expression, sharpe, fitness, turnover, failed_checks, modules_used)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(alpha_id) DO UPDATE SET
                    expression=excluded.expression,
                    sharpe=excluded.sharpe,
                    fitness=excluded.fitness,
                    turnover=excluded.turnover,
                    failed_checks=excluded.failed_checks,
                    modules_used=excluded.modules_used,
                    attempt_count=0,
                    created_at=CURRENT_TIMESTAMP
                """,
                (alpha_id, expression, sharpe, fitness, turnover, checks_json, modules_json),
            )
            return 1

    def get_rescue_candidate(self) -> Optional[Dict]:
        """Get a rescue candidate with attempt_count < 3. Returns None if pool is empty."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT * FROM rescue_pool
                WHERE attempt_count < 3
                ORDER BY sharpe DESC, fitness DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row:
                result = dict(row)
                # Parse JSON fields
                result["failed_checks"] = json.loads(result.get("failed_checks", "[]"))
                result["modules_used"] = json.loads(result.get("modules_used", "[]"))
                return result
            return None

    def increment_rescue_attempt(self, alpha_id: str) -> int:
        """Increment attempt_count for a rescue candidate. Returns number of rows updated."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE rescue_pool SET attempt_count = attempt_count + 1 WHERE alpha_id = ?",
                (alpha_id,),
            )
            return cur.rowcount

    def delete_from_rescue_pool(self, alpha_id: str) -> int:
        """Delete alpha from rescue pool. Returns number of rows deleted."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM rescue_pool WHERE alpha_id = ?", (alpha_id,))
            return cur.rowcount

    def cleanup_rescue_pool(self) -> int:
        """Delete all rescue candidates with attempt_count >= 3. Returns number of rows deleted."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM rescue_pool WHERE attempt_count >= 3")
            return cur.rowcount

    def count_rescue_pool(self) -> int:
        """Count rescue candidates with attempt_count < 3."""
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM rescue_pool WHERE attempt_count < 3")
            return cur.fetchone()[0]

    # ── Analytics ────────────────────────────────────────────────────

    def get_operator_stats(self, days: int = 7) -> List[Dict]:
        """Get operator performance statistics."""
        alphas = self.get_top_alphas(limit=500, days=days)

        import re
        op_stats: Dict[str, List[float]] = {}
        for alpha in alphas:
            expr = alpha.get("expression", "")
            fitness = alpha.get("fitness")
            if fitness is None:
                continue
            ops = re.findall(r"\b(ts_\w+|group_\w+|rank|zscore|log|sqrt|abs|sign|scale)\b", expr)
            for op in set(ops):
                if op not in op_stats:
                    op_stats[op] = []
                op_stats[op].append(fitness)

        result = []
        for op, fitnesses in op_stats.items():
            result.append({
                "operator": op,
                "count": len(fitnesses),
                "avg_fitness": round(sum(fitnesses) / len(fitnesses), 4),
                "max_fitness": round(max(fitnesses), 4),
                "success_count": sum(1 for f in fitnesses if f >= 1.0),
            })
        return sorted(result, key=lambda x: x["avg_fitness"], reverse=True)

    def get_field_stats(self, days: int = 7) -> List[Dict]:
        """Get field performance statistics."""
        alphas = self.get_top_alphas(limit=500, days=days)

        import re
        field_stats: Dict[str, List[float]] = {}
        for alpha in alphas:
            expr = alpha.get("expression", "")
            fitness = alpha.get("fitness")
            if fitness is None:
                continue
            fields = re.findall(r"[a-z][a-z0-9_]*(?:_[a-z0-9_]+)+", expr, re.IGNORECASE)
            non_ops = [
                f for f in fields
                if not any(f.startswith(p) for p in ["ts_", "group_", "vec_"])
            ]
            for field in set(non_ops):
                if field not in field_stats:
                    field_stats[field] = []
                field_stats[field].append(fitness)

        result = []
        for field, fitnesses in field_stats.items():
            result.append({
                "field": field,
                "count": len(fitnesses),
                "avg_fitness": round(sum(fitnesses) / len(fitnesses), 4),
                "max_fitness": round(max(fitnesses), 4),
            })
        return sorted(result, key=lambda x: x["avg_fitness"], reverse=True)

    def get_daily_summary(self, days: int = 7) -> List[Dict]:
        """Get daily mining summary for the last N days."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    DATE(created_at) as day,
                    COUNT(*) as total_tested,
                    SUM(CASE WHEN fitness >= 1.0 AND sharpe >= 1.25 THEN 1 ELSE 0 END) as successes,
                    ROUND(AVG(fitness), 4) as avg_fitness,
                    ROUND(MAX(fitness), 4) as max_fitness,
                    ROUND(AVG(sharpe), 4) as avg_sharpe,
                    ROUND(MAX(sharpe), 4) as max_sharpe
                FROM alphas
                WHERE created_at >= DATE('now', ?)
                GROUP BY DATE(created_at)
                ORDER BY day DESC
                """,
                (f"-{days} days",),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_retrospect_report(self, days: int = 7) -> Dict:
        """Generate a comprehensive retrospect report."""
        return {
            "daily_summary": self.get_daily_summary(days),
            "top_operators": self.get_operator_stats(days)[:10],
            "top_fields": self.get_field_stats(days)[:10],
            "total_alphas": self.count_alphas(),
            "recent_alphas": self.count_alphas(days=days),
            "successful_alphas": len(self.get_successful_alphas()),
        }

    def get_alpha_summary(self) -> Dict:
        """Get summary statistics for notifications."""
        with self._cursor() as cur:
            # Total counts
            cur.execute("SELECT COUNT(*) as total FROM alphas")
            total = cur.fetchone()["total"]

            cur.execute("SELECT COUNT(*) as count FROM alphas WHERE status = 'submitted'")
            submitted = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) as count FROM alphas WHERE status = 'unsubmitted'")
            unsubmitted = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) as count FROM alphas WHERE status = 'pending'")
            pending = cur.fetchone()["count"]

            # All-time submittable count
            cur.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN sharpe >= 1.25 AND fitness >= 1.0 THEN 1 ELSE 0 END) as submittable
                FROM alphas
            """)
            row_all = cur.fetchone()
            new_all = row_all["total"]
            submittable_all = row_all["submittable"] or 0

            return {
                "total": total,
                "submitted": submitted,
                "unsubmitted": unsubmitted,
                "pending": pending,
                "new_all_time": new_all,
                "submittable_all_time": submittable_all,
            }

    def get_all_time_stats(self) -> Dict:
        """Get all-time cumulative statistics for mining progress summary."""
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) as total FROM alphas")
            total = cur.fetchone()["total"]

            cur.execute("""
                SELECT COUNT(*) as passed FROM alphas
                WHERE sharpe >= 1.25 AND fitness >= 1.0
            """)
            passed = cur.fetchone()["passed"]

            cur.execute("SELECT MAX(sharpe) as best_sharpe FROM alphas")
            best_sharpe = cur.fetchone()["best_sharpe"] or 0

            cur.execute("SELECT MAX(fitness) as best_fitness FROM alphas")
            best_fitness = cur.fetchone()["best_fitness"] or 0

            failed = total - passed

            return {
                "tested": total,
                "passed": passed,
                "failed": failed,
                "best_sharpe": best_sharpe,
                "best_fitness": best_fitness,
            }


# ── Global singleton ─────────────────────────────────────────────────

_db_instance: Optional[AlphaDB] = None


def get_alpha_db(db_path: str = DB_PATH) -> AlphaDB:
    """Get the global AlphaDB singleton."""
    global _db_instance
    if _db_instance is None:
        _db_instance = AlphaDB(db_path)
    return _db_instance

```

----------------------------------------

## File: `core\api_session.py`

```python
"""
Simple API Session Manager for WorldQuant Brain.

Simplified version matching the IQC approach - direct requests.Session
with basic authentication and retry logic.
"""

import os
import logging
import time
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

# Constants
BASE_URL = "https://api.worldquantbrain.com"


class WorldQuantSession:
    """Simple WorldQuant Brain API session."""

    def __init__(self, username: str = None, password: str = None):
        """Initialize session with credentials from .env if not provided."""
        if username is None or password is None:
            from .config import load_credentials
            username, password = load_credentials()

        self.username = username
        self.password = password
        self.session = None
        self._authenticate()

    def _authenticate(self):
        """Create authenticated session."""
        logger.info("Authenticating with WorldQuant Brain...")

        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        })
        self.session.auth = HTTPBasicAuth(self.username, self.password)

        try:
            resp = self.session.post(
                f"{BASE_URL}/authentication",
                verify=False,
                timeout=15
            )
            if resp.status_code == 201:
                logger.info("Authentication successful")
                return True
            else:
                raise Exception(f"Authentication failed: {resp.text}")
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            raise

    def ensure_authenticated(self):
        """Re-authenticate if session may have expired."""
        # Simple check - if session exists, assume it's valid
        # The caller should handle 401/403 responses and re-auth if needed
        if self.session is None:
            self._authenticate()

    def request(self, method: str, url: str, **kwargs):
        """Make an API request with automatic retry on auth failure."""
        # Ensure URL is absolute
        if not url.startswith("http"):
            url = f"{BASE_URL}{url}"

        # Set defaults
        kwargs.setdefault("verify", False)
        kwargs.setdefault("timeout", 30)

        try:
            resp = self.session.request(method, url, **kwargs)

            # Handle auth failures
            if resp.status_code in [401, 403]:
                logger.warning("Auth expired, re-authenticating...")
                self._authenticate()
                resp = self.session.request(method, url, **kwargs)

            return resp

        except requests.exceptions.RequestException as e:
            logger.error(f"Request error: {e}")
            raise

    def get(self, url: str, **kwargs):
        """Make a GET request."""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        """Make a POST request."""
        return self.request("POST", url, **kwargs)

    def submit_simulation(self, expression: str, settings: dict = None) -> dict:
        """Submit a factor for backtesting."""
        default_settings = {
            "instrumentType": "EQUITY",
            "region": "USA",
            "universe": "TOP3000",
            "delay": 1,
            "decay": 0,
            "neutralization": "NONE",
            "truncation": 0.08,
            "pasteurization": "ON",
            "unitHandling": "VERIFY",
            "nanHandling": "OFF",
            "language": "FASTEXPR",
            "visualization": False
        }

        if settings:
            default_settings.update(settings)

        payload = {
            "type": "REGULAR",
            "settings": default_settings,
            "regular": expression
        }

        resp = self.post(f"{BASE_URL}/simulations", json=payload)

        if resp.status_code == 201:
            sim_id = resp.headers.get("Location", "").split("/")[-1]
            return {"success": True, "sim_id": sim_id}
        else:
            return {"success": False, "error": resp.text[:200]}

    def poll_simulation(self, sim_id: str, timeout: int = 600) -> dict:
        """Poll simulation until complete."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            resp = self.get(f"{BASE_URL}/simulations/{sim_id}")

            if resp.status_code != 200:
                time.sleep(3)
                continue

            data = resp.json()
            status = data.get("status")

            if status in ["FINISHED", "WARNING", "COMPLETE", "COMPLETED"]:
                alpha_id = data.get("alpha")
                if alpha_id:
                    # Get alpha performance
                    alpha_resp = self.get(f"{BASE_URL}/alphas/{alpha_id}")
                    if alpha_resp.status_code == 200:
                        perf = alpha_resp.json().get("is", {})
                        return {
                            "success": True,
                            "alpha_id": alpha_id,
                            "sharpe": perf.get("sharpe", 0),
                            "fitness": perf.get("fitness", 0),
                            "turnover": perf.get("turnover", 0),
                            "margin": perf.get("margin", 0),
                            "message": data.get("message", "Success")
                        }
                return {"success": True, "alpha_id": alpha_id}

            elif status in ["ERROR", "FAILED"]:
                return {"success": False, "error": data.get("message", "Unknown error")}

            time.sleep(3)

        return {"success": False, "error": "Simulation timeout"}

    def submit_alpha(self, alpha_id: str) -> bool:
        """Submit an alpha to WorldQuant Brain."""
        resp = self.post(
            f"{BASE_URL}/alphas/{alpha_id}/submit",
            json={"type": "REGULAR"}
        )
        return resp.status_code == 201


# Global session instance
_session: Optional[WorldQuantSession] = None


def get_session(username: str = None, password: str = None) -> WorldQuantSession:
    """Get or create the global session instance."""
    global _session
    if _session is None:
        _session = WorldQuantSession(username, password)
    return _session


def reset_session():
    """Reset the global session (for testing or re-authentication)."""
    global _session
    _session = None

```

----------------------------------------

## File: `core\config.py`

```python
import os
import logging

# Clean SOCKS proxy BEFORE requests is imported — prevents urllib3/requests
# from picking up all_proxy and failing SSL connections
for _k in ('all_proxy', 'ALL_PROXY'):
    os.environ.pop(_k, None)

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def load_credentials() -> tuple[str, str]:
    username = os.getenv("WQ_USERNAME")
    password = os.getenv("WQ_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "WQ_USERNAME and WQ_PASSWORD must be set in .env file"
        )
    return username, password


# ── LLM Configuration Getters ──────────────────────────────────────────────

def get_llm_provider() -> str:
    """Get the active LLM provider: 'deepseek', 'ollama', or 'gemini'."""
    return os.getenv("LLM_PROVIDER", "deepseek").lower()


def get_ollama_url() -> str:
    return os.getenv("OLLAMA_URL", "http://localhost:11434")


def get_deepseek_api_key() -> str:
    return os.getenv("DEEPSEEK_API_KEY", "")


def get_deepseek_base_url() -> str:
    return os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


def get_gemini_api_key() -> str:
    return os.getenv("GEMINI_API_KEY", "")


def get_gemini_base_url() -> str:
    return os.getenv(
        "GEMINI_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    )


def get_gemini_model() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-2.0-flash")


def get_default_model() -> str:
    """Get the default model name with env/file override.

    Checks (in order):
      1. WQ_DEFAULT_MODEL env var
      2. data/model_config.json file
      3. fallback: provider-specific default
    """
    env_model = os.getenv("WQ_DEFAULT_MODEL")
    if env_model:
        return env_model

    config_path = os.path.join("data", "model_config.json")
    if os.path.exists(config_path):
        try:
            import json
            with open(config_path) as f:
                data = json.load(f)
            model = data.get("default_model")
            if model:
                return model
        except Exception:
            pass

    provider = get_llm_provider()
    if provider == "gemini":
        return get_gemini_model()
    elif provider == "deepseek":
        return "deepseek-coder"
    return "qwen3.5:35b"


def set_default_model(model_name: str):
    """Persist the default model to a data file (never modifies source code)."""
    import json
    from datetime import datetime
    config_path = os.path.join("data", "model_config.json")
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(
            {
                "default_model": model_name,
                "updated_at": datetime.now().isoformat(),
            },
            f,
        )


def fix_session_proxy(sess: requests.Session) -> None:
    for k in ['all_proxy', 'ALL_PROXY']:
        if os.environ.pop(k, None):
            logger.info(f"Removed {k} from environment to avoid SOCKS conflicts")

    http_proxy = (
        os.environ.get('HTTPS_PROXY', '')
        or os.environ.get('https_proxy', '')
        or os.environ.get('HTTP_PROXY', '')
        or os.environ.get('http_proxy', '')
    )

    if http_proxy and http_proxy.startswith('http'):
        sess.proxies = {
            'http': http_proxy,
            'https': http_proxy,
        }
        logger.info(f"Set HTTP proxy for session: {http_proxy}")
    elif http_proxy and 'socks' in http_proxy.lower():
        logger.warning(
            f"Proxy {http_proxy} is SOCKS-based, which may cause SSL errors. "
            f"Consider setting HTTPS_PROXY to an HTTP proxy (e.g. http://127.0.0.1:7897)"
        )
        sess.proxies = {
            'http': http_proxy,
            'https': http_proxy,
        }
    else:
        logger.info("No HTTP proxy configured, using direct connection")

    try:
        import certifi
        sess.verify = certifi.where()
        logger.info(f"Using certifi SSL certificates: {certifi.where()}")
    except ImportError:
        logger.warning("certifi not installed, using default SSL certificates")

    retry_strategy = requests.adapters.Retry(
        total=7,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PATCH"],
    )
    adapter = requests.adapters.HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=20,
        pool_maxsize=20,
    )
    sess.mount('https://', adapter)
    sess.mount('http://', adapter)

```

----------------------------------------

## File: `core\data_fetcher.py`

```python
"""
WorldQuant Brain Data Field and Operator Fetcher

Fetches available data fields and operators from the WQ Brain API.
Saves results to organized directories:
- data/fields/{dataset}.csv — Data fields grouped by dataset
- data/operators/operators.csv — All operators

Fields are cached locally to avoid repeated API calls (429 rate limit).
Re-run this script manually when you want to refresh the field list.
"""

import os
import re
import json
import logging
import time
from typing import List, Dict

import pandas as pd

logger = logging.getLogger(__name__)

# Base directories
BASE_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
FIELDS_DIR = os.path.join(BASE_DATA_DIR, "fields_delay1")  # Default to delay1
OPERATORS_DIR = os.path.join(BASE_DATA_DIR, "operators")

# Region -> Universe mapping
REGION_UNIVERSE_MAP: Dict[str, List[str]] = {
    "USA": ["TOP3000", "TOP1000", "TOP500"],
    "GLB": ["TOP3000"],
    "EUR": ["TOP2500", "TOP1200"],
    "ASI": ["MINVOL1M"],
    "CHN": ["TOP2000U"],
}


class DataFetcher:
    """Fetches data fields and operators from WorldQuant Brain API."""

    def __init__(self, session=None):
        os.makedirs(FIELDS_DIR, exist_ok=True)
        os.makedirs(OPERATORS_DIR, exist_ok=True)

        if session is not None:
            self._session = session.session if hasattr(session, "session") else session
        else:
            import requests
            from requests.auth import HTTPBasicAuth
            from core.config import load_credentials

            username, password = load_credentials()
            self._session = requests.Session()
            self._session.auth = HTTPBasicAuth(username, password)
            self._session.headers.update({
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            })

            resp = self._session.post(
                "https://api.worldquantbrain.com/authentication",
                verify=False,
                timeout=15,
            )
            if resp.status_code != 201:
                raise Exception(f"Authentication failed: {resp.text}")

    # ── Dataset discovery ────────────────────────────────────────────

    def fetch_datasets(
        self,
        region: str = "USA",
        universe: str = "TOP3000",
        delay: int = 1,
        limit: int = 50,
    ) -> List[Dict]:
        """Fetch all dataset IDs for a given region/universe/delay."""
        url = "https://api.worldquantbrain.com/data-sets"
        all_datasets = []
        offset = 0

        while True:
            params = {
                "delay": delay,
                "instrumentType": "EQUITY",
                "limit": limit,
                "offset": offset,
                "region": region,
                "universe": universe,
            }

            try:
                resp = self._session.get(url, params=params, verify=False, timeout=30)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 30))
                    logger.warning(f"Rate limited, waiting {retry_after}s...")
                    time.sleep(retry_after)
                    continue

                if resp.status_code != 200:
                    logger.warning(f"Failed to fetch datasets: {resp.status_code}")
                    break

                data = resp.json()
                results = data.get("results", [])
                total = data.get("count", 0)

                if not results:
                    break

                for item in results:
                    ds_id = item.get("id") or item.get("dataset_id")
                    if ds_id:
                        all_datasets.append({
                            "id": ds_id,
                            "name": item.get("name", ds_id),
                            "fields": item.get("fieldCount", 0),
                        })

                logger.info(f"  Fetched {len(all_datasets)}/{total} datasets...")
                if len(all_datasets) >= total:
                    break

                offset += limit
                time.sleep(2)

            except Exception as e:
                logger.warning(f"Error fetching datasets: {e}")
                break

        logger.info(f"Found {len(all_datasets)} datasets for {region}/{universe}")
        return all_datasets

    # ── Field fetching (per dataset) ─────────────────────────────────

    def fetch_fields_for_dataset(
        self,
        dataset_id: str,
        region: str = "USA",
        universe: str = "TOP3000",
        delay: int = 1,
        limit: int = 50,
    ) -> List[Dict]:
        """Fetch all fields for a specific dataset."""
        url = "https://api.worldquantbrain.com/data-fields"
        all_fields = []
        offset = 0

        while True:
            params = {
                "instrumentType": "EQUITY",
                "region": region,
                "delay": delay,
                "universe": universe,
                "dataset.id": dataset_id,
                "limit": limit,
                "offset": offset,
            }

            try:
                resp = self._session.get(url, params=params, verify=False, timeout=30)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 10))
                    logger.warning(f"Rate limited, waiting {retry_after}s...")
                    time.sleep(retry_after)
                    continue

                if resp.status_code != 200:
                    logger.warning(f"Failed to fetch fields for {dataset_id}: {resp.status_code}")
                    break

                data = resp.json()
                results = data.get("results", [])
                total = data.get("count", 0)

                if not results:
                    break

                all_fields.extend(results)
                if len(all_fields) >= total:
                    break

                offset += limit
                time.sleep(2)

            except Exception as e:
                logger.warning(f"Error fetching fields for {dataset_id}: {e}")
                break

        return all_fields

    # ── Full field fetch (all datasets) ──────────────────────────────

    def fetch_all_fields(
        self,
        region: str = "USA",
        universe: str = "TOP3000",
        delay: int = 1,
    ) -> List[Dict]:
        """Fetch all fields by iterating through datasets."""
        logger.info(f"Fetching all fields for {region}/{universe}...")

        datasets = self.fetch_datasets(region=region, universe=universe, delay=delay)
        all_fields = []

        for i, ds in enumerate(datasets):
            ds_id = ds["id"]
            logger.info(f"  [{i+1}/{len(datasets)}] Fetching fields for dataset: {ds_id}")
            fields = self.fetch_fields_for_dataset(
                dataset_id=ds_id,
                region=region,
                universe=universe,
                delay=delay,
            )

            # Tag each field with dataset info
            for f in fields:
                f["_dataset_id"] = ds_id
                f["_dataset_name"] = ds.get("name", ds_id)

            all_fields.extend(fields)
            logger.info(f"    Got {len(fields)} fields (total: {len(all_fields)})")
            time.sleep(3)

        logger.info(f"Finished fetching. Total: {len(all_fields)} fields from {len(datasets)} datasets")
        return all_fields

    # ── Save fields to CSV ───────────────────────────────────────────

    def save_fields_to_csv(self, fields: List[Dict], delay: int = 1) -> List[str]:
        """Save fields to CSV files grouped by dataset."""
        # 根据 delay 参数选择目录
        fields_dir = os.path.join(BASE_DATA_DIR, f"fields_delay{delay}")
        os.makedirs(fields_dir, exist_ok=True)

        datasets: Dict[str, List[Dict]] = {}

        for field in fields:
            ds_id = field.get("_dataset_id", "unknown")
            ds_name = field.get("_dataset_name", ds_id)

            if ds_id not in datasets:
                datasets[ds_id] = {"name": ds_name, "fields": []}

            datasets[ds_id]["fields"].append({
                "Field": field.get("id", ""),
                "Description": field.get("description", ""),
                "Type": field.get("type", ""),
                "Dataset": ds_name,
                "Alphas": field.get("alphaCount", 0),  # 使用正确的字段名 alphaCount
            })

        saved_files = []
        for ds_id, ds_info in datasets.items():
            ds_fields = ds_info["fields"]
            if not ds_fields:
                continue

            safe_name = re.sub(r"[^a-z0-9_]+", "_", ds_id.lower()).strip("_")
            csv_path = os.path.join(fields_dir, f"{safe_name}.csv")

            df = pd.DataFrame(ds_fields)
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            saved_files.append(csv_path)
            logger.info(f"Saved {len(ds_fields)} fields to {csv_path}")

        return saved_files

    # ── Operators ────────────────────────────────────────────────────

    def fetch_operators(self) -> List[Dict]:
        """Fetch all available operators."""
        logger.info("Fetching operators...")
        url = "https://api.worldquantbrain.com/operators"

        try:
            resp = self._session.get(url, verify=False, timeout=60)
            if resp.status_code != 200:
                logger.warning(f"Failed to fetch operators: {resp.status_code}")
                return []

            operators_raw = resp.json()
            if not isinstance(operators_raw, list):
                return []

            operators = []
            for item in operators_raw:
                operators.append({
                    "Name": item.get("name", ""),
                    "Category": item.get("category", ""),
                    "Scope": ", ".join(item.get("scope", [])),
                    "Definition": item.get("definition", ""),
                    "Description": item.get("description", ""),
                })

            logger.info(f"Fetched {len(operators)} operators")
            return operators

        except Exception as e:
            logger.error(f"Error fetching operators: {e}")
            return []

    def save_operators_to_csv(self, operators: List[Dict]) -> str:
        """Save operators to CSV file."""
        if not operators:
            return ""

        df = pd.DataFrame(operators)
        csv_path = os.path.join(OPERATORS_DIR, "operators.csv")
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logger.info(f"Saved {len(operators)} operators to {csv_path}")
        return csv_path

    # ── Combined fetch ───────────────────────────────────────────────

    def fetch_and_save_all(
        self,
        region: str = "USA",
        universe: str = "TOP3000",
    ) -> Dict[str, object]:
        """Fetch and save all data fields and operators."""
        result = {"fields": [], "operators": ""}

        fields = self.fetch_all_fields(region=region, universe=universe)
        result["fields"] = self.save_fields_to_csv(fields)

        operators = self.fetch_operators()
        result["operators"] = self.save_operators_to_csv(operators)

        return result

    def get_field_summary(self) -> Dict[str, int]:
        """Get summary of saved fields by file."""
        summary = {}
        if not os.path.exists(FIELDS_DIR):
            return summary

        for file in sorted(os.listdir(FIELDS_DIR)):
            if file.endswith(".csv"):
                try:
                    df = pd.read_csv(os.path.join(FIELDS_DIR, file))
                    summary[file.replace(".csv", "")] = len(df)
                except Exception:
                    pass
        return summary

    def get_operator_summary(self) -> Dict[str, int]:
        """Get summary of saved operators by category."""
        summary = {}
        csv_path = os.path.join(OPERATORS_DIR, "operators.csv")
        if not os.path.exists(csv_path):
            return summary

        try:
            df = pd.read_csv(csv_path)
            if "Category" in df.columns:
                summary = df["Category"].value_counts().to_dict()
        except Exception:
            pass
        return summary


def fetch_and_save_all(
    region: str = "USA",
    universe: str = "TOP3000",
    session=None,
) -> Dict[str, object]:
    """Convenience function to fetch and save all data."""
    fetcher = DataFetcher(session=session)
    return fetcher.fetch_and_save_all(region=region, universe=universe)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = fetch_and_save_all()

    print("\n=== Summary ===")
    print(f"Fields saved: {len(result['fields'])} files")
    print(f"Operators saved: {result['operators']}")

```

----------------------------------------

## File: `core\feishu_client.py`

```python
"""
Feishu API Client — Handles message parsing and replies.
"""

import os
import logging
import requests
import json
from typing import Optional

logger = logging.getLogger(__name__)


class FeishuClient:
    """Feishu API client for receiving and replying to messages."""

    def __init__(self, app_id: str = None, app_secret: str = None):
        self.app_id = app_id or os.getenv("FEISHU_APP_ID")
        self.app_secret = app_secret or os.getenv("FEISHU_APP_SECRET")
        self._tenant_token: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def _get_tenant_token(self, force_refresh: bool = False) -> str:
        """Get tenant_access_token (with caching)."""
        if self._tenant_token and not force_refresh:
            return self._tenant_token

        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            self._tenant_token = data["tenant_access_token"]
            return self._tenant_token
        else:
            logger.error(f"Failed to get tenant_access_token: {data}")
            self._tenant_token = None
            return None

    def verify_challenge(self, body: dict) -> dict:
        """Handle Feishu webhook verification requests."""
        challenge = body.get("challenge")
        if challenge:
            return {"challenge": challenge}
        return {}

    def parse_message(self, body: dict) -> tuple:
        """
        Parse message events.
        Returns: (message_id, chat_id, text_content) or (None, None, None)
        """
        try:
            # Feishu event format
            event = body.get("event", {})
            message = event.get("message", {})

            logger.debug(f"Received event: {body}")

            message_id = message.get("message_id")
            chat_id = message.get("chat_id")
            msg_type = message.get("message_type")

            # Process text messages only
            if msg_type != "text":
                return None, None, None

            # Parse text content
            content = message.get("content", "{}")
            text = json.loads(content).get("text", "").strip()

            # Remove automatically added @mention prefix by Feishu
            # Format: @_user_1 /summary or @_user_1 space /summary
            if text.startswith("@_user_"):
                parts = text.split(maxsplit=1)
                text = parts[1] if len(parts) > 1 else ""

            return message_id, chat_id, text

        except Exception as e:
            logger.error(f"Failed to parse message: {e}")
            return None, None, None

    def reply_message(self, message_id: str, title: str, content: str) -> bool:
        """Reply to message (Markdown card format)."""
        token = self._get_tenant_token()
        if not token:
            return False

        # Feishu Card V2 format - content must be a string
        card = {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue"
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content
                    }
                ]
            }
        }

        payload = {
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False)
        }

        try:
            resp = requests.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
            )
            data = resp.json()

            # Token expired, retry once after refreshing
            if data.get("code") == 99991663:
                logger.info("Token expired, refreshing and retrying...")
                token = self._get_tenant_token(force_refresh=True)
                if not token:
                    return False
                resp = requests.post(
                    f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=10,
                )
                data = resp.json()

            if data.get("code") == 0:
                logger.info(f"Message replied successfully: {message_id}")
                return True
            else:
                logger.warning(f"Failed to reply message: {data}")
                return False
        except Exception as e:
            logger.warning(f"Exception while replying message: {e}")
            return False


# ── Global singleton ─────────────────────────────────────────────────

_client_instance: Optional[FeishuClient] = None


def get_feishu_client() -> FeishuClient:
    """Get global FeishuClient singleton instance."""
    global _client_instance
    if _client_instance is None:
        _client_instance = FeishuClient()
    return _client_instance

```

----------------------------------------

## File: `core\llm_client.py`

```python
"""
Unified LLM Client for Alpha Generation

Supports Ollama (local), DeepSeek API, and Gemini API for generating
WorldQuant Brain alpha expressions.
"""

import os
import json
import re
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


class LLMClient:
    """Unified LLM client supporting Ollama, DeepSeek API, and Gemini API."""

    def __init__(self, provider: str = "auto"):
        """
        Initialize LLM client.

        Args:
            provider: "ollama", "deepseek", "gemini", or "auto"
                      (tries Gemini or DeepSeek API first, falls back to Ollama)
        """
        self.provider = provider
        self._setup_provider()

    def _setup_provider(self):
        """Setup the LLM provider based on configuration."""
        if self.provider == "auto":
            # Check for API keys in environment variables
            gemini_key = os.getenv("GEMINI_API_KEY")
            deepseek_key = os.getenv("DEEPSEEK_API_KEY")

            if gemini_key:
                self.provider = "gemini"
                logger.info("Using Gemini API")
            elif deepseek_key:
                self.provider = "deepseek"
                logger.info("Using DeepSeek API")
            else:
                self.provider = "ollama"
                logger.info("Using Ollama (local)")

        if self.provider == "gemini":
            self._setup_gemini()
        elif self.provider == "deepseek":
            self._setup_deepseek()
        elif self.provider == "ollama":
            self._setup_ollama()
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

    def _setup_gemini(self):
        """Setup Gemini API client via OpenAI compatibility endpoint."""
        try:
            from openai import OpenAI
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise ValueError("GEMINI_API_KEY not set in .env")

            base_url = os.getenv(
                "GEMINI_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta/openai/"
            )
            self.client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=120.0
            )
            self.model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
            logger.info("Gemini API client initialized")
        except ImportError:
            raise ImportError("openai package required for Gemini API: pip install openai")

    def _setup_deepseek(self):
        """Setup DeepSeek API client."""
        try:
            from openai import OpenAI
            api_key = os.getenv("DEEPSEEK_API_KEY")
            if not api_key:
                raise ValueError("DEEPSEEK_API_KEY not set in .env")

            self.client = OpenAI(
                api_key=api_key,
                base_url="https://api.deepseek.com",
                timeout=120.0
            )
            self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
            logger.info("DeepSeek API client initialized")
        except ImportError:
            raise ImportError("openai package required for DeepSeek API: pip install openai")

    def _setup_ollama(self):
        """Setup Ollama client."""
        import requests
        self.ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        self.model = os.getenv("OLLAMA_MODEL", "qwen3:8b")

        # Test connection
        try:
            resp = requests.get(f"{self.ollama_url}/api/tags", timeout=5)
            if resp.status_code != 200:
                logger.warning(f"Ollama not responding at {self.ollama_url}")
        except Exception as e:
            logger.warning(f"Cannot connect to Ollama: {e}")

    def generate_alphas(self, system_prompt: str, user_prompt: str, num_alphas: int = 5) -> List[Dict]:
        """
        Generate alpha expressions using the configured LLM.

        Args:
            system_prompt: System instructions for the LLM
            user_prompt: User prompt with specific requirements
            num_alphas: Number of alphas to generate

        Returns:
            List of alpha dicts with 'expression', 'logic', and optional 'settings'
        """
        if self.provider == "gemini":
            return self._generate_gemini(system_prompt, user_prompt)
        elif self.provider == "deepseek":
            return self._generate_deepseek(system_prompt, user_prompt)
        else:
            return self._generate_ollama(system_prompt, user_prompt)

    def _generate_gemini(self, system_prompt: str, user_prompt: str) -> List[Dict]:
        """Generate using Gemini API."""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            )
            content = response.choices[0].message.content
            return self._extract_json(content)
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            return []

    def _generate_deepseek(self, system_prompt: str, user_prompt: str) -> List[Dict]:
        """Generate using DeepSeek API."""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            )
            content = response.choices[0].message.content
            return self._extract_json(content)
        except Exception as e:
            logger.error(f"DeepSeek API error: {e}")
            return []

    def _generate_ollama(self, system_prompt: str, user_prompt: str) -> List[Dict]:
        """Generate using Ollama API."""
        import requests

        try:
            response = requests.post(
                f"{self.ollama_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    "stream": False,
                    "options": {
                        "temperature": 0.7,
                        "num_predict": 2048
                    }
                },
                timeout=120
            )

            if response.status_code != 200:
                logger.error(f"Ollama error: {response.status_code} {response.text}")
                return []

            data = response.json()
            content = data.get("message", {}).get("content", "")
            return self._extract_json(content)
        except Exception as e:
            logger.error(f"Ollama error: {e}")
            return []

    def _extract_json(self, text: str) -> List[Dict]:
        """Extract JSON array from LLM response text."""
        # Try to find JSON array in the response
        match = re.search(r'\[\s*\{.*?\}\s*\]', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON: {e}")

        # Try to find individual JSON objects
        objects = re.findall(r'\{[^{}]+\}', text)
        if objects:
            results = []
            for obj_str in objects:
                try:
                    obj = json.loads(obj_str)
                    if "expression" in obj:
                        results.append(obj)
                except json.JSONDecodeError:
                    continue
            if results:
                return results

        logger.warning("No valid JSON found in LLM response")
        return []


# Default system prompt for alpha generation
DEFAULT_SYSTEM_PROMPT = """You are a top-tier WorldQuant quantitative architect.
Core Discipline:
1. Output ONLY a pure JSON array, without any extra Markdown or conversational text.
2. Placeholders like <WINDOW> are STRICTLY PROHIBITED; you must fill in concrete integers (e.g., 5, 10, 20, 60).
3. FASTEXPR syntax MUST be used, wrapping core logic in this smoothing shell:
   ts_decay_linear( zscore( your_core_cross_sectional_or_time_series_logic ), 10 )
   Note: Decay window must be at least 10; use 20 or larger for high turnover factors.
4. Do NOT use group_neutralize inside the expression; neutralization is controlled by settings.
5. The JSON structure MUST be: [{"logic": "description", "expression": "code", "settings": {"delay":<specified_by_user_prompt>, "neutralization":"INDUSTRY", "truncation":0.08, "pasteurization":"ON"}}]
   - neutralization MUST be one of the following: "NONE", "INDUSTRY", "SUBINDUSTRY", "SECTOR", "MARKET"
   - STRICTLY PROHIBITED to use other values like "STYLE", "COUNTRY", etc.!
6. Event fields (e.g., nws_*, snt_*, scl_*_buzz*, rp_*, fnd6_*event*, anl4_*, etc.) are VECTOR type:
   - ❌ WQ API rejects almost all operators for VECTOR fields: ==, !=, sign(), trade_when(), etc. are NOT supported!
   - ❌ Cannot participate in arithmetic operations (+, -, *, /)
   - ❌ Cannot use ts_delta, ts_mean, ts_sum, rank, sign, zscore, or any other operators
   - ⚠️ HIGHLY RECOMMENDED: Only use MATRIX type fields to generate alphas; ignore VECTOR fields!
   - ⚠️ If VECTOR fields are listed in the prompt, DO NOT use them. Use MATRIX fields only.
7. Operator arguments MUST strictly follow rules:
   - Single arg: rank(x), sign(x), abs(x), log(x), zscore(x), inverse(x), sqrt(x)
   - Time-series single arg + window: ts_rank(x,d), ts_zscore(x,d), ts_mean(x,d), ts_std_dev(x,d), ts_sum(x,d), ts_delta(x,d), ts_delay(x,d), ts_decay_linear(x,d)
   - Time-series dual arg + window: ts_corr(x,y,d), ts_covariance(y,x,d)
   - Logic: if_else(condition, true_val, false_val), trade_when(condition, x, y)
   - Incorrect example: rank(a,b) ❌ → Correct: rank(a/b) ✓
8. String literals are STRICTLY PROHIBITED! WorldQuant does not support string comparison.
   - ❌ Incorrect: if_else(field == "revision", x, y)
   - ❌ Incorrect: if_else(field > "value", x, y)
   - ✓ Correct: if_else(field > 0, x, y)
   - ✓ Correct: trade_when(field > threshold, x, y)
   - All conditions must be numerical comparisons (>, <, ==, >=, <=, !=)
9. Double or single quotes inside expressions are STRICTLY PROHIBITED! Expressions can only contain numbers, variable names, and operators.
10. Logical operators MUST use function syntax; infix forms or symbols are forbidden:
    - Logical AND: and(input1, input2) — e.g., and(x > 0, y > 0)
    - Logical OR: or(input1, input2) — e.g., or(x > 0, y > 0)
    - Logical NOT: not(x) — e.g., not(x > 0)
    - ✓ Correct: if_else(and(x > 0, y > 0), a, b)
    - ❌ Incorrect: if_else(x > 0 and y > 0, a, b) — Infix form forbidden
    - ❌ Incorrect: if_else(x > 0 & y > 0, a, b) — Symbol form forbidden
11. Scientific notation is STRICTLY PROHIBITED! FASTEXPR does not support notations like 1e-6 or 1e-8.
    - ❌ Incorrect: x / (y + 1e-6)  → Throws error Unexpected character 'e'
    - ✓ Correct: x / (y + 0.000001)
    - All small values must be written in decimal form, without using 'e' notation.
"""


def get_llm_client(provider: str = "auto") -> LLMClient:
    """Get or create LLM client singleton."""
    if not hasattr(get_llm_client, '_instance'):
        get_llm_client._instance = LLMClient(provider=provider)
    return get_llm_client._instance

```

----------------------------------------

## File: `core\log_manager.py`

```python
import logging
import os
from datetime import datetime


LOG_DIR = "log"
DATE_FMT = "%Y-%m-%d"


class DailyFileHandler(logging.FileHandler):
    """FileHandler that creates a new log file each day."""

    def __init__(self, log_dir: str, encoding: str = "utf-8"):
        self.log_dir = log_dir
        self._current_date = datetime.now().strftime(DATE_FMT)
        os.makedirs(log_dir, exist_ok=True)
        filepath = self._get_filepath(self._current_date)
        super().__init__(filepath, encoding=encoding)

    def _get_filepath(self, date_str: str) -> str:
        return os.path.abspath(os.path.join(self.log_dir, f"{date_str}.log"))

    def emit(self, record: logging.LogRecord) -> None:
        today = datetime.now().strftime(DATE_FMT)
        if today != self._current_date:
            self._current_date = today
            if self.stream:
                self.stream.close()
            self.baseFilename = self._get_filepath(today)
            self.stream = self._open()
        super().emit(record)


def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = DailyFileHandler(LOG_DIR)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

```

----------------------------------------

## File: `core\notifier.py`

```python
"""
Notification Module — Feishu Webhook Notifications, supporting Alpha Discovery, Periodic Summary, and Circuit Breaker Alerts.
"""

import os
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)


class Notifier:
    """Feishu Webhook Notifier."""

    def __init__(self):
        self.webhook_url: Optional[str] = os.getenv("FEISHU_WEBHOOK")
        if self.webhook_url:
            logger.info("Feishu notification enabled")
        else:
            logger.info("FEISHU_WEBHOOK not configured, notification disabled")

        # Circuit breaker counters
        self._consecutive_auth_failures = 0
        self._consecutive_llm_errors = 0

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def send(self, title: str, content: str) -> bool:
        """Send Feishu message (Rich Text format). Returns success status."""
        if not self.webhook_url:
            return False

        try:
            resp = requests.post(
                self.webhook_url,
                json={
                    "msg_type": "post",
                    "content": {
                        "post": {
                            "zh_cn": {
                                "title": title,
                                "content": [[{"tag": "text", "text": content}]],
                            }
                        }
                    },
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    logger.info(f"Feishu notification sent successfully: {title}")
                    return True
                else:
                    logger.warning(f"Feishu notification failed: {data}")
            else:
                logger.warning(f"Feishu notification HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"Feishu notification exception: {e}")
        return False

    def send_markdown(self, title: str, markdown: str, template: str = "blue") -> bool:
        """Send Feishu Markdown card message. Returns success status."""
        if not self.webhook_url:
            return False

        # Feishu Card V2 format
        payload = {
            "msg_type": "interactive",
            "card": {
                "schema": "2.0",
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": template
                },
                "body": {
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": markdown
                        }
                    ]
                }
            }
        }

        try:
            resp = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    logger.info(f"Feishu Markdown notification sent successfully: {title}")
                    return True
                else:
                    logger.warning(f"Feishu notification failed: {data}")
            else:
                logger.warning(f"Feishu notification HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"Feishu notification exception: {e}")
        return False

    # ── Alpha Discovery Notification ──────────────────────────────────

    def notify_alpha(
        self,
        alpha_id: str,
        sharpe: float,
        fitness: float,
        turnover: float,
        expression: str,
        member_id: str = "",
    ):
        """Send notification when a qualified Alpha is discovered (Markdown card format)."""
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        expr_short = expression[:80] + ("..." if len(expression) > 80 else "")

        lines = [
            f"**Discovery Time:** {timestamp}",
            "",
            "## Alpha Information",
            f"- **ID:** {alpha_id}",
            f"- **Member:** {member_id or 'N/A'}",
            "",
            "## Metrics",
            "| Metric | Value |",
            "|------|------|",
            f"| Sharpe | **{sharpe:.2f}** |",
            f"| Fitness | **{fitness:.2f}** |",
            f"| Turnover | **{turnover:.2f}** |",
            "",
            "## Expression",
            f"```\n{expr_short}\n```",
        ]

        self.send_markdown("New Alpha Discovered!", "\n".join(lines), template="green")

    # ── Correlation Check Notification ──────────────────────────────

    def notify_correlation_check(
        self,
        total: int,
        passed: int,
        failed: int,
        failed_alphas: list,
        summary: dict = None,
    ):
        """Correlation check result notification (Markdown card format)."""
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            f"**Check Time:** {timestamp}",
            "",
            "## Correlation Check Results",
            f"- Total Checked: **{total}**",
            f"- PASS: **{passed}** ✅",
            f"- FAIL: **{failed}** ❌",
        ]

        if failed_alphas:
            lines.append("")
            lines.append("## Failure Details")
            lines.append("| Alpha ID | Correlation Value | Limit |")
            lines.append("|----------|-------------------|-------|")
            for alpha in failed_alphas[:10]:
                lines.append(f"| {alpha['alpha_id']} | {alpha['value']:.4f} | {alpha['limit']} |")
            if len(failed_alphas) > 10:
                lines.append(f"| ... | Total {len(failed_alphas)} items | |")

        if summary:
            lines.append("")
            lines.append("## Alpha Pool Summary")
            lines.append(f"- Total Alphas: **{summary.get('total', 0)}**")
            lines.append(f"- Submitted: **{summary.get('submitted', 0)}**")
            lines.append(f"- Pending Check: **{summary.get('pending', 0)}**")
            lines.append(f"- Submittable: **{summary.get('unsubmitted', 0)}**")
            lines.append("")
            lines.append("## All-Time Statistics")
            lines.append(f"- Total Alphas: **{summary.get('new_all_time', 0)}**")
            lines.append(f"- Submittable: **{summary.get('submittable_all_time', 0)}**")

        self.send_markdown("Correlation Check Report", "\n".join(lines))

    # ── Periodic Summary Notification ────────────────────────────────

    def notify_summary(
        self,
        tested: int,
        passed: int,
        failed: int,
        best_sharpe: float,
        best_fitness: float,
        rescue_pool: int,
        member_id: str = "",
    ):
        """Periodic summary notification (Markdown card format)."""
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            f"**Summary Time:** {timestamp}",
            "",
            "## Test Results",
            "| Item | Count |",
            "|------|------|",
            f"| Tested | **{tested}** |",
            f"| Passed | **{passed}** ✅ |",
            f"| Failed | **{failed}** ❌ |",
            "",
            "## Best Metrics",
            f"- Sharpe: **{best_sharpe:.2f}**",
            f"- Fitness: **{best_fitness:.2f}**",
            "",
            f"**Rescue Pool:** {rescue_pool}",
        ]
        if member_id:
            lines.append(f"**Member:** {member_id}")

        self.send_markdown("Mining Progress Summary", "\n".join(lines), template="orange")

    # ── Circuit Breaker Alerts ──────────────────────────────────────

    def record_auth_failure(self):
        """Record an authentication failure. Trigger alert on 3 consecutive failures."""
        self._consecutive_auth_failures += 1
        if self._consecutive_auth_failures >= 3:
            self.send(
                "Miner Shutdown Warning",
                "{} consecutive authentication failures (AUTH_FAILED)\n"
                "Token may have expired, miner has stopped running.\n"
                "Please check account credentials or re-login.".format(
                    self._consecutive_auth_failures
                ),
            )

    def record_auth_success(self):
        """Authentication succeeded, reset counter."""
        self._consecutive_auth_failures = 0

    def record_llm_error(self):
        """Record an LLM call failure. Trigger alert on 5 consecutive failures."""
        self._consecutive_llm_errors += 1
        if self._consecutive_llm_errors >= 5:
            self.send(
                "Miner Shutdown Warning",
                "{} consecutive LLM call failures\n"
                "DeepSeek API quota may be exhausted.\n"
                "Please check API Key balance.".format(
                    self._consecutive_llm_errors
                ),
            )

    def record_llm_success(self):
        """LLM call succeeded, reset counter."""
        self._consecutive_llm_errors = 0

    def notify_fatal(self, reason: str, member_id: str = ""):
        """Send highest level alert when a fatal error stops the miner."""
        lines = [reason]
        if member_id:
            lines.append("Member: {}".format(member_id))
        self.send("Miner Shutdown Warning", "\n".join(lines))


_notifier: Optional[Notifier] = None


def get_notifier() -> Notifier:
    global _notifier
    if _notifier is None:
        _notifier = Notifier()
    return _notifier

```

----------------------------------------

## File: `core\submission_quota.py`

```python
"""
Submission quota tracker with persistent state.

Tracks daily submission count and enforces a configurable limit.
Thread-safe for concurrent environments. Replaces the simplistic
`can_submit_today()` date-only check with counted quota management.
"""

import json
import os
import threading
import time
import logging
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_QUOTA_FILE = os.path.join("data", "submission_log.json")


class SubmissionQuota:
    """Daily submission quota tracker with persistent state."""

    def __init__(
        self,
        daily_limit: int = 10,
        quota_file: str = DEFAULT_QUOTA_FILE,
    ):
        self.daily_limit = daily_limit
        self.quota_file = quota_file
        self._lock = threading.Lock()
        self._date: str = ""
        self._count: int = 0
        self._submitted_ids: List[str] = []
        self._load()

    # ── Public API ──────────────────────────────────────────────────

    def can_submit(self) -> bool:
        """Check if we can submit today (under daily limit)."""
        self._ensure_date()
        with self._lock:
            return self._count < self.daily_limit

    def remaining(self) -> int:
        """Number of submissions still available today."""
        self._ensure_date()
        with self._lock:
            return max(0, self.daily_limit - self._count)

    def record_submission(self, alpha_id: str):
        """Record a successful submission."""
        self._ensure_date()
        with self._lock:
            self._count += 1
            self._submitted_ids.append(alpha_id)
            self._save()

    def count_today(self) -> int:
        """Number of submissions already made today."""
        self._ensure_date()
        with self._lock:
            return self._count

    def last_submission_date(self) -> Optional[str]:
        """Return the date string of the last submission day, if any."""
        with self._lock:
            return self._date if self._count > 0 else None

    def is_already_submitted_today(self) -> bool:
        """True if any submission has already occurred today."""
        self._ensure_date()
        with self._lock:
            return self._count > 0

    # ── Internal ────────────────────────────────────────────────────

    def _ensure_date(self):
        today = datetime.now().date().isoformat()
        with self._lock:
            if self._date != today:
                self._date = today
                self._count = 0
                self._submitted_ids = []

    def _load(self):
        if os.path.exists(self.quota_file):
            try:
                with open(self.quota_file, "r") as f:
                    data = json.load(f)
                self._date = data.get("last_submission_date", "")
                self._count = data.get("submission_count", 0)
                self._submitted_ids = data.get("submitted_ids", [])
                self.daily_limit = data.get("daily_limit", self.daily_limit)
                self._ensure_date()
                logger.info(
                    "Loaded quota: %d/%d submissions today (%s)",
                    self._count,
                    self.daily_limit,
                    self._date,
                )
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("Could not load quota file: %s", e)

    def _save(self):
        os.makedirs(os.path.dirname(self.quota_file), exist_ok=True)
        data = {
            "last_submission_date": self._date,
            "submission_count": self._count,
            "submitted_ids": self._submitted_ids[-200:],  # keep last 200
            "daily_limit": self.daily_limit,
            "updated_at": datetime.now().isoformat(),
        }
        with open(self.quota_file, "w") as f:
            json.dump(data, f, indent=2)


# ── Global singleton ─────────────────────────────────────────────────

_quota_instance: Optional[SubmissionQuota] = None
_quota_lock = threading.Lock()


def get_submission_quota(daily_limit: int = 10) -> SubmissionQuota:
    """Return the process-wide singleton SubmissionQuota."""
    global _quota_instance
    with _quota_lock:
        if _quota_instance is None:
            _quota_instance = SubmissionQuota(daily_limit=daily_limit)
        return _quota_instance
```

----------------------------------------

## File: `core\__init__.py`

```python
from .config import load_credentials
from .api_session import get_session, WorldQuantSession
from .llm_client import get_llm_client, LLMClient
from .data_fetcher import DataFetcher, fetch_and_save_all
from .alpha_db import get_alpha_db
from .submission_quota import get_submission_quota
from .log_manager import setup_logger

```

----------------------------------------

