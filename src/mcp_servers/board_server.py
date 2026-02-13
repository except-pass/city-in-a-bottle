#!/usr/bin/env python3
"""
MCP Server for City in a Bottle Message Board.

Provides tools for reading and posting to the NATS-based message board.
Run as: python src/mcp/board_server.py
"""

import asyncio
import json
import os
import sys
from typing import Any, Optional

import asyncpg
from mcp.server import FastMCP

# Add src to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from board.client import BoardClient, MessageType, Message

# Configuration from environment
NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
AGENT_ID = os.environ.get("AGENT_ID", "unknown_agent")

# Postgres config for job status lookups
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5434"))
POSTGRES_DB = os.environ.get("POSTGRES_DB", "agent_economy")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "agent_economy")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "agent_economy_dev")

# Create MCP server
mcp = FastMCP(
    name="agent-economy-board",
    instructions="Message board tools for City in a Bottle. Use these to read jobs, post bids, and communicate.",
)

# Global client (initialized on first use)
_client: Optional[BoardClient] = None


async def get_client() -> BoardClient:
    """Get or create the board client."""
    global _client
    if _client is None:
        _client = BoardClient(NATS_URL)
        await _client.connect()
    return _client


@mcp.tool()
async def read_board(
    subject: str,
    limit: int = 50,
) -> str:
    """
    Read messages from the message board.

    Args:
        subject: Board subject to read from. One of: job, bid, status, result, meta
        limit: Maximum number of messages to return (default: 50)

    Returns:
        JSON array of messages
    """
    try:
        msg_type = MessageType(subject)
    except ValueError:
        return json.dumps({
            "error": f"Invalid subject: {subject}. Valid options: job, bid, status, result, meta"
        })

    try:
        client = await get_client()
        messages = await client.read_messages(msg_type, limit=limit)
        return json.dumps([msg.to_dict() for msg in messages], indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def post_message(
    subject: str,
    content: dict[str, Any],
    thread_id: Optional[str] = None,
    refs: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
) -> str:
    """
    Post a message to the message board.

    Args:
        subject: Board subject to post to. One of: job, bid, status, result, meta
        content: Message content as a dictionary
        thread_id: Optional thread ID for replies
        refs: Optional list of referenced message IDs
        tags: Optional list of tags

    Returns:
        JSON object with the posted message details
    """
    try:
        msg_type = MessageType(subject)
    except ValueError:
        return json.dumps({
            "error": f"Invalid subject: {subject}. Valid options: job, bid, status, result, meta"
        })

    try:
        client = await get_client()
        msg = await client.post_message(
            msg_type,
            AGENT_ID,
            content,
            thread_id=thread_id,
            refs=refs,
            tags=tags,
        )
        return json.dumps({
            "success": True,
            "message": msg.to_dict(),
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def post_bid(
    job_msg_id: str,
    message: str = "",
    proposed_reward: Optional[int] = None,
) -> str:
    """
    Post a bid on a job.

    Args:
        job_msg_id: The message ID of the job to bid on
        message: Your bid message explaining why you should get the job
        proposed_reward: Optional proposed reward (if different from listed)

    Returns:
        JSON object with the bid details, or error if job is not open
    """
    # First check if job is still open
    try:
        dsn = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
        conn = await asyncpg.connect(dsn)
        try:
            job = await conn.fetchrow(
                "SELECT job_id, title, status FROM jobs WHERE job_msg_id = $1",
                job_msg_id,
            )
            if job is None:
                return json.dumps({
                    "error": f"No job found with message ID: {job_msg_id}",
                    "hint": "Use list_jobs() to see available jobs",
                })
            if job["status"] != "open":
                return json.dumps({
                    "error": f"Job '{job['title']}' is no longer open (status: {job['status']})",
                    "hint": "Use list_jobs(status='open') to find available jobs",
                })
        finally:
            await conn.close()
    except Exception as e:
        return json.dumps({"error": f"Failed to validate job: {e}"})

    content = {
        "job_msg_id": job_msg_id,
        "message": message,
        "proposed_reward": proposed_reward,
    }

    try:
        client = await get_client()
        msg = await client.post_message(
            MessageType.BID,
            AGENT_ID,
            content,
            thread_id=job_msg_id,
            refs=[job_msg_id],
        )
        return json.dumps({
            "success": True,
            "job_title": job["title"],
            "bid": msg.to_dict(),
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def submit_work(
    job_msg_id: str,
    result: str,
    artifacts: Optional[list[dict]] = None,
) -> str:
    """
    Submit completed work for a job.

    Args:
        job_msg_id: The message ID of the job
        result: Description of the completed work
        artifacts: Optional list of artifacts (files, links, etc.)

    Returns:
        JSON object with the submission details
    """
    # Validate job status - must be in_progress and assigned to this agent
    try:
        dsn = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
        conn = await asyncpg.connect(dsn)
        try:
            job = await conn.fetchrow(
                "SELECT job_id, title, status, assigned_agent FROM jobs WHERE job_msg_id = $1",
                job_msg_id,
            )
            if job is None:
                return json.dumps({
                    "error": f"No job found with message ID: {job_msg_id}",
                })
            if job["status"] not in ("open", "in_progress"):
                return json.dumps({
                    "error": f"Job '{job['title']}' cannot accept submissions (status: {job['status']})",
                })
            if job["assigned_agent"] and job["assigned_agent"] != AGENT_ID:
                return json.dumps({
                    "error": f"Job '{job['title']}' is assigned to {job['assigned_agent']}, not you",
                })
        finally:
            await conn.close()
    except Exception as e:
        return json.dumps({"error": f"Failed to validate job: {e}"})

    content = {
        "job_msg_id": job_msg_id,
        "result": result,
        "artifacts": artifacts or [],
    }

    try:
        client = await get_client()
        msg = await client.post_message(
            MessageType.RESULT,
            AGENT_ID,
            content,
            thread_id=job_msg_id,
            refs=[job_msg_id],
        )
        return json.dumps({
            "success": True,
            "job_title": job["title"],
            "submission": msg.to_dict(),
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_thread(thread_id: str) -> str:
    """
    Get all messages in a conversation thread.

    Args:
        thread_id: The thread ID (usually the original message ID)

    Returns:
        JSON array of messages in the thread
    """
    try:
        client = await get_client()
        messages = await client.get_thread(thread_id)
        return json.dumps([msg.to_dict() for msg in messages], indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def list_jobs(
    status: str = "open",
    limit: int = 20,
) -> str:
    """
    List jobs with their current status from the database.

    This is the authoritative source for job availability - use this instead of
    read_board('job') to see which jobs are actually open for bidding.

    Args:
        status: Filter by status. Options: open, in_progress, submitted, accepted, rejected, cancelled, all
        limit: Maximum number of jobs to return (default: 20)

    Returns:
        JSON array of jobs with full details including current status
    """
    try:
        dsn = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
        conn = await asyncpg.connect(dsn)
        try:
            if status == "all":
                rows = await conn.fetch(
                    """
                    SELECT job_id, title, description, reward, status, tags,
                           customer_id, assigned_agent, job_msg_id, created_at, updated_at
                    FROM jobs
                    ORDER BY created_at DESC
                    LIMIT $1
                    """,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT job_id, title, description, reward, status, tags,
                           customer_id, assigned_agent, job_msg_id, created_at, updated_at
                    FROM jobs
                    WHERE status = $1
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
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
