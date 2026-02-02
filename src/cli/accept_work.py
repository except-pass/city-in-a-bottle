#!/usr/bin/env python3
"""
Accept completed work and credit tokens to the agent.

Usage:
    python accept_work.py --job-id <uuid> --agent <agent_id>
"""

import argparse
import asyncio
import sys
from datetime import datetime
from uuid import UUID

import asyncpg

# Add parent to path for imports
sys.path.insert(0, str(__file__).rsplit("/", 3)[0])

from src.ledger.client import LedgerClient


async def accept_work(
    job_id: str,
    agent_id: str,
    note: str = None,
    postgres_dsn: str = "postgresql://agent_economy:agent_economy_dev@localhost:5432/agent_economy",
) -> dict:
    """
    Accept work and credit tokens to the agent.

    Args:
        job_id: Job ID (UUID string)
        agent_id: Agent who completed the work
        note: Optional note about the acceptance
        postgres_dsn: Postgres connection string

    Returns:
        Result including new balance
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

        if job['status'] not in ('submitted', 'in_progress', 'open'):
            raise ValueError(f"Job cannot be accepted (status: {job['status']})")

        reward = job['reward']

        # Update job status
        await conn.execute(
            """
            UPDATE jobs
            SET status = 'accepted',
                assigned_agent = $1,
                completed_at = $2,
                updated_at = $2
            WHERE job_id = $3
            """,
            agent_id,
            datetime.utcnow(),
            job_uuid,
        )
    finally:
        await conn.close()

    # Credit tokens to agent
    ledger = LedgerClient()
    await ledger.connect()
    try:
        tx = await ledger.credit(
            agent_id,
            reward,
            reason="job_reward",
            counterparty_id="customer",
            job_id=job_uuid,
            note=note or f"Reward for job: {job['title']}",
        )

        return {
            "job_id": job_id,
            "agent_id": agent_id,
            "reward": reward,
            "new_balance": tx.balance_after,
            "status": "accepted",
        }
    finally:
        await ledger.close()


def main():
    parser = argparse.ArgumentParser(description="Accept completed work")
    parser.add_argument("--job-id", "-j", required=True, help="Job ID (UUID)")
    parser.add_argument("--agent", "-a", required=True, help="Agent who completed the work")
    parser.add_argument("--note", "-n", help="Optional note")
    parser.add_argument(
        "--postgres-dsn",
        default="postgresql://agent_economy:agent_economy_dev@localhost:5432/agent_economy",
        help="Postgres DSN",
    )

    args = parser.parse_args()

    try:
        result = asyncio.run(
            accept_work(
                job_id=args.job_id,
                agent_id=args.agent,
                note=args.note,
                postgres_dsn=args.postgres_dsn,
            )
        )

        print("Work accepted!")
        print(f"  Job ID: {result['job_id']}")
        print(f"  Agent: {result['agent_id']}")
        print(f"  Reward: {result['reward']} tokens")
        print(f"  New Balance: {result['new_balance']} tokens")

    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
