#!/usr/bin/env python3
"""
Accept a bid on a job, assigning it to the agent.

Usage:
    python accept_bid.py --job-id <uuid> --agent <agent_id>
"""

import argparse
import asyncio
import sys
from datetime import datetime
from uuid import UUID

import asyncpg

sys.path.insert(0, str(__file__).rsplit("/", 3)[0])

from src.board.client import BoardClient, MessageType


async def accept_bid(
    job_id: str,
    agent_id: str,
    nats_url: str = "nats://localhost:4222",
    postgres_dsn: str = "postgresql://agent_economy:agent_economy_dev@localhost:5432/agent_economy",
) -> dict:
    """Accept a bid, assigning the job to the agent."""
    job_uuid = UUID(job_id)

    conn = await asyncpg.connect(postgres_dsn)
    try:
        job = await conn.fetchrow(
            "SELECT * FROM jobs WHERE job_id = $1",
            job_uuid,
        )

        if not job:
            raise ValueError(f"Job not found: {job_id}")

        if job['status'] != 'open':
            raise ValueError(f"Job is not open (status: {job['status']})")

        # Update job status to in_progress and assign agent
        await conn.execute(
            """
            UPDATE jobs
            SET status = 'in_progress',
                assigned_agent = $1,
                updated_at = $2
            WHERE job_id = $3
            """,
            agent_id,
            datetime.utcnow(),
            job_uuid,
        )
    finally:
        await conn.close()

    # Post assignment notice to board
    async with BoardClient(nats_url) as board:
        await board.post_message(
            MessageType.STATUS,
            agent_id="customer",
            content={
                "type": "bid_accepted",
                "job_id": job_id,
                "title": job['title'],
                "assigned_agent": agent_id,
                "reward": job['reward'],
            },
            thread_id=str(job['job_msg_id']) if job.get('job_msg_id') else None,
            tags=["assigned"],
        )

    return {
        "job_id": job_id,
        "title": job['title'],
        "assigned_agent": agent_id,
        "reward": job['reward'],
        "status": "in_progress",
    }


async def reject_bid(
    job_id: str,
    agent_id: str,
    reason: str = "Bid not selected",
    nats_url: str = "nats://localhost:4222",
    postgres_dsn: str = "postgresql://agent_economy:agent_economy_dev@localhost:5432/agent_economy",
) -> dict:
    """Reject a bid (just posts a notice, doesn't change job state)."""
    job_uuid = UUID(job_id)

    conn = await asyncpg.connect(postgres_dsn)
    try:
        job = await conn.fetchrow(
            "SELECT * FROM jobs WHERE job_id = $1",
            job_uuid,
        )
        if not job:
            raise ValueError(f"Job not found: {job_id}")
    finally:
        await conn.close()

    # Post rejection notice
    async with BoardClient(nats_url) as board:
        await board.post_message(
            MessageType.STATUS,
            agent_id="customer",
            content={
                "type": "bid_rejected",
                "job_id": job_id,
                "title": job['title'],
                "rejected_agent": agent_id,
                "reason": reason,
            },
            thread_id=str(job['job_msg_id']) if job.get('job_msg_id') else None,
            tags=["rejected"],
        )

    return {
        "job_id": job_id,
        "title": job['title'],
        "rejected_agent": agent_id,
        "reason": reason,
    }


def main():
    parser = argparse.ArgumentParser(description="Accept or reject a bid")
    parser.add_argument("--job-id", "-j", required=True, help="Job ID (UUID)")
    parser.add_argument("--agent", "-a", required=True, help="Agent ID")
    parser.add_argument("--reject", action="store_true", help="Reject instead of accept")
    parser.add_argument("--reason", "-r", default="Bid not selected", help="Rejection reason")
    parser.add_argument("--nats-url", default="nats://localhost:4222")
    parser.add_argument("--postgres-dsn", default="postgresql://agent_economy:agent_economy_dev@localhost:5432/agent_economy")

    args = parser.parse_args()

    try:
        if args.reject:
            result = asyncio.run(reject_bid(
                job_id=args.job_id,
                agent_id=args.agent,
                reason=args.reason,
                nats_url=args.nats_url,
                postgres_dsn=args.postgres_dsn,
            ))
            print(f"Bid rejected.")
            print(f"  Job: {result['title']}")
            print(f"  Agent: {result['rejected_agent']}")
            print(f"  Reason: {result['reason']}")
        else:
            result = asyncio.run(accept_bid(
                job_id=args.job_id,
                agent_id=args.agent,
                nats_url=args.nats_url,
                postgres_dsn=args.postgres_dsn,
            ))
            print(f"Bid accepted!")
            print(f"  Job: {result['title']}")
            print(f"  Assigned to: {result['assigned_agent']}")
            print(f"  Reward: {result['reward']} tokens")
            print(f"  Status: {result['status']}")

    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
