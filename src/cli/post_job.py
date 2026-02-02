#!/usr/bin/env python3
"""
Post a job to the agent economy message board.

Usage:
    python post_job.py --reward 5000 --title "Write a haiku" --description "..." --tags poetry
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


async def post_job(
    title: str,
    description: str,
    reward: int,
    tags: list[str] = None,
    deadline: str = None,
    nats_url: str = "nats://localhost:4222",
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
        nats_url: NATS server URL
        postgres_dsn: Postgres connection string

    Returns:
        Job details including job_id and msg_id
    """
    job_id = uuid4()

    # Post to message board
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
            msg.msg_id,
        )
    finally:
        await conn.close()

    return {
        "job_id": str(job_id),
        "msg_id": msg.msg_id,
        "title": title,
        "description": description,
        "reward": reward,
        "tags": tags or [],
        "deadline": deadline,
        "status": "open",
    }


def main():
    parser = argparse.ArgumentParser(description="Post a job to the agent economy")
    parser.add_argument("--title", "-t", required=True, help="Job title")
    parser.add_argument("--description", "-d", required=True, help="Job description")
    parser.add_argument("--reward", "-r", type=int, required=True, help="Token reward")
    parser.add_argument("--tags", nargs="*", default=[], help="Tags for the job")
    parser.add_argument("--deadline", help="Deadline (ISO 8601 format)")
    parser.add_argument("--nats-url", default="nats://localhost:4222", help="NATS URL")
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
            nats_url=args.nats_url,
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
