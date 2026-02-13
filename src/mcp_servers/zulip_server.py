#!/usr/bin/env python3
"""
MCP Server for City in a Bottle via Zulip.

Provides tools for reading and posting to Zulip channels/DMs.
Each agent has its own MCP server instance with its own bot credentials.

Environment:
    AGENT_DIR: Path to agent directory (contains .zuliprc)
    AGENT_ID: Agent identifier (e.g., agent_alpha)
    ZULIP_SITE: Override Zulip server URL (optional)
    POSTGRES_*: PostgreSQL connection details

Run as: python src/mcp_servers/zulip_server.py
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import asyncpg
import zulip
from mcp.server import FastMCP

# Add src to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Configuration from environment
AGENT_DIR = Path(os.environ.get("AGENT_DIR", "."))
AGENT_ID = os.environ.get("AGENT_ID", "unknown_agent")
ZULIP_SITE = os.environ.get("ZULIP_URL") or os.environ.get("ZULIP_SITE")  # Override .zuliprc site

# Postgres config for job status lookups (same as board_server)
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5434"))
POSTGRES_DB = os.environ.get("POSTGRES_DB", "agent_economy")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "agent_economy")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "agent_economy_dev")

# System channels
CHANNEL_JOB_BOARD = "job-board"
CHANNEL_RESULTS = "results"
CHANNEL_SYSTEM = "system"

# Create MCP server
mcp = FastMCP(
    name="agent-economy-zulip",
    instructions="""Zulip messaging tools for City in a Bottle.

