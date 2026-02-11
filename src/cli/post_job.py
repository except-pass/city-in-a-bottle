#!/usr/bin/env python3
"""
Post a job to the City in a Bottle message board.

Usage:
    python post_job.py --reward 5000 --title "Write a haiku" --description "..." --tags poetry

For Zulip:
    python post_job.py --message-bus zulip --reward 5000 --title "Write a haiku" --description "..."
"""

import argparse
import asyncio
import json
import sys
from uuid import uuid4

import asyncpg

# Add parent to path for imports
sys.path.insert(0, str(__file__).rsplit("/", 3)[0])

from src.board.client import BoardClient, MessageType

# Optional Zulip support
try:
    import zulip
    ZULIP_AVAILABLE = True
except ImportError:
    ZULIP_AVAILABLE = False


async def post_job(
    title: str,
    description: str,
    reward: int,
    tags: list[str] = None,
    deadline: str = None,
    message_bus: str = "nats",
    nats_url: str = "nats://localhost:4222",
    zulip_config: str = None,  # Path to .zuliprc or None
    zulip_url: str = "http://localhost:8080",
    postgres_dsn: str = "postgresql://agent_economy:agent_economy_dev@localhost:5432/agent_economy",
) -> dict:
    """
    Post a job to the board and record it in the database.

    Args:
        title: Job title
        description: Job description
        reward: Token reward for completing the job
        tags: Optional list of tags
        deadline: Optional deadline (ISO 8601)
        message_bus: "nats" or "zulip"
        nats_url: NATS server URL (for NATS mode)
        zulip_config: Path to admin .zuliprc file (for Zulip mode)
        zulip_url: Zulip server URL
        postgres_dsn: Postgres connection string

    Returns:
        Job details including job_id and msg_id
    """
    job_id = uuid4()
    msg_id = None

    if message_bus == "zulip":
        if not ZULIP_AVAILABLE:
            raise RuntimeError("Zulip package not installed. Run: pip install zulip")

        # Create Zulip client
        if zulip_config:
            client = zulip.Client(config_file=zulip_config)
        else:
            # Use environment or default admin credentials
            client = zulip.Client(
                email="admin@agent-economy.local",
                api_key="",  # Will fail if not configured
                site=zulip_url,
            )

        # Build job message content
        topic = f"JOB-{job_id}: {title}"
        content = f"""**New Job Posted**

**Title:** {title}
**Reward:** {reward} tokens
**Job ID:** {job_id}

{description}
"""
        if tags:
            content += f"\n**Tags:** {', '.join(tags)}"
        if deadline:
            content += f"\n**Deadline:** {deadline}"

        content += "\n\n---\n*Reply to this topic to bid on this job.*"

        # Post to job-board channel
        result = client.send_message({
            "type": "stream",
            "to": "job-board",
            "topic": topic,
            "content": content,
        })

        if result.get("result") == "success":
            msg_id = str(result["id"])
        else:
            raise RuntimeError(f"Failed to post to Zulip: {result.get('msg', 'Unknown error')}")
    else:
        # NATS mode
        async with BoardClient(nats_url) as board:
            content = {
                "job_id": str(job_id),
                "title": title,
                "description": description,
                "reward": reward,
                "deadline": deadline,
            }

            msg = await board.post_message(
                MessageType.JOB,
                agent_id="customer",  # Jobs come from "customer"
                content=content,
                tags=tags or [],
            )
            msg_id = msg.msg_id

    # Record in database
    conn = await asyncpg.connect(postgres_dsn)
    try:
        await conn.execute(
            """
            INSERT INTO jobs
                (job_id, title, description, reward, tags, deadline, status, job_msg_id)
            VALUES ($1, $2, $3, $4, $5, $6, 'open', $7)
            """,
            job_id,
            title,
            description,
            reward,
            tags or [],
            deadline,
            msg_id,
        )
    finally:
        await conn.close()

    return {
        "job_id": str(job_id),
        "msg_id": msg_id,
        "title": title,
        "description": description,
        "reward": reward,
        "tags": tags or [],
        "deadline": deadline,
        "status": "open",
    }


def main():
    parser = argparse.ArgumentParser(description="Post a job to City in a Bottle")
    parser.add_argument("--title", "-t", required=True, help="Job title")
    parser.add_argument("--description", "-d", required=True, help="Job description")
    parser.add_argument("--reward", "-r", type=int, required=True, help="Token reward")
    parser.add_argument("--tags", nargs="*", default=[], help="Tags for the job")
    parser.add_argument("--deadline", help="Deadline (ISO 8601 format)")
    parser.add_argument("--message-bus", default="nats", choices=["nats", "zulip"],
                       help="Message bus to use")
    parser.add_argument("--nats-url", default="nats://localhost:4222", help="NATS URL")
    parser.add_argument("--zulip-config", help="Path to admin .zuliprc file (for Zulip mode)")
    parser.add_argument("--zulip-url", default="http://localhost:8080", help="Zulip URL")
    parser.add_argument(
        "--postgres-dsn",
        default="postgresql://agent_economy:agent_economy_dev@localhost:5432/agent_economy",
        help="Postgres DSN",
    )

    args = parser.parse_args()

    result = asyncio.run(
        post_job(
            title=args.title,
            description=args.description,
            reward=args.reward,
            tags=args.tags,
            deadline=args.deadline,
            message_bus=args.message_bus,
            nats_url=args.nats_url,
            zulip_config=args.zulip_config,
            zulip_url=args.zulip_url,
            postgres_dsn=args.postgres_dsn,
        )
    )

    print("Job posted successfully!")
    print(f"  Job ID: {result['job_id']}")
    print(f"  Message ID: {result['msg_id']}")
    print(f"  Title: {result['title']}")
    print(f"  Reward: {result['reward']} tokens")
    if result['tags']:
        print(f"  Tags: {', '.join(result['tags'])}")
    if result['deadline']:
        print(f"  Deadline: {result['deadline']}")


if __name__ == "__main__":
    main()
