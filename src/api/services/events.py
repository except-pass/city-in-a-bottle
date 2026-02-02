"""SSE event generator for real-time system events."""

import asyncio
import json
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

import nats
from nats.js.api import ConsumerConfig, DeliverPolicy

from ..models import SystemEvent


# Event category colors (matching dashboard CSS classes)
CATEGORY_COLORS = {
    "LEDGER": "text-green-500",
    "JOBS": "text-blue-400",
    "RUNNER": "text-yellow-500",
    "BOARD": "text-slate-400",
    "SCHED": "text-green-500",
    "MCP": "text-purple-400",
    "WARN": "text-red-400",
    "ERROR": "text-red-500",
}


class EventsService:
    """Service for streaming system events via SSE."""

    def __init__(self, nats_url: str = "nats://localhost:4222"):
        self.nats_url = nats_url
        self._nc: Optional[nats.NATS] = None
        self._js: Optional[nats.js.JetStreamContext] = None
        self._connected = False

    async def connect(self) -> None:
        """Connect to NATS server."""
        try:
            self._nc = await nats.connect(self.nats_url)
            self._js = self._nc.jetstream()
            self._connected = True
        except Exception:
            self._connected = False

    async def close(self) -> None:
        """Close connection to NATS server."""
        if self._nc:
            await self._nc.close()
            self._nc = None
            self._js = None
            self._connected = False

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    @property
    def is_connected(self) -> bool:
        """Check if connected to NATS."""
        return self._connected and self._nc is not None and self._nc.is_connected

    def _parse_nats_message(self, subject: str, data: bytes) -> Optional[SystemEvent]:
        """Parse a NATS message into a SystemEvent."""
        try:
            payload = json.loads(data.decode())
            timestamp = datetime.now(timezone.utc)

            # Determine category based on subject
            if "jobs" in subject:
                category = "JOBS"
                msg_content = payload.get("content", {})
                title = msg_content.get("title", "Unknown job")
                reward = msg_content.get("reward", 0)
                message = f"New job posted: \"{title}\" ({reward:,} TKN)"
            elif "bids" in subject:
                category = "BOARD"
                agent_id = payload.get("agent_id", "unknown")
                message = f"New bid from {agent_id}"
            elif "work" in subject:
                category = "RUNNER"
                agent_id = payload.get("agent_id", "unknown")
                message = f"Work update from {agent_id}"
            elif "results" in subject:
                category = "JOBS"
                agent_id = payload.get("agent_id", "unknown")
                message = f"Result submitted by {agent_id}"
            elif "meta" in subject:
                category = "SCHED"
                message = payload.get("content", {}).get("message", "System message")
            else:
                category = "BOARD"
                message = f"Message on {subject}"

            return SystemEvent(
                timestamp=timestamp,
                category=category,
                message=message,
                color=CATEGORY_COLORS.get(category, "text-slate-400"),
            )
        except Exception:
            return None

    async def stream_events(self) -> AsyncGenerator[str, None]:
        """
        Generate SSE events from NATS streams.

        Yields:
            SSE formatted event strings
        """
        if not self._js:
            # Yield a connection error event
            event = SystemEvent(
                timestamp=datetime.now(timezone.utc),
                category="ERROR",
                message="NATS not connected",
                color=CATEGORY_COLORS["ERROR"],
            )
            yield f"data: {event.model_dump_json()}\n\n"
            return

        # Subscribe to all board subjects
        subjects = ["board.jobs", "board.bids", "board.work", "board.results", "board.meta"]

        try:
            # Create ephemeral consumer for real-time events
            sub = await self._nc.subscribe("board.>")

            # Send initial connection event
            event = SystemEvent(
                timestamp=datetime.now(timezone.utc),
                category="SCHED",
                message="Connected to event stream",
                color=CATEGORY_COLORS["SCHED"],
            )
            yield f"data: {event.model_dump_json()}\n\n"

            while True:
                try:
                    msg = await asyncio.wait_for(sub.next_msg(), timeout=30.0)
                    event = self._parse_nats_message(msg.subject, msg.data)
                    if event:
                        yield f"data: {event.model_dump_json()}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield ": keepalive\n\n"
                except Exception as e:
                    event = SystemEvent(
                        timestamp=datetime.now(timezone.utc),
                        category="ERROR",
                        message=f"Stream error: {str(e)[:50]}",
                        color=CATEGORY_COLORS["ERROR"],
                    )
                    yield f"data: {event.model_dump_json()}\n\n"
                    break

        except Exception as e:
            event = SystemEvent(
                timestamp=datetime.now(timezone.utc),
                category="ERROR",
                message=f"Failed to subscribe: {str(e)[:50]}",
                color=CATEGORY_COLORS["ERROR"],
            )
            yield f"data: {event.model_dump_json()}\n\n"

    async def get_recent_events(
        self,
        pool,  # asyncpg pool
        limit: int = 20,
    ) -> list[SystemEvent]:
        """
        Get recent events from database transactions.

        Args:
            pool: asyncpg connection pool
            limit: Maximum number of events to return

        Returns:
            List of SystemEvent from recent transactions
        """
        events = []

        try:
            async with pool.acquire() as conn:
                # Get recent transactions
                rows = await conn.fetch(
                    """
                    SELECT timestamp, agent_id, tx_type, amount, reason, counterparty_id
                    FROM token_transactions
                    ORDER BY timestamp DESC
                    LIMIT $1
                    """,
                    limit,
                )

                for row in rows:
                    agent_id = row["agent_id"]
                    tx_type = row["tx_type"]
                    amount = row["amount"]
                    reason = row["reason"]

                    if tx_type == "credit":
                        category = "LEDGER"
                        message = f"{agent_id} credited {amount:,} TKN ({reason})"
                    elif tx_type == "debit":
                        category = "LEDGER"
                        message = f"{agent_id} debited {amount:,} TKN ({reason})"
                    elif tx_type == "transfer_out":
                        category = "LEDGER"
                        to_agent = row["counterparty_id"] or "unknown"
                        message = f"{agent_id} transferred {amount:,} TKN to {to_agent}"
                    elif tx_type == "transfer_in":
                        category = "LEDGER"
                        from_agent = row["counterparty_id"] or "unknown"
                        message = f"{agent_id} received {amount:,} TKN from {from_agent}"
                    else:
                        category = "LEDGER"
                        message = f"{agent_id}: {tx_type} {amount:,} TKN"

                    # Check for low balance warning
                    if tx_type in ("debit", "transfer_out"):
                        balance = await conn.fetchval(
                            """
                            SELECT balance_after FROM token_transactions
                            WHERE agent_id = $1
                            ORDER BY timestamp DESC LIMIT 1
                            """,
                            agent_id,
                        )
                        if balance is not None and balance < 1000:
                            events.append(
                                SystemEvent(
                                    timestamp=row["timestamp"],
                                    category="WARN",
                                    message=f"{agent_id} balance below 1000 TKN",
                                    color=CATEGORY_COLORS["WARN"],
                                )
                            )

                    events.append(
                        SystemEvent(
                            timestamp=row["timestamp"],
                            category=category,
                            message=message,
                            color=CATEGORY_COLORS.get(category, "text-slate-400"),
                        )
                    )

        except Exception:
            pass

        return events[:limit]
