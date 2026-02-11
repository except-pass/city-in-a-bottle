#!/usr/bin/env python3
"""
List jobs in City in a Bottle.

Usage:
    python list_jobs.py
    python list_jobs.py --status open
    python list_jobs.py --all
"""

import argparse
import asyncio
import sys
from datetime import datetime

import asyncpg

# Add parent to path for imports
sys.path.insert(0, str(__file__).rsplit("/", 3)[0])


async def list_jobs(
    status: str = None,
    show_all: bool = False,
    limit: int = 50,
    postgres_dsn: str = "postgresql://agent_economy:agent_economy_dev@localhost:5432/agent_economy",
) -> list[dict]:
    """
    List jobs from the database.

    Args:
        status: Filter by status (open, in_progress, submitted, accepted, rejected)
        show_all: Show all jobs including completed
        limit: Maximum number of jobs to return
        postgres_dsn: Postgres connection string

    Returns:
        List of job records
    """
    conn = await asyncpg.connect(postgres_dsn)
    try:
        if status:
            rows = await conn.fetch(
                """
                SELECT * FROM jobs
                WHERE status = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                status,
                limit,
            )
        elif show_all:
            rows = await conn.fetch(
                """
                SELECT * FROM jobs
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
        else:
            # Default: show open and in_progress jobs
            rows = await conn.fetch(
                """
                SELECT * FROM jobs
                WHERE status IN ('open', 'in_progress', 'submitted')
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )

        return [dict(row) for row in rows]
    finally:
        await conn.close()


def format_job(job: dict) -> str:
    """Format a job for display."""
    lines = [
        f"Job: {job['job_id']}",
        f"  Title: {job['title']}",
        f"  Reward: {job['reward']} tokens",
        f"  Status: {job['status']}",
    ]

    if job.get('assigned_agent'):
        lines.append(f"  Assigned: {job['assigned_agent']}")

    if job.get('tags'):
        lines.append(f"  Tags: {', '.join(job['tags'])}")

    if job.get('deadline'):
        lines.append(f"  Deadline: {job['deadline']}")

    lines.append(f"  Created: {job['created_at'].isoformat()}")

    if job.get('description'):
        desc = job['description'][:100]
        if len(job['description']) > 100:
            desc += "..."
        lines.append(f"  Description: {desc}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="List jobs in City in a Bottle")
    parser.add_argument(
        "--status", "-s",
        choices=["open", "in_progress", "submitted", "accepted", "rejected", "cancelled"],
        help="Filter by status",
    )
    parser.add_argument("--all", "-a", action="store_true", help="Show all jobs")
    parser.add_argument("--limit", "-l", type=int, default=50, help="Maximum jobs to show")
    parser.add_argument(
        "--postgres-dsn",
        default="postgresql://agent_economy:agent_economy_dev@localhost:5432/agent_economy",
        help="Postgres DSN",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    jobs = asyncio.run(
        list_jobs(
            status=args.status,
            show_all=args.all,
            limit=args.limit,
            postgres_dsn=args.postgres_dsn,
        )
    )

    if args.json:
        import json
        # Convert datetime objects for JSON
        for job in jobs:
            for key in ['created_at', 'updated_at', 'submitted_at', 'completed_at', 'deadline']:
                if job.get(key) and isinstance(job[key], datetime):
                    job[key] = job[key].isoformat()
            # Convert UUID to string
            for key in ['job_id', 'job_msg_id', 'work_msg_id']:
                if job.get(key):
                    job[key] = str(job[key])
        print(json.dumps(jobs, indent=2))
    else:
        if not jobs:
            print("No jobs found.")
        else:
            print(f"Found {len(jobs)} job(s):\n")
            for job in jobs:
                print(format_job(job))
                print()


if __name__ == "__main__":
    main()
