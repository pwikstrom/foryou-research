"""Recent messages from the configured Slack channel.

Reads ``SLACK_BOT_TOKEN`` / ``SLACK_CHANNEL_ID`` and caches the result for five
minutes to stay clear of Slack's rate limits."""

import os
import time
from datetime import UTC, datetime

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Global cache for messages to avoid hitting rate limits
# Structure: { 'timestamp': float, 'messages': [] }
_message_cache = {"timestamp": 0, "messages": []}
CACHE_DURATION = 300  # 5 minutes


def get_slack_client():
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return None
    return WebClient(token=token)


def get_recent_messages(limit=5):
    """
    Fetches recent messages from the configured Slack channel.
    Returns a list of dicts: {'text': str, 'user': str, 'ts': str, 'ts_iso': str}
    where ``ts_iso`` is the offset-aware instant the template renders.
    """
    global _message_cache

    # Check cache
    if time.time() - _message_cache["timestamp"] < CACHE_DURATION:
        return _message_cache["messages"]

    client = get_slack_client()
    channel_id = os.environ.get("SLACK_CHANNEL_ID")

    if not client or not channel_id:
        # Return mock data/instruction if credentials are missing
        return [
            {
                "text": "Slack integration not configured. Please set SLACK_BOT_TOKEN and SLACK_CHANNEL_ID environment variables.",
                "user": "System",
                "ts": str(time.time()),
                "ts_iso": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        ]

    try:
        result = client.conversations_history(channel=channel_id, limit=limit)
        messages = result.get("messages", [])

        formatted_messages = []
        for msg in messages:
            # Every message with text is kept, including subtypes such as
            # channel_join.
            if "text" in msg:
                ts = float(msg.get("ts", 0))
                ts_iso = datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")

                # The raw Slack user id is shown; resolving each id to a name
                # would cost an API call per user.
                user_id = msg.get("user", "Unknown")

                formatted_messages.append(
                    {
                        "text": msg.get("text"),
                        "user": user_id,
                        "ts": msg.get("ts"),
                        "ts_iso": ts_iso,
                    }
                )

        # Update cache
        _message_cache["timestamp"] = time.time()
        _message_cache["messages"] = formatted_messages
        return formatted_messages

    except SlackApiError as e:
        print(f"Error fetching Slack messages: {e}")
        return [
            {
                "text": f"Error fetching messages: {e}",
                "user": "System",
                "ts": str(time.time()),
                "ts_iso": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        ]
