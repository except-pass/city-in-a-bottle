"""
Setup script for NATS JetStream streams.

Creates the required streams for City in a Bottle message board.
"""

import asyncio
import nats
from nats.js.api import StreamConfig, RetentionPolicy, StorageType


# Stream configurations for the message board
STREAMS = [
    {
        "name": "BOARD_JOB",
        "subjects": ["board.jobs"],
        "description": "Job postings from customers and agents",
    },
    {
        "name": "BOARD_BID",
        "subjects": ["board.bids"],
        "description": "Bids and proposals from agents",
    },
    {
        "name": "BOARD_WORK",
        "subjects": ["board.work"],
        "description": "Status updates on work in progress",
    },
    {
        "name": "BOARD_RESULT",
        "subjects": ["board.results"],
        "description": "Completed work deliverables",
    },
    {
        "name": "BOARD_META",
        "subjects": ["board.meta"],
        "description": "System announcements and meta information",
    },
]


async def setup_streams(nats_url: str = "nats://localhost:4222") -> None:
    """
    Create or update all required JetStream streams.

    Args:
        nats_url: NATS server URL
    """
    nc = await nats.connect(nats_url)
    js = nc.jetstream()

    print(f"Connected to NATS at {nats_url}")
    print("Setting up JetStream streams...")

    for stream_def in STREAMS:
        config = StreamConfig(
            name=stream_def["name"],
            subjects=stream_def["subjects"],
            description=stream_def["description"],
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            max_msgs=100000,  # Max messages per stream
            max_bytes=100 * 1024 * 1024,  # 100MB max per stream
            max_age=60 * 60 * 24 * 30,  # 30 days retention
            duplicate_window=60 * 5,  # 5 minute deduplication window
        )

        try:
            # Try to get existing stream
            stream = await js.stream_info(stream_def["name"])
            print(f"  Stream {stream_def['name']} already exists, updating...")
            await js.update_stream(config)
        except Exception as e:
            if "stream not found" in str(e).lower():
                # Create new stream
                print(f"  Creating stream {stream_def['name']}...")
                await js.add_stream(config)
            else:
                raise

        print(f"  ✓ {stream_def['name']}: {stream_def['subjects']}")

    print("\nAll streams configured successfully!")

    # Print stream info
    print("\nStream Status:")
    for stream_def in STREAMS:
        try:
            info = await js.stream_info(stream_def["name"])
            print(f"  {stream_def['name']}: {info.state.messages} messages, {info.state.bytes} bytes")
        except Exception as e:
            print(f"  {stream_def['name']}: Error getting info - {e}")

    await nc.close()


async def delete_streams(nats_url: str = "nats://localhost:4222") -> None:
    """
    Delete all streams (for cleanup/reset).

    Args:
        nats_url: NATS server URL
    """
    nc = await nats.connect(nats_url)
    js = nc.jetstream()

    print(f"Connected to NATS at {nats_url}")
    print("Deleting JetStream streams...")

    for stream_def in STREAMS:
        try:
            await js.delete_stream(stream_def["name"])
            print(f"  ✓ Deleted {stream_def['name']}")
        except Exception as e:
            if "stream not found" in str(e).lower():
                print(f"  - {stream_def['name']} does not exist")
            else:
                print(f"  ✗ Error deleting {stream_def['name']}: {e}")

    await nc.close()


async def show_status(nats_url: str = "nats://localhost:4222") -> None:
    """
    Show status of all streams.

    Args:
        nats_url: NATS server URL
    """
    nc = await nats.connect(nats_url)
    js = nc.jetstream()

    print(f"Connected to NATS at {nats_url}")
    print("\nStream Status:")

    for stream_def in STREAMS:
        try:
            info = await js.stream_info(stream_def["name"])
            print(f"\n{stream_def['name']}:")
            print(f"  Subjects: {stream_def['subjects']}")
            print(f"  Messages: {info.state.messages}")
            print(f"  Bytes: {info.state.bytes}")
            print(f"  First seq: {info.state.first_seq}")
            print(f"  Last seq: {info.state.last_seq}")
        except Exception as e:
            if "stream not found" in str(e).lower():
                print(f"\n{stream_def['name']}: NOT CREATED")
            else:
                print(f"\n{stream_def['name']}: Error - {e}")

    await nc.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Setup NATS JetStream streams for City in a Bottle")
    parser.add_argument(
        "command",
        choices=["setup", "delete", "status"],
        default="setup",
        nargs="?",
        help="Command to run (default: setup)",
    )
    parser.add_argument(
        "--nats-url",
        default="nats://localhost:4222",
        help="NATS server URL",
    )

    args = parser.parse_args()

    if args.command == "setup":
        asyncio.run(setup_streams(args.nats_url))
    elif args.command == "delete":
        asyncio.run(delete_streams(args.nats_url))
    elif args.command == "status":
        asyncio.run(show_status(args.nats_url))
