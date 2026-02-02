#!/usr/bin/env python3
"""
Reject submitted work (no reward).

Usage:
    python reject_work.py --job-id <uuid> --reason "Work did not meet requirements"
"""

import argparse
import asyncio
import sys
from datetime import datetime
from uuid import UUID

import asyncpg

# Add parent to path for imports
sys.path.insert(0, str(__file__).rsplit("/", 3)[0])

from src.board.client import BoardClient, MessageType


async def reject_work(
    job_id: str,
    reason: str,
    reopen: bool = False,
    nats_url: str = "nats://localhost:4222",
    postgres_dsn: str = "postgresql://agent_economy:agent_economy_dev@localhost:5432/agent_economy",
) -> dict:
    """
    Reject submitted work.

    Args:
        job_id: Job ID (UUID string)
        reason: Reason for rejection
        reopen: If True, reopen the job for new bids
        nats_url: NATS server URL
        postgres_dsn: Postgres connection string

    Returns:
        Result including job status
    """
    job_uuid = UUID(job_id)

    # Get job details
    conn = await asyncpg.connect(postgres_dsn)
    try:
        job = await conn.fetchrow(
            "SELECT * FROM jobs WHERE job_id = $1",
            job_uuid,
        )

        if not job:
            raise ValueError(f"Job not found: {job_id}")

        if job['status'] not in ('submitted', 'in_progress'):
            raise ValueError(f"Job cannot be rejected (status: {job['status']})")

        assigned_agent = job['assigned_agent']
        new_status = 'open' if reopen else 'rejected'

        # Update job status
        await conn.execute(
            """
            UPDATE jobs
            SET status = $1,
                assigned_agent = CASE WHEN $2 THEN NULL ELSE assigned_agent END,
                updated_at = $3
            WHERE job_id = $4
            """,
            new_status,
            reopen,
            datetime.utcnow(),
            job_uuid,
        )
    finally:
        await conn.close()

    # Post rejection notice to board
    async with BoardClient(nats_url) as board:
        await board.post_message(
            MessageType.META,
            agent_id="customer",
            content={
                "type": "rejection",
                "job_id": job_id,
                "agent_id": assigned_agent,
                "reason": reason,
                "reopened": reopen,
            },
            thread_id=str(job['job_msg_id']) if job.get('job_msg_id') else None,
            tags=["rejection"],
        )

    return {
        "job_id": job_id,
        "previous_agent": assigned_agent,
        "reason": reason,
        "status": new_status,
        "reopened": reopen,
    }


def main():
    parser = argparse.ArgumentParser(description="Reject submitted work")
    parser.add_argument("--job-id", "-j", required=True, help="Job ID (UUID)")
    parser.add_argument("--reason", "-r", required=True, help="Reason for rejection")
    parser.add_argument(
        "--reopen",
        action="store_true",
        help="Reopen the job for new bids",
    )
    parser.add_argument("--nats-url", default="nats://localhost:4222", help="NATS URL")
    parser.add_argument(
        "--postgres-dsn",
        default="postgresql://agent_economy:agent_economy_dev@localhost:5432/agent_economy",
        help="Postgres DSN",
    )

    args = parser.parse_args()

    try:
        result = asyncio.run(
            reject_work(
                job_id=args.job_id,
                reason=args.reason,
                reopen=args.reopen,
                nats_url=args.nats_url,
                postgres_dsn=args.postgres_dsn,
            )
        )

        print("Work rejected.")
        print(f"  Job ID: {result['job_id']}")
        if result['previous_agent']:
            print(f"  Agent: {result['previous_agent']}")
        print(f"  Reason: {result['reason']}")
        print(f"  Status: {result['status']}")
        if result['reopened']:
            print("  Job has been reopened for new bids.")

    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
