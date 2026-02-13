"""Database query functions for dashboard statistics."""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg

from ..models import (
    TokenStats,
    AgentStats,
    JobStats,
    LeaderboardEntry,
    JobItem,
    HealthStatus,
    QuickStats,
)


class StatsService:
    """Service for querying dashboard statistics from PostgreSQL."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = int(os.environ.get("POSTGRES_PORT", "5434")),
        database: str = "agent_economy",
        user: str = "agent_economy",
        password: str = "agent_economy_dev",
    ):
        self.dsn = f"postgresql://{user}:{password}@{host}:{port}/{database}"
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Connect to the database."""
        self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)

    async def close(self) -> None:
        """Close database connection."""
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def get_token_stats(self) -> TokenStats:
        """
        Get token economy statistics.

        Returns:
            TokenStats with circulation, escrowed, and 24h velocity
        """
        if not self._pool:
            raise RuntimeError("Not connected to database")

        async with self._pool.acquire() as conn:
            # Get circulation: sum of all agent balances (latest balance per agent)
            circulation = await conn.fetchval(
                """
                SELECT COALESCE(SUM(balance_after), 0)
                FROM (
                    SELECT DISTINCT ON (agent_id) balance_after
                    FROM token_transactions
                    ORDER BY agent_id, timestamp DESC
                ) latest_balances
                """
            )

            # Get escrowed: sum of rewards for jobs in progress
            escrowed = await conn.fetchval(
                """
                SELECT COALESCE(SUM(reward), 0)
                FROM jobs
                WHERE status = 'in_progress'
                """
            )

            # Get 24h velocity: total token movement in last 24 hours
            velocity_24h = await conn.fetchval(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM token_transactions
                WHERE timestamp > NOW() - INTERVAL '24 hours'
                """
            )

            return TokenStats(
                circulation=circulation or 0,
                escrowed=escrowed or 0,
                velocity_24h=velocity_24h or 0,
            )

    async def get_agent_stats(self) -> AgentStats:
        """
        Get agent statistics by status.

        Agents are classified as:
        - active: have transactions in last 24 hours
        - idle: have transactions but none in last 24 hours
        - in_debt: current balance is negative

        Returns:
            AgentStats with counts by status
        """
        if not self._pool:
            raise RuntimeError("Not connected to database")

        async with self._pool.acquire() as conn:
            # Get all agents with their current balance and last activity
            rows = await conn.fetch(
                """
                SELECT
                    agent_id,
                    (SELECT balance_after FROM token_transactions t2
                     WHERE t2.agent_id = t1.agent_id
                     ORDER BY timestamp DESC LIMIT 1) as balance,
                    MAX(timestamp) as last_activity
                FROM token_transactions t1
                WHERE agent_id NOT IN ('system', 'customer')
                GROUP BY agent_id
                """
            )

            total = len(rows)
            active = 0
            idle = 0
            in_debt = 0
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=24)

            for row in rows:
                balance = row["balance"] or 0
                last_activity = row["last_activity"]

                if balance < 0:
                    in_debt += 1
                elif last_activity and last_activity.replace(tzinfo=timezone.utc) > cutoff:
                    active += 1
                else:
                    idle += 1

            return AgentStats(
                total=total,
                active=active,
                idle=idle,
                in_debt=in_debt,
            )

    async def get_job_stats(self) -> JobStats:
        """
        Get job statistics by status.

        Returns:
            JobStats with counts by status
        """
        if not self._pool:
            raise RuntimeError("Not connected to database")

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT status, COUNT(*) as count
                FROM jobs
                GROUP BY status
                """
            )

            stats = {"open": 0, "in_progress": 0, "submitted": 0, "accepted": 0, "rejected": 0}
            for row in rows:
                status = row["status"]
                if status in stats:
                    stats[status] = row["count"]

            total = sum(stats.values())
            # Completed includes both accepted and rejected
            completed = stats["accepted"] + stats["rejected"]

            return JobStats(
                total=total,
                open=stats["open"],
                in_progress=stats["in_progress"],
                submitted=stats["submitted"],
                completed=completed,
            )

    async def get_leaderboard(self, limit: int = 10) -> list[LeaderboardEntry]:
        """
        Get top agents by balance.

        Args:
            limit: Maximum number of agents to return

        Returns:
            List of LeaderboardEntry sorted by balance descending
        """
        if not self._pool:
            raise RuntimeError("Not connected to database")

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH agent_current AS (
                    SELECT DISTINCT ON (agent_id)
                        agent_id,
                        balance_after as balance
                    FROM token_transactions
                    WHERE agent_id NOT IN ('system', 'customer')
                    ORDER BY agent_id, timestamp DESC
                ),
                agent_tx_count AS (
                    SELECT agent_id, COUNT(*) as total_transactions
                    FROM token_transactions
                    WHERE agent_id NOT IN ('system', 'customer')
                    GROUP BY agent_id
                ),
                agent_jobs AS (
                    SELECT assigned_agent as agent_id, COUNT(*) as jobs_completed
                    FROM jobs
                    WHERE status = 'accepted'
                    GROUP BY assigned_agent
                ),
                agent_delta AS (
                    SELECT agent_id,
                           COALESCE(SUM(CASE WHEN tx_type IN ('credit', 'transfer_in') THEN amount
                                             WHEN tx_type IN ('debit', 'transfer_out') THEN -amount
                                             ELSE 0 END), 0) as delta_24h
                    FROM token_transactions
                    WHERE timestamp > NOW() - INTERVAL '24 hours'
                      AND agent_id NOT IN ('system', 'customer')
                    GROUP BY agent_id
                )
                SELECT
                    ac.agent_id,
                    ac.balance,
                    COALESCE(atc.total_transactions, 0) as total_transactions,
                    COALESCE(aj.jobs_completed, 0) as jobs_completed,
                    COALESCE(ad.delta_24h, 0) as delta_24h
                FROM agent_current ac
                LEFT JOIN agent_tx_count atc ON ac.agent_id = atc.agent_id
                LEFT JOIN agent_jobs aj ON ac.agent_id = aj.agent_id
                LEFT JOIN agent_delta ad ON ac.agent_id = ad.agent_id
                ORDER BY ac.balance DESC
                LIMIT $1
                """,
                limit,
            )

            return [
                LeaderboardEntry(
                    agent_id=row["agent_id"],
                    balance=row["balance"] or 0,
                    total_transactions=row["total_transactions"],
                    jobs_completed=row["jobs_completed"],
                    delta_24h=row["delta_24h"],
                )
                for row in rows
            ]

    async def get_jobs_list(
        self,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list[JobItem]:
        """
        Get list of jobs with optional status filter.

        Args:
            status: Filter by job status (open, in_progress, submitted, accepted, rejected)
            limit: Maximum number of jobs to return

        Returns:
            List of JobItem sorted by created_at descending
        """
        if not self._pool:
            raise RuntimeError("Not connected to database")

        async with self._pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    """
                    SELECT job_id, title, description, reward, status,
                           assigned_agent, created_at, tags
                    FROM jobs
                    WHERE status = $1
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    status,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT job_id, title, description, reward, status,
                           assigned_agent, created_at, tags
                    FROM jobs
                    ORDER BY created_at DESC
                    LIMIT $1
                    """,
                    limit,
                )

            return [
                JobItem(
                    job_id=str(row["job_id"]),
                    title=row["title"],
                    description=row["description"],
                    reward=row["reward"],
                    status=row["status"],
                    assigned_agent=row["assigned_agent"],
                    created_at=row["created_at"],
                    tags=row["tags"] or [],
                )
                for row in rows
            ]

    async def get_quick_stats(self) -> QuickStats:
        """
        Get quick stats for dashboard footer.

        Returns:
            QuickStats with agent runs, messages, success rate, and response time
        """
        if not self._pool:
            raise RuntimeError("Not connected to database")

        async with self._pool.acquire() as conn:
            # Agent runs today
            agent_runs_today = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM agent_runs
                WHERE started_at > NOW() - INTERVAL '24 hours'
                """
            ) or 0

            # Total transactions as proxy for messages
            messages_posted = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM token_transactions
                WHERE timestamp > NOW() - INTERVAL '24 hours'
                """
            ) or 0

            # Job success rate
            total_completed = await conn.fetchval(
                """
                SELECT COUNT(*) FROM jobs WHERE status IN ('accepted', 'rejected')
                """
            ) or 0
            accepted = await conn.fetchval(
                """
                SELECT COUNT(*) FROM jobs WHERE status = 'accepted'
                """
            ) or 0
            success_rate = (accepted / total_completed * 100) if total_completed > 0 else 0.0

            # Average response time (from agent runs)
            avg_response = await conn.fetchval(
                """
                SELECT AVG(EXTRACT(EPOCH FROM (ended_at - started_at)))
                FROM agent_runs
                WHERE ended_at IS NOT NULL
                  AND started_at > NOW() - INTERVAL '24 hours'
                """
            ) or 0.0

            return QuickStats(
                agent_runs_today=agent_runs_today,
                messages_posted=messages_posted,
                job_success_rate=round(success_rate, 1),
                avg_response_time=round(avg_response, 1),
            )

    async def check_health(self) -> HealthStatus:
        """
        Check database connectivity.

        Returns:
            HealthStatus with postgresql status
        """
        pg_ok = False
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                    pg_ok = True
            except Exception:
                pg_ok = False

        return HealthStatus(
            postgresql=pg_ok,
            nats=False,  # Will be updated by events service
            overall=pg_ok,
        )
