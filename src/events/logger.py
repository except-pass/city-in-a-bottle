"""
Event Logger for City in a Bottle

Logs all significant events with timestamps for replay visualizations.

Usage:
    from src.events.logger import EventLogger

    logger = EventLogger()
    logger.log_tool_call(agent_id, run_id, epoch, tool_name, input_data)
    logger.log_tool_result(agent_id, run_id, epoch, tool_name, output_data)
    logger.log_message(agent_id, run_id, epoch, channel, topic, content)
    logger.log_transfer(agent_id, run_id, epoch, to_agent, amount, reason)
"""

import json
import os
from datetime import datetime
from typing import Any

import psycopg2

# Config
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5434")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "agent_economy")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "agent_economy")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "agent_economy_dev")


class EventLogger:
    """Logs events to the database for replay visualizations."""

    def __init__(self, conn=None):
        self._conn = conn
        self._own_conn = False

    def _get_conn(self):
        if self._conn is None:
            self._conn = psycopg2.connect(
                host=POSTGRES_HOST,
                port=POSTGRES_PORT,
                dbname=POSTGRES_DB,
                user=POSTGRES_USER,
                password=POSTGRES_PASSWORD,
            )
            self._own_conn = True
        return self._conn

    def close(self):
        if self._own_conn and self._conn:
            self._conn.close()
            self._conn = None

    def _log_event(
        self,
        agent_id: str,
        event_type: str,
        event_subtype: str | None = None,
        run_id: str | None = None,
        epoch_number: int | None = None,
        tool_name: str | None = None,
        input_data: Any = None,
        output_data: Any = None,
        target_agent: str | None = None,
        channel: str | None = None,
        amount: int | None = None,
        metadata: dict | None = None,
    ):
        """Log a single event."""
        conn = self._get_conn()
        cur = conn.cursor()

        try:
            cur.execute("""
                INSERT INTO events (
                    agent_id, event_type, event_subtype, run_id, epoch_number,
                    tool_name, input, output, target_agent, channel, amount, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
            """, (
                agent_id,
                event_type,
                event_subtype,
                run_id,
                epoch_number,
                tool_name,
                json.dumps(input_data) if input_data else None,
                json.dumps(output_data) if output_data else None,
                target_agent,
                channel,
                amount,
                json.dumps(metadata) if metadata else None,
            ))
            conn.commit()
        except psycopg2.Error as e:
            # Don't crash the agent if logging fails
            print(f"Warning: Event logging failed: {e}")
            conn.rollback()

    def log_tool_call(
        self,
        agent_id: str,
        tool_name: str,
        input_data: Any,
        run_id: str | None = None,
        epoch_number: int | None = None,
    ):
        """Log a tool being called."""
        # Classify the tool for easier querying
        subtype = self._classify_tool(tool_name)

        self._log_event(
            agent_id=agent_id,
            event_type="tool_call",
            event_subtype=subtype,
            run_id=run_id,
            epoch_number=epoch_number,
            tool_name=tool_name,
            input_data=input_data,
        )

    def log_tool_result(
        self,
        agent_id: str,
        tool_name: str,
        input_data: Any,
        output_data: Any,
        run_id: str | None = None,
        epoch_number: int | None = None,
    ):
        """Log a tool result."""
        subtype = self._classify_tool(tool_name)

        # Extract denormalized fields for common queries
        channel = None
        target_agent = None
        amount = None

        if isinstance(input_data, dict):
            channel = input_data.get("channel")
            target_agent = input_data.get("to") or input_data.get("recipients")
            if isinstance(target_agent, list):
                target_agent = target_agent[0] if target_agent else None
            amount = input_data.get("amount")

        self._log_event(
            agent_id=agent_id,
            event_type="tool_result",
            event_subtype=subtype,
            run_id=run_id,
            epoch_number=epoch_number,
            tool_name=tool_name,
            input_data=input_data,
            output_data=output_data,
            target_agent=target_agent,
            channel=channel,
            amount=amount,
        )

    def log_message(
        self,
        agent_id: str,
        channel: str,
        topic: str | None,
        content: str,
        message_type: str = "channel",  # 'channel' or 'dm'
        recipients: list[str] | None = None,
        run_id: str | None = None,
        epoch_number: int | None = None,
    ):
        """Log a message sent."""
        self._log_event(
            agent_id=agent_id,
            event_type="message",
            event_subtype=message_type,
            run_id=run_id,
            epoch_number=epoch_number,
            channel=channel,
            target_agent=recipients[0] if recipients else None,
            input_data={
                "channel": channel,
                "topic": topic,
                "content": content[:500],  # Truncate for storage
                "recipients": recipients,
            },
        )

    def log_transfer(
        self,
        agent_id: str,
        to_agent: str,
        amount: int,
        reason: str,
        run_id: str | None = None,
        epoch_number: int | None = None,
    ):
        """Log a token transfer."""
        self._log_event(
            agent_id=agent_id,
            event_type="transfer",
            event_subtype=reason,
            run_id=run_id,
            epoch_number=epoch_number,
            target_agent=to_agent,
            amount=amount,
            input_data={"to": to_agent, "amount": amount, "reason": reason},
        )

    def log_file_write(
        self,
        agent_id: str,
        file_path: str,
        operation: str = "write",  # 'write', 'edit', 'delete'
        run_id: str | None = None,
        epoch_number: int | None = None,
    ):
        """Log a file operation."""
        self._log_event(
            agent_id=agent_id,
            event_type="file",
            event_subtype=operation,
            run_id=run_id,
            epoch_number=epoch_number,
            input_data={"path": file_path, "operation": operation},
        )

    def log_job_event(
        self,
        agent_id: str,
        job_id: str,
        action: str,  # 'post', 'bid', 'accept', 'complete', 'reject'
        details: dict | None = None,
        run_id: str | None = None,
        epoch_number: int | None = None,
    ):
        """Log a job-related event."""
        self._log_event(
            agent_id=agent_id,
            event_type="job",
            event_subtype=action,
            run_id=run_id,
            epoch_number=epoch_number,
            input_data={"job_id": job_id, **(details or {})},
        )

    def log_agent_start(
        self,
        agent_id: str,
        run_id: str,
        epoch_number: int | None = None,
        balance: int | None = None,
    ):
        """Log an agent starting a run."""
        self._log_event(
            agent_id=agent_id,
            event_type="lifecycle",
            event_subtype="start",
            run_id=run_id,
            epoch_number=epoch_number,
            input_data={"balance": balance},
        )

    def log_agent_end(
        self,
        agent_id: str,
        run_id: str,
        epoch_number: int | None = None,
        balance: int | None = None,
        status: str = "completed",
        tokens_spent: int | None = None,
    ):
        """Log an agent ending a run."""
        self._log_event(
            agent_id=agent_id,
            event_type="lifecycle",
            event_subtype="end",
            run_id=run_id,
            epoch_number=epoch_number,
            input_data={
                "balance": balance,
                "status": status,
                "tokens_spent": tokens_spent,
            },
        )

    def _classify_tool(self, tool_name: str) -> str:
        """Classify tool into category for querying."""
        if not tool_name:
            return "unknown"

        tool_lower = tool_name.lower()

        if "ledger" in tool_lower:
            return "ledger"
        elif "send" in tool_lower and ("message" in tool_lower or "dm" in tool_lower):
            return "message_send"
        elif "read" in tool_lower or "list" in tool_lower or "get" in tool_lower:
            if "message" in tool_lower or "channel" in tool_lower or "dm" in tool_lower:
                return "message_read"
            return "read"
        elif "forgejo" in tool_lower or "git" in tool_lower:
            return "git"
        elif tool_name in ("Write", "Edit"):
            return "file_write"
        elif tool_name == "Read":
            return "file_read"
        else:
            return "other"


# Singleton for easy import
_default_logger: EventLogger | None = None


def get_logger() -> EventLogger:
    """Get the default event logger."""
    global _default_logger
    if _default_logger is None:
        _default_logger = EventLogger()
    return _default_logger
