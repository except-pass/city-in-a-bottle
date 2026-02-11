"""
Message Board Client for City in a Bottle.

Provides async interface to NATS JetStream for posting and reading messages.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Any
from enum import Enum

import nats
from nats.js.api import StreamConfig, RetentionPolicy, ConsumerConfig, DeliverPolicy


class MessageType(str, Enum):
    """Types of messages on the board."""
    JOB = "job"
    BID = "bid"
    STATUS = "status"
    RESULT = "result"
    META = "meta"


@dataclass
class Message:
    """Message envelope for the board."""
    msg_id: str
    type: MessageType
    agent_id: str
    content: dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    thread_id: Optional[str] = None
    refs: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "msg_id": self.msg_id,
            "thread_id": self.thread_id,
            "type": self.type.value if isinstance(self.type, MessageType) else self.type,
            "agent_id": self.agent_id,
            "timestamp": self.timestamp,
            "content": self.content,
            "refs": self.refs,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """Create Message from dictionary."""
        return cls(
            msg_id=data["msg_id"],
            thread_id=data.get("thread_id"),
            type=MessageType(data["type"]),
            agent_id=data["agent_id"],
            timestamp=data["timestamp"],
            content=data["content"],
            refs=data.get("refs", []),
            tags=data.get("tags", []),
        )


# Subject names for different message types
SUBJECTS = {
    MessageType.JOB: "board.jobs",
    MessageType.BID: "board.bids",
    MessageType.STATUS: "board.work",
    MessageType.RESULT: "board.results",
    MessageType.META: "board.meta",
}

# Stream names (must match init_streams.py)
STREAM_NAMES = {
    MessageType.JOB: "BOARD_JOB",
    MessageType.BID: "BOARD_BID",
    MessageType.STATUS: "BOARD_WORK",
    MessageType.RESULT: "BOARD_RESULT",
    MessageType.META: "BOARD_META",
}


class BoardClient:
    """Async client for the City in a Bottle message board."""

    def __init__(self, nats_url: str = "nats://localhost:4222"):
        self.nats_url = nats_url
        self._nc: Optional[nats.NATS] = None
        self._js: Optional[nats.js.JetStreamContext] = None

    async def connect(self) -> None:
        """Connect to NATS server."""
        self._nc = await nats.connect(self.nats_url)
        self._js = self._nc.jetstream()

    async def close(self) -> None:
        """Close connection to NATS server."""
        if self._nc:
            await self._nc.close()
            self._nc = None
            self._js = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _get_subject(self, msg_type: MessageType) -> str:
        """Get the subject for a message type."""
        return SUBJECTS[msg_type]

    async def post_message(
        self,
        msg_type: MessageType,
        agent_id: str,
        content: dict[str, Any],
        thread_id: Optional[str] = None,
        refs: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
    ) -> Message:
        """
        Post a message to the board.

        Args:
            msg_type: Type of message (job, bid, status, result, meta)
            agent_id: ID of the agent posting
            content: Message content
            thread_id: Optional thread ID for replies
            refs: Optional list of referenced message IDs
            tags: Optional list of tags

        Returns:
            The posted Message object
        """
        if not self._js:
            raise RuntimeError("Not connected to NATS")

        msg = Message(
            msg_id=str(uuid.uuid4()),
            type=msg_type,
            agent_id=agent_id,
            content=content,
            thread_id=thread_id,
            refs=refs or [],
            tags=tags or [],
        )

        subject = self._get_subject(msg_type)
        await self._js.publish(
            subject,
            json.dumps(msg.to_dict()).encode(),
            headers={"Nats-Msg-Id": msg.msg_id},  # Deduplication
        )

        return msg

    async def read_messages(
        self,
        msg_type: MessageType,
        limit: int = 100,
        start_time: Optional[datetime] = None,
    ) -> list[Message]:
        """
        Read messages from the board.

        Args:
            msg_type: Type of messages to read
            limit: Maximum number of messages to return
            start_time: Optional start time to read from

        Returns:
            List of Message objects
        """
        if not self._js:
            raise RuntimeError("Not connected to NATS")

        subject = self._get_subject(msg_type)
        stream_name = STREAM_NAMES[msg_type]

        # Create ephemeral pull consumer
        consumer_config = ConsumerConfig(
            deliver_policy=DeliverPolicy.ALL if start_time is None else DeliverPolicy.BY_START_TIME,
            opt_start_time=start_time.isoformat() if start_time else None,
        )

        messages = []
        try:
            psub = await self._js.pull_subscribe(
                subject,
                durable=None,  # Ephemeral
                stream=stream_name,
                config=consumer_config,
            )

            try:
                fetched = await psub.fetch(limit, timeout=1)
                for msg in fetched:
                    try:
                        data = json.loads(msg.data.decode())
                        messages.append(Message.from_dict(data))
                    except (json.JSONDecodeError, KeyError) as e:
                        # Skip malformed messages
                        continue
            except asyncio.TimeoutError:
                # No more messages available
                pass

        except Exception as e:
            # Stream might not exist yet
            if "stream not found" in str(e).lower():
                return []
            raise

        return messages

    async def read_all_messages(
        self,
        limit_per_type: int = 50,
    ) -> dict[MessageType, list[Message]]:
        """
        Read messages from all board subjects.

        Args:
            limit_per_type: Maximum messages per type

        Returns:
            Dictionary mapping message type to list of messages
        """
        result = {}
        for msg_type in MessageType:
            result[msg_type] = await self.read_messages(msg_type, limit=limit_per_type)
        return result

    async def get_message_by_id(self, msg_id: str) -> Optional[Message]:
        """
        Find a specific message by ID across all streams.

        Args:
            msg_id: The message ID to find

        Returns:
            The Message if found, None otherwise
        """
        for msg_type in MessageType:
            messages = await self.read_messages(msg_type, limit=1000)
            for msg in messages:
                if msg.msg_id == msg_id:
                    return msg
        return None

    async def get_thread(self, thread_id: str) -> list[Message]:
        """
        Get all messages in a thread.

        Args:
            thread_id: The thread ID

        Returns:
            List of messages in the thread, sorted by timestamp
        """
        all_messages = []
        for msg_type in MessageType:
            messages = await self.read_messages(msg_type, limit=1000)
            for msg in messages:
                if msg.thread_id == thread_id or msg.msg_id == thread_id:
                    all_messages.append(msg)

        return sorted(all_messages, key=lambda m: m.timestamp)


# Convenience functions for common operations
async def post_job(
    client: BoardClient,
    agent_id: str,
    title: str,
    description: str,
    reward: int,
    tags: Optional[list[str]] = None,
    deadline: Optional[str] = None,
) -> Message:
    """Post a job to the board."""
    content = {
        "title": title,
        "description": description,
        "reward": reward,
        "deadline": deadline,
    }
    return await client.post_message(
        MessageType.JOB,
        agent_id,
        content,
        tags=tags,
    )


async def post_bid(
    client: BoardClient,
    agent_id: str,
    job_msg_id: str,
    proposed_reward: Optional[int] = None,
    message: str = "",
) -> Message:
    """Post a bid on a job."""
    content = {
        "job_msg_id": job_msg_id,
        "proposed_reward": proposed_reward,
        "message": message,
    }
    return await client.post_message(
        MessageType.BID,
        agent_id,
        content,
        thread_id=job_msg_id,
        refs=[job_msg_id],
    )


async def post_work_result(
    client: BoardClient,
    agent_id: str,
    job_msg_id: str,
    result: str,
    artifacts: Optional[list[dict]] = None,
) -> Message:
    """Post completed work result."""
    content = {
        "job_msg_id": job_msg_id,
        "result": result,
        "artifacts": artifacts or [],
    }
    return await client.post_message(
        MessageType.RESULT,
        agent_id,
        content,
        thread_id=job_msg_id,
        refs=[job_msg_id],
    )


# CLI support
if __name__ == "__main__":
    import argparse

    async def main():
        parser = argparse.ArgumentParser(description="Board client CLI")
        subparsers = parser.add_subparsers(dest="command", required=True)

        # Post command
        post_parser = subparsers.add_parser("post", help="Post a message")
        post_parser.add_argument("--type", "-t", required=True, choices=[t.value for t in MessageType])
        post_parser.add_argument("--agent", "-a", required=True, help="Agent ID")
        post_parser.add_argument("--content", "-c", required=True, help="JSON content")
        post_parser.add_argument("--tags", nargs="*", help="Tags")

        # Read command
        read_parser = subparsers.add_parser("read", help="Read messages")
        read_parser.add_argument("--type", "-t", choices=[t.value for t in MessageType])
        read_parser.add_argument("--limit", "-l", type=int, default=10)

        args = parser.parse_args()

        async with BoardClient() as client:
            if args.command == "post":
                msg = await client.post_message(
                    MessageType(args.type),
                    args.agent,
                    json.loads(args.content),
                    tags=args.tags,
                )
                print(f"Posted message: {msg.msg_id}")
                print(json.dumps(msg.to_dict(), indent=2))

            elif args.command == "read":
                if args.type:
                    messages = await client.read_messages(MessageType(args.type), limit=args.limit)
                    print(f"\n{args.type.upper()} messages:")
                    for msg in messages:
                        print(json.dumps(msg.to_dict(), indent=2))
                else:
                    all_messages = await client.read_all_messages(limit_per_type=args.limit)
                    for msg_type, messages in all_messages.items():
                        if messages:
                            print(f"\n{msg_type.value.upper()} messages ({len(messages)}):")
                            for msg in messages:
                                print(json.dumps(msg.to_dict(), indent=2))

    asyncio.run(main())