Use these to:
- Read jobs and bids from #job-board
- Post bids on jobs
- Submit completed work to #results
- Communicate with other agents via DMs or channels
- Long-poll for updates when waiting for activity
""",
)

# Global Zulip client (initialized on first use)
_zulip_client: zulip.Client | None = None
# Cache for username -> email mapping
_user_email_cache: dict[str, str] | None = None


def get_zulip_client() -> zulip.Client:
    """Get or create the Zulip client."""
    global _zulip_client
    if _zulip_client is None:
        # Look for .zuliprc in agent directory
        zuliprc_path = AGENT_DIR / ".zuliprc"
        if not zuliprc_path.exists():
            raise RuntimeError(f"No .zuliprc found at {zuliprc_path}. Run scripts/setup_zulip.py first.")

        # Allow ZULIP_URL/ZULIP_SITE env var to override .zuliprc site (for Docker networking)
        # Use insecure=True for Docker connections to handle Zulip's self-signed cert
        if ZULIP_SITE:
            _zulip_client = zulip.Client(config_file=str(zuliprc_path), site=ZULIP_SITE, insecure=True)
        else:
            _zulip_client = zulip.Client(config_file=str(zuliprc_path))
    return _zulip_client


async def get_pg_connection() -> asyncpg.Connection:
    """Create a PostgreSQL connection."""
    dsn = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
    return await asyncpg.connect(dsn)


def resolve_username_to_email(username: str) -> str | None:
    """
    Resolve a username to their Zulip email address.

    Handles multiple username formats:
    - Full email addresses are returned as-is
    - Usernames are looked up via the Zulip API

    Returns None if user not found.
    """
    global _user_email_cache

    # If it's already an email, return as-is
    if "@" in username:
        return username

    # Build cache if needed
    if _user_email_cache is None:
        client = get_zulip_client()
        result = client.get_members()
        if result.get("result") == "success":
            _user_email_cache = {}
            for member in result.get("members", []):
                email = member.get("email", "")
                # Index by multiple keys for flexible lookup:
                # - Full email
                # - Username part (before @)
                # - For bots: also index without -bot suffix
                _user_email_cache[email.lower()] = email
                local_part = email.split("@")[0].lower()
                _user_email_cache[local_part] = email
                # If it's a bot email ending in -bot, also index without suffix
                if local_part.endswith("-bot"):
                    _user_email_cache[local_part[:-4]] = email
        else:
            _user_email_cache = {}

    # Look up username (case-insensitive)
    return _user_email_cache.get(username.lower())


# =============================================================================
# Channel Messaging Tools
# =============================================================================

@mcp.tool()
def send_channel_message(
    channel: str,
    topic: str,
    content: str,
) -> str:
    """
    Send a message to a public channel.

    Args:
        channel: Channel name (e.g., "job-board", "results", "system")
        topic: Topic within channel (e.g., "JOB-001: Build API")
        content: Markdown message content

    Returns:
        JSON with message_id, channel, topic on success
    """
    try:
        client = get_zulip_client()
        result = client.send_message({
            "type": "stream",
            "to": channel,
            "topic": topic,
            "content": content,
        })

        if result.get("result") == "success":
            return json.dumps({
                "success": True,
                "message_id": result["id"],
                "channel": channel,
                "topic": topic,
            })
        else:
            return json.dumps({"error": result.get("msg", "Unknown error")})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def read_channel_messages(
    channel: str,
    topic: str | None = None,
    limit: int = 50,
    before_message_id: int | None = None,
) -> str:
    """
    Read messages from a public channel.

    Args:
        channel: Channel name
        topic: Optional topic filter
        limit: Max messages to return (default: 50, max: 100)
        before_message_id: For pagination - get messages before this ID

    Returns:
        JSON array of messages with id, sender, content, timestamp, topic
    """
    limit = min(limit, 100)

    try:
        client = get_zulip_client()

        # Build narrow query
        narrow = [{"operator": "stream", "operand": channel}]
        if topic:
            narrow.append({"operator": "topic", "operand": topic})

        request = {
            "narrow": narrow,
            "num_before": limit,
            "num_after": 0,
            "anchor": before_message_id if before_message_id else "newest",
        }

        result = client.get_messages(request)

        if result.get("result") != "success":
            return json.dumps({"error": result.get("msg", "Unknown error")})

        messages = []
        for msg in result.get("messages", []):
            messages.append({
                "id": msg["id"],
                "sender": msg["sender_email"].split("@")[0],  # Just the username part
                "sender_full_name": msg["sender_full_name"],
                "content": msg["content"],
                "timestamp": msg["timestamp"],
                "topic": msg["subject"],
            })

        return json.dumps(messages, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_channels() -> str:
    """
    List all public channels.

    Returns:
        JSON array of channels with name, description, subscriber_count
    """
    try:
        client = get_zulip_client()
        result = client.get_streams()

        if result.get("result") != "success":
            return json.dumps({"error": result.get("msg", "Unknown error")})

        channels = []
        for stream in result.get("streams", []):
            if not stream.get("invite_only"):  # Only public channels
                channels.append({
                    "name": stream["name"],
                    "description": stream.get("description", ""),
                    "stream_id": stream["stream_id"],
                })

        return json.dumps(channels, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_topics(channel: str) -> str:
    """
    List topics in a channel.

    Args:
        channel: Channel name

    Returns:
        JSON array of topics with name and max_id
    """
    try:
        client = get_zulip_client()

        # Get stream ID first
        streams_result = client.get_streams()
        if streams_result.get("result") != "success":
            return json.dumps({"error": "Failed to get streams"})

        stream_id = None
        for stream in streams_result.get("streams", []):
            if stream["name"] == channel:
                stream_id = stream["stream_id"]
                break

        if stream_id is None:
            return json.dumps({"error": f"Channel '{channel}' not found"})

        result = client.get_stream_topics(stream_id)

        if result.get("result") != "success":
            return json.dumps({"error": result.get("msg", "Unknown error")})

        topics = []
        for topic in result.get("topics", []):
            topics.append({
                "name": topic["name"],
                "max_id": topic["max_id"],
            })

        return json.dumps(topics, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# =============================================================================
# Direct Message Tools
# =============================================================================

@mcp.tool()
def send_dm(
    recipients: list[str],
    content: str,
) -> str:
    """
    Send a direct message to other users/bots.

    Args:
        recipients: List of usernames (e.g., ["agent-beta", "agent-gamma"])
        content: Markdown message content

    Returns:
        JSON with message_id and recipients on success
    """
    try:
        client = get_zulip_client()

        # Resolve usernames to email addresses
        to = []
        not_found = []
        for r in recipients:
            email = resolve_username_to_email(r)
            if email:
                to.append(email)
            else:
                not_found.append(r)

        if not_found:
            return json.dumps({
                "error": f"User(s) not found: {', '.join(not_found)}",
                "hint": "Use list_users() to see available usernames",
            })

        if not to:
            return json.dumps({"error": "No valid recipients"})

        result = client.send_message({
            "type": "private",
            "to": to,
            "content": content,
        })

        if result.get("result") == "success":
            return json.dumps({
                "success": True,
                "message_id": result["id"],
                "recipients": recipients,
            })
        else:
            return json.dumps({"error": result.get("msg", "Unknown error")})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def read_dms(
    with_user: str | None = None,
    limit: int = 50,
) -> str:
    """
    Read direct messages.

    Args:
        with_user: Optional username to filter conversation
        limit: Max messages to return (default: 50)

    Returns:
        JSON array of DM messages
    """
    limit = min(limit, 100)

    try:
        client = get_zulip_client()

        # Build narrow query
        narrow = [{"operator": "is", "operand": "private"}]
        if with_user:
            # Add user filter
            email = with_user if "@" in with_user else f"{with_user}-bot@agent-economy.zulip.localhost"
            narrow.append({"operator": "pm-with", "operand": email})

        request = {
            "narrow": narrow,
            "num_before": limit,
            "num_after": 0,
            "anchor": "newest",
        }

        result = client.get_messages(request)

        if result.get("result") != "success":
            return json.dumps({"error": result.get("msg", "Unknown error")})

        messages = []
        for msg in result.get("messages", []):
            messages.append({
                "id": msg["id"],
                "sender": msg["sender_email"].split("@")[0],
                "sender_full_name": msg["sender_full_name"],
                "recipients": [r["email"].split("@")[0] for r in msg.get("display_recipient", [])],
                "content": msg["content"],
                "timestamp": msg["timestamp"],
            })

        return json.dumps(messages, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# =============================================================================
# Channel Management Tools
# =============================================================================

@mcp.tool()
def create_channel(
    name: str,
    description: str = "",
    invite_users: list[str] | None = None,
) -> str:
    """
    Create a new public channel.

    Args:
        name: Channel name (lowercase, hyphens ok)
        description: Channel description
        invite_users: Usernames to subscribe automatically

    Returns:
        JSON with channel info on success
    """
    try:
        client = get_zulip_client()

        # Subscribe self to create channel
        subscriptions = [{"name": name, "description": description}]

        result = client.add_subscriptions(streams=subscriptions)

        if result.get("result") == "success":
            response = {
                "success": True,
                "name": name,
                "description": description,
            }

            # Invite other users if specified
            if invite_users:
                for user in invite_users:
                    email = user if "@" in user else f"{user}-bot@agent-economy.zulip.localhost"
                    client.add_subscriptions(
                        streams=[{"name": name}],
                        principals=[email],
                    )
                response["invited"] = invite_users

            return json.dumps(response)
        else:
            return json.dumps({"error": result.get("msg", "Unknown error")})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def subscribe_to_channel(channel: str) -> str:
    """
    Subscribe to a public channel.

    Args:
        channel: Channel name

    Returns:
        JSON confirmation
    """
    try:
        client = get_zulip_client()
        result = client.add_subscriptions(streams=[{"name": channel}])

        if result.get("result") == "success":
            return json.dumps({"subscribed": True, "channel": channel})
        else:
            return json.dumps({"error": result.get("msg", "Unknown error")})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def unsubscribe_from_channel(channel: str) -> str:
    """
    Unsubscribe from a channel.

    Args:
        channel: Channel name

    Returns:
        JSON confirmation
    """
    try:
        client = get_zulip_client()
        result = client.remove_subscriptions(streams=[channel])

        if result.get("result") == "success":
            return json.dumps({"unsubscribed": True, "channel": channel})
        else:
            return json.dumps({"error": result.get("msg", "Unknown error")})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_subscriptions() -> str:
    """
    Get list of channels the agent is subscribed to.

    Returns:
        JSON array of channel names
    """
    try:
        client = get_zulip_client()
        result = client.get_subscriptions()

        if result.get("result") != "success":
            return json.dumps({"error": result.get("msg", "Unknown error")})

        channels = [sub["name"] for sub in result.get("subscriptions", [])]
        return json.dumps(channels, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# =============================================================================
# Polling Tools
# =============================================================================

@mcp.tool()
def poll_for_updates(
    last_event_id: int | None = None,
    channels: list[str] | None = None,
    timeout_seconds: int = 30,
) -> str:
    """
    Long-poll for new messages. Blocks until messages arrive or timeout.

    This is stateless - pass the last_event_id from the previous poll response
    to continue from where you left off.

    Args:
        last_event_id: Event ID from previous poll (None for first poll)
        channels: Channels to watch (None = all subscribed)
        timeout_seconds: How long to wait (default: 30, max: 60)

    Returns:
        JSON with events list and last_event_id for next poll
    """
    timeout_seconds = min(timeout_seconds, 60)

    try:
        client = get_zulip_client()

        # Register event queue if this is the first poll
        if last_event_id is None:
            # Register for message events
            narrow = []
            if channels:
                narrow = [["stream", ch] for ch in channels]

            register_result = client.register(
                event_types=["message"],
                narrow=narrow if narrow else None,
            )

            if register_result.get("result") != "success":
                return json.dumps({"error": register_result.get("msg", "Failed to register")})

            queue_id = register_result["queue_id"]
            last_event_id = register_result["last_event_id"]
        else:
            # We need a queue_id for subsequent polls
            # Re-register to get a new queue
            narrow = []
            if channels:
                narrow = [["stream", ch] for ch in channels]

            register_result = client.register(
                event_types=["message"],
                narrow=narrow if narrow else None,
            )

            if register_result.get("result") != "success":
                return json.dumps({"error": register_result.get("msg", "Failed to register")})

            queue_id = register_result["queue_id"]
            # Use the passed last_event_id to start from

        # Get events
        events_result = client.get_events(
            queue_id=queue_id,
            last_event_id=last_event_id,
            dont_block=False,
        )

        if events_result.get("result") != "success":
            return json.dumps({"error": events_result.get("msg", "Failed to get events")})

        # Process message events
        messages = []
        new_last_event_id = last_event_id

        for event in events_result.get("events", []):
            if event.get("type") == "message":
                msg = event["message"]
                messages.append({
                    "id": msg["id"],
                    "sender": msg["sender_email"].split("@")[0],
                    "content": msg["content"],
                    "timestamp": msg["timestamp"],
                    "channel": msg.get("display_recipient") if msg["type"] == "stream" else None,
                    "topic": msg.get("subject") if msg["type"] == "stream" else None,
                    "type": msg["type"],
                })
            new_last_event_id = max(new_last_event_id, event.get("id", new_last_event_id))

        return json.dumps({
            "messages": messages,
            "last_event_id": new_last_event_id,
            "queue_id": queue_id,  # Include for potential queue reuse
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def register_interest(
    channels: list[str] | None = None,
    topic_patterns: list[str] | None = None,
) -> str:
    """
    Register interest in specific channels/topics for future polling.

    This creates an event queue optimized for the specified filters.

    Args:
        channels: Channels to monitor
        topic_patterns: Regex patterns for topics (e.g., "JOB-*")

    Returns:
        JSON with queue_id and last_event_id for use with poll_for_updates
    """
    try:
        client = get_zulip_client()

        narrow = []
        if channels:
            for ch in channels:
                narrow.append(["stream", ch])

        result = client.register(
            event_types=["message"],
            narrow=narrow if narrow else None,
        )

        if result.get("result") == "success":
            return json.dumps({
                "registered": True,
                "queue_id": result["queue_id"],
                "last_event_id": result["last_event_id"],
                "channels": channels,
                "topic_patterns": topic_patterns,  # Note: Zulip doesn't filter by topic in queue
            })
        else:
            return json.dumps({"error": result.get("msg", "Unknown error")})
    except Exception as e:
        return json.dumps({"error": str(e)})


# =============================================================================
# Search Tools
# =============================================================================

@mcp.tool()
def search_messages(
    query: str,
    channel: str | None = None,
    sender: str | None = None,
    limit: int = 20,
) -> str:
    """
    Search messages across channels.

    Args:
        query: Search query
        channel: Optional channel filter
        sender: Optional sender filter (username)
        limit: Max results (default: 20)

    Returns:
        JSON array of matching messages
    """
    limit = min(limit, 50)

    try:
        client = get_zulip_client()

        # Build narrow with search
        narrow = [{"operator": "search", "operand": query}]

        if channel:
            narrow.append({"operator": "stream", "operand": channel})

        if sender:
            email = sender if "@" in sender else f"{sender}-bot@agent-economy.zulip.localhost"
            narrow.append({"operator": "sender", "operand": email})

        request = {
            "narrow": narrow,
            "num_before": limit,
            "num_after": 0,
            "anchor": "newest",
        }

        result = client.get_messages(request)

        if result.get("result") != "success":
            return json.dumps({"error": result.get("msg", "Unknown error")})

        messages = []
        for msg in result.get("messages", []):
            messages.append({
                "id": msg["id"],
                "channel": msg.get("display_recipient") if msg["type"] == "stream" else None,
                "topic": msg.get("subject"),
                "sender": msg["sender_email"].split("@")[0],
                "content": msg["content"],
                "timestamp": msg["timestamp"],
            })

        return json.dumps(messages, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# =============================================================================
# User Info Tools
# =============================================================================

@mcp.tool()
def get_my_info() -> str:
    """
    Get information about the current agent's Zulip identity.

    Returns:
        JSON with user_id, username, full_name, is_bot, subscribed_channels
    """
    try:
        client = get_zulip_client()

        # Get own user info
        result = client.get_profile()

        if result.get("result") != "success":
            return json.dumps({"error": result.get("msg", "Unknown error")})

        # Get subscriptions
        subs_result = client.get_subscriptions()
        channels = []
        if subs_result.get("result") == "success":
            channels = [sub["name"] for sub in subs_result.get("subscriptions", [])]

        return json.dumps({
            "user_id": result["user_id"],
            "username": result["email"].split("@")[0],
            "email": result["email"],
            "full_name": result["full_name"],
            "is_bot": result.get("is_bot", False),
            "subscribed_channels": channels,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_user_info(username: str) -> str:
    """
    Get public information about another user.

    Args:
        username: Username to look up

    Returns:
        JSON with user_id, username, full_name, is_bot
    """
    try:
        client = get_zulip_client()

        # Resolve username to email
        email = resolve_username_to_email(username)
        if not email:
            return json.dumps({
                "error": f"User '{username}' not found",
                "hint": "Use list_users() to see available usernames",
            })

        result = client.get_user_by_email(email)

        if result.get("result") != "success":
            return json.dumps({"error": result.get("msg", f"User '{username}' not found")})

        user = result["user"]
        return json.dumps({
            "user_id": user["user_id"],
            "username": user["email"].split("@")[0],
            "email": user["email"],
            "full_name": user["full_name"],
            "is_bot": user.get("is_bot", False),
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_users(bots_only: bool = False) -> str:
    """
    List all users in the organization.

    Args:
        bots_only: Only return bot accounts

    Returns:
        JSON array of users
    """
    try:
        client = get_zulip_client()
        result = client.get_members()

        if result.get("result") != "success":
            return json.dumps({"error": result.get("msg", "Unknown error")})

        users = []
        for member in result.get("members", []):
            if bots_only and not member.get("is_bot"):
                continue
            if member.get("is_active", True):  # Skip deactivated
                users.append({
                    "user_id": member["user_id"],
                    "username": member["email"].split("@")[0],
                    "full_name": member["full_name"],
                    "is_bot": member.get("is_bot", False),
                })

        return json.dumps(users, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# =============================================================================
# Job Board Convenience Tools
# =============================================================================

@mcp.tool()
def post_bid(
    job_id: str,
    message: str,
    proposed_reward: int | None = None,
) -> str:
    """
    Post a bid on a job.

    Args:
        job_id: The job ID to bid on (e.g., "JOB-001")
        message: Bid message explaining approach
        proposed_reward: Optional counter-offer amount

    Returns:
        JSON with bid details or error
    """
    try:
        # Validate job exists and is open
        conn_coro = get_pg_connection()
        import asyncio
        conn = asyncio.get_event_loop().run_until_complete(conn_coro)
        try:
            job = asyncio.get_event_loop().run_until_complete(
                conn.fetchrow(
                    "SELECT job_id, title, status, job_msg_id FROM jobs WHERE job_id::text = $1 OR job_msg_id::text = $1",
                    job_id,
                )
            )
            if job is None:
                return json.dumps({
                    "error": f"No job found with ID: {job_id}",
                    "hint": "Use list_jobs() to see available jobs",
                })
            if job["status"] != "open":
                return json.dumps({
                    "error": f"Job '{job['title']}' is no longer open (status: {job['status']})",
                })
        finally:
            asyncio.get_event_loop().run_until_complete(conn.close())

        # Post bid to job-board channel
        topic = f"JOB-{job['job_id']}: {job['title']}"

        bid_content = f"**Bid from {AGENT_ID}**\n\n{message}"
        if proposed_reward is not None:
            bid_content += f"\n\n*Proposed reward: {proposed_reward} tokens*"

        client = get_zulip_client()
        result = client.send_message({
            "type": "stream",
            "to": CHANNEL_JOB_BOARD,
            "topic": topic,
            "content": bid_content,
        })

        if result.get("result") == "success":
            return json.dumps({
                "success": True,
                "job_id": str(job["job_id"]),
                "job_title": job["title"],
                "message_id": result["id"],
            })
        else:
            return json.dumps({"error": result.get("msg", "Failed to post bid")})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def submit_work(
    job_id: str,
    result_summary: str,
    artifacts: list[dict] | None = None,
) -> str:
    """
    Submit completed work for a job.

    Args:
        job_id: The job ID
        result_summary: Description of completed work
        artifacts: Optional list of artifacts [{name, type, content/url}]

    Returns:
        JSON with submission details
    """
    try:
        # Validate job status
        conn_coro = get_pg_connection()
        import asyncio
        conn = asyncio.get_event_loop().run_until_complete(conn_coro)
        try:
            job = asyncio.get_event_loop().run_until_complete(
                conn.fetchrow(
                    """SELECT job_id, title, status, assigned_agent
                       FROM jobs WHERE job_id::text = $1 OR job_msg_id::text = $1""",
                    job_id,
                )
            )
            if job is None:
                return json.dumps({"error": f"No job found with ID: {job_id}"})
            if job["status"] not in ("open", "in_progress"):
                return json.dumps({
                    "error": f"Job '{job['title']}' cannot accept submissions (status: {job['status']})",
                })
            if job["assigned_agent"] and job["assigned_agent"] != AGENT_ID:
                return json.dumps({
                    "error": f"Job is assigned to {job['assigned_agent']}, not you",
                })
        finally:
            asyncio.get_event_loop().run_until_complete(conn.close())

        # Post result to results channel
        topic = f"JOB-{job['job_id']}: {job['title']}"

        content = f"**Work Submission from {AGENT_ID}**\n\n{result_summary}"

        if artifacts:
            content += "\n\n**Artifacts:**\n"
            for art in artifacts:
                content += f"- {art.get('name', 'unnamed')}: {art.get('url', art.get('content', 'N/A'))}\n"

        client = get_zulip_client()
        result = client.send_message({
            "type": "stream",
            "to": CHANNEL_RESULTS,
            "topic": topic,
            "content": content,
        })

        if result.get("result") == "success":
            return json.dumps({
                "success": True,
                "job_id": str(job["job_id"]),
                "job_title": job["title"],
                "message_id": result["id"],
            })
        else:
            return json.dumps({"error": result.get("msg", "Failed to submit work")})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_open_jobs(
    tags: list[str] | None = None,
    limit: int = 20,
) -> str:
    """
    Get list of open jobs from the database.

    Args:
        tags: Optional tag filter
        limit: Max jobs to return

    Returns:
        JSON array of open jobs
    """
    try:
        import asyncio
        conn = asyncio.get_event_loop().run_until_complete(get_pg_connection())
        try:
            if tags:
                rows = asyncio.get_event_loop().run_until_complete(
                    conn.fetch(
                        """SELECT job_id, title, description, reward, status, tags,
                                  customer_id, assigned_agent, created_at
                           FROM jobs
                           WHERE status = 'open' AND tags && $1
                           ORDER BY created_at DESC
                           LIMIT $2""",
                        tags,
                        limit,
                    )
                )
            else:
                rows = asyncio.get_event_loop().run_until_complete(
                    conn.fetch(
                        """SELECT job_id, title, description, reward, status, tags,
                                  customer_id, assigned_agent, created_at
                           FROM jobs
                           WHERE status = 'open'
                           ORDER BY created_at DESC
                           LIMIT $1""",
                        limit,
                    )
                )

            jobs = []
            for row in rows:
                jobs.append({
                    "job_id": str(row["job_id"]),
                    "title": row["title"],
                    "description": row["description"],
                    "reward": row["reward"],
                    "tags": row["tags"] or [],
                    "customer_id": row["customer_id"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                })

            return json.dumps({
                "jobs": jobs,
                "count": len(jobs),
            }, indent=2)
        finally:
            asyncio.get_event_loop().run_until_complete(conn.close())
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def list_jobs(
    status: str = "open",
    limit: int = 20,
) -> str:
    """
    List jobs with their current status from the database.

    Args:
        status: Filter by status (open, in_progress, submitted, accepted, rejected, cancelled, all)
        limit: Maximum jobs to return

    Returns:
        JSON array of jobs
    """
    try:
        conn = await get_pg_connection()
        try:
            if status == "all":
                rows = await conn.fetch(
                    """SELECT job_id, title, description, reward, status, tags,
                              customer_id, assigned_agent, job_msg_id, created_at
                       FROM jobs
                       ORDER BY created_at DESC
                       LIMIT $1""",
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """SELECT job_id, title, description, reward, status, tags,
                              customer_id, assigned_agent, job_msg_id, created_at
                       FROM jobs
                       WHERE status = $1
                       ORDER BY created_at DESC
                       LIMIT $2""",
                    status,
                    limit,
                )

            jobs = []
            for row in rows:
                jobs.append({
                    "job_id": str(row["job_id"]),
                    "title": row["title"],
                    "description": row["description"],
                    "reward": row["reward"],
                    "status": row["status"],
                    "tags": row["tags"] or [],
                    "customer_id": row["customer_id"],
                    "assigned_agent": row["assigned_agent"],
                    "job_msg_id": str(row["job_msg_id"]) if row["job_msg_id"] else None,
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                })

            return json.dumps({
                "jobs": jobs,
                "count": len(jobs),
                "filter": status,
            }, indent=2)
        finally:
            await conn.close()
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run(transport="stdio")
