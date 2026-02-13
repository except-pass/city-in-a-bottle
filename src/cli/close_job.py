#!/usr/bin/env python3
"""
Close/cancel a job without reward.

Usage:
    python close_job.py --job-id <uuid> --reason "No longer needed"

For Zulip:
    python close_job.py --message-bus zulip --zulip-config admin.zuliprc --job-id <uuid>
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

# Optional Zulip support
try:
    import zulip
    ZULIP_AVAILABLE = True
except ImportError:
    ZULIP_AVAILABLE = False


async def close_job(
    job_id: str,
    reason: str = "Cancelled by customer",
    message_bus: str = "nats",
    nats_url: str = "nats://localhost:4222",
    zulip_config: str = None,
    zulip_url: str = "http://localhost:8081",
    postgres_dsn: str = "postgresql://agent_economy:agent_economy_dev@localhost:5434/agent_economy",
) -> dict:
    """
    Close/cancel a job without paying reward.

    Args:
        job_id: Job ID (UUID string)
        reason: Reason for closing
        nats_url: NATS server URL
        postgres_dsn: Postgres connection string

    Returns:
        Result with job details
    """
    job_uuid = UUID(job_id)

    conn = await asyncpg.connect(postgres_dsn)
    try:
        job = await conn.fetchrow(
            "SELECT * FROM jobs WHERE job_id = $1",
            job_uuid,
        )

        if not job:
            raise ValueError(f"Job not found: {job_id}")

        old_status = job['status']

        if old_status in ('accepted', 'cancelled'):
            raise ValueError(f"Job already {old_status}, cannot close")

        # Update job status to cancelled
        await conn.execute(
            """
            UPDATE jobs
            SET status = 'cancelled',
                updated_at = $1
            WHERE job_id = $2
            """,
            datetime.utcnow(),
            job_uuid,
        )
    finally:
        await conn.close()

    # Post cancellation notice
    if message_bus == "zulip":
        if not ZULIP_AVAILABLE:
            raise RuntimeError("Zulip package not installed")
        if zulip_config:
            client = zulip.Client(config_file=zulip_config)
        else:
            client = zulip.Client(email="admin@agent-economy.local", api_key="", site=zulip_url)

        topic = f"JOB-{job['job_id']}: {job['title']}"
        content = f"""**Job Cancelled**

This job has been cancelled.

**Reason:** {reason}
"""
        client.send_message({
            "type": "stream",
            "to": "job-board",
            "topic": topic,
            "content": content,
        })
    else:
        async with BoardClient(nats_url) as board:
            await board.post_message(
                MessageType.META,
                agent_id="customer",
                content={
                    "type": "job_cancelled",
                    "job_id": job_id,
                    "title": job['title'],
                    "reason": reason,
                    "previous_status": old_status,
                },
                thread_id=str(job['job_msg_id']) if job.get('job_msg_id') else None,
                tags=["cancelled"],
            )

    return {
        "job_id": job_id,
        "title": job['title'],
        "previous_status": old_status,
        "new_status": "cancelled",
        "reason": reason,
    }


def main():
    parser = argparse.ArgumentParser(description="Close/cancel a job")
    parser.add_argument("--job-id", "-j", required=True, help="Job ID (UUID)")
    parser.add_argument("--reason", "-r", default="Cancelled by customer", help="Reason for closing")
    parser.add_argument("--message-bus", default="nats", choices=["nats", "zulip"],
                       help="Message bus to use")
    parser.add_argument("--nats-url", default="nats://localhost:4222", help="NATS URL")
    parser.add_argument("--zulip-config", help="Path to admin .zuliprc file")
    parser.add_argument("--zulip-url", default="http://localhost:8081", help="Zulip URL")
    parser.add_argument(
        "--postgres-dsn",
        default="postgresql://agent_economy:agent_economy_dev@localhost:5434/agent_economy",
        help="Postgres DSN",
    )

    args = parser.parse_args()

    try:
        result = asyncio.run(
            close_job(
                job_id=args.job_id,
                reason=args.reason,
                message_bus=args.message_bus,
                nats_url=args.nats_url,
                zulip_config=args.zulip_config,
                zulip_url=args.zulip_url,
                postgres_dsn=args.postgres_dsn,
            )
        )

        print("Job closed.")
        print(f"  Job ID: {result['job_id']}")
        print(f"  Title: {result['title']}")
        print(f"  Status: {result['previous_status']} → {result['new_status']}")
        print(f"  Reason: {result['reason']}")

    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
