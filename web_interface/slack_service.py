import os
import time
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from datetime import datetime

# Global cache for messages to avoid hitting rate limits
# Structure: { 'timestamp': float, 'messages': [] }
_message_cache = {
    'timestamp': 0,
    'messages': []
}
CACHE_DURATION = 300  # 5 minutes

def get_slack_client():
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return None
    return WebClient(token=token)

def get_recent_messages(limit=5):
    """
    Fetches recent messages from the configured Slack channel.
    Returns a list of dicts: {'text': str, 'user': str, 'ts': str, 'date_str': str}
    """
    global _message_cache
    
    # Check cache
    if time.time() - _message_cache['timestamp'] < CACHE_DURATION:
        return _message_cache['messages']

    client = get_slack_client()
    channel_id = os.environ.get("SLACK_CHANNEL_ID")

    if not client or not channel_id:
        # Return mock data/instruction if credentials are missing
        return [{
            'text': 'Slack integration not configured. Please set SLACK_BOT_TOKEN and SLACK_CHANNEL_ID environment variables.',
            'user': 'System',
            'ts': str(time.time()),
            'date_str': datetime.now().strftime('%Y-%m-%d %H:%M')
        }]

    try:
        result = client.conversations_history(channel=channel_id, limit=limit)
        messages = result.get('messages', [])
        
        formatted_messages = []
        for msg in messages:
            # Skip subtypes like channel_join, etc., if needed. keeping simple for now.
            if 'text' in msg:
                ts = float(msg.get('ts', 0))
                date_str = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
                
                # Try to get user info if needed, but 'user' ID is what we have. 
                # resolving every user might be slow, so we can just use the ID or look it up if we cache users.
                # For now, just use the ID or 'Bot' if username is missing.
                user_id = msg.get('user', 'Unknown')
                
                formatted_messages.append({
                    'text': msg.get('text'),
                    'user': user_id, 
                    'ts': msg.get('ts'),
                    'date_str': date_str
                })
        
        # Update cache
        _message_cache['timestamp'] = time.time()
        _message_cache['messages'] = formatted_messages
        return formatted_messages

    except SlackApiError as e:
        print(f"Error fetching Slack messages: {e}")
        return [{
            'text': f"Error fetching messages: {e}",
            'user': 'System',
            'ts': str(time.time()),
            'date_str': datetime.now().strftime('%Y-%m-%d %H:%M')
        }]
