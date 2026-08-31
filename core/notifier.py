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
