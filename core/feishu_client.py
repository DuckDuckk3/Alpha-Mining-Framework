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
