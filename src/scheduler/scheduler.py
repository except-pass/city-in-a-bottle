#!/usr/bin/env python3
"""
Scheduler for Agent Economy.

Handles per-agent scheduling with configurable tick intervals,
error backoff, and debt handling.
"""

import asyncio
import json
import logging
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Add parent to path for imports
sys.path.insert(0, str(__file__).rsplit("/", 3)[0])

from src.runner.runner import AgentRunner
from src.ledger.client import LedgerClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scheduler")


@dataclass
class AgentScheduleState:
    """State for a scheduled agent."""
    agent_id: str
    tick_interval: int  # seconds
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    consecutive_errors: int = 0
    backoff_until: Optional[datetime] = None
    is_paused: bool = False
    pause_reason: Optional[str] = None

    def calculate_next_run(self) -> datetime:
        """Calculate the next run time."""
        base_time = self.last_run or datetime.utcnow()

        # Apply backoff if there were errors
        if self.consecutive_errors > 0:
            backoff_multiplier = min(2 ** self.consecutive_errors, 32)  # Max 32x backoff
            interval = self.tick_interval * backoff_multiplier
        else:
            interval = self.tick_interval

        self.next_run = base_time + timedelta(seconds=interval)
        return self.next_run

    def record_success(self) -> None:
        """Record a successful run."""
        self.last_run = datetime.utcnow()
        self.consecutive_errors = 0
        self.backoff_until = None
        self.calculate_next_run()

    def record_error(self) -> None:
        """Record a failed run."""
        self.last_run = datetime.utcnow()
        self.consecutive_errors += 1
        self.calculate_next_run()
        logger.warning(
            f"Agent {self.agent_id} error #{self.consecutive_errors}, "
            f"next run at {self.next_run}"
        )

    def pause(self, reason: str) -> None:
        """Pause the agent."""
        self.is_paused = True
        self.pause_reason = reason
        logger.info(f"Agent {self.agent_id} paused: {reason}")

    def resume(self) -> None:
        """Resume the agent."""
        self.is_paused = False
        self.pause_reason = None
        self.calculate_next_run()
        logger.info(f"Agent {self.agent_id} resumed")


class Scheduler:
    """
    Scheduler for running multiple agents.

    Handles:
    - Per-agent tick intervals
    - Error backoff
    - Debt monitoring
    - Graceful shutdown
    """

    def __init__(
        self,
        agents_base_dir: str = "agents",
        nats_url: str = "nats://localhost:4222",
        postgres_host: str = "localhost",
        postgres_port: int = 5432,
        debt_pause_threshold: Optional[int] = None,
    ):
        self.agents_base_dir = Path(agents_base_dir).resolve()
        self.nats_url = nats_url
        self.postgres_host = postgres_host
        self.postgres_port = postgres_port
        self.debt_pause_threshold = debt_pause_threshold

        self.runner = AgentRunner(
            agents_base_dir=str(self.agents_base_dir),
            nats_url=nats_url,
            postgres_host=postgres_host,
            postgres_port=postgres_port,
        )

        self.agents: dict[str, AgentScheduleState] = {}
        self._running = False
        self._shutdown_event = asyncio.Event()

    def _load_agent_config(self, agent_id: str) -> dict:
        """Load agent configuration."""
        config_path = self.agents_base_dir / agent_id / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                return json.load(f)
        return {}

    def discover_agents(self) -> list[str]:
        """Discover all agents in the agents directory."""
        agents = []
        if self.agents_base_dir.exists():
            for path in self.agents_base_dir.iterdir():
                if path.is_dir() and (path / "agent.md").exists():
                    agents.append(path.name)
        return agents

    def add_agent(self, agent_id: str) -> None:
        """Add an agent to the scheduler."""
        if agent_id in self.agents:
            logger.warning(f"Agent {agent_id} already scheduled")
            return

        config = self._load_agent_config(agent_id)
        tick_interval = config.get("tick_interval_seconds", 300)

        state = AgentScheduleState(
            agent_id=agent_id,
            tick_interval=tick_interval,
        )
        state.calculate_next_run()

        self.agents[agent_id] = state
        logger.info(f"Added agent {agent_id} (tick interval: {tick_interval}s)")

    def remove_agent(self, agent_id: str) -> None:
        """Remove an agent from the scheduler."""
        if agent_id in self.agents:
            del self.agents[agent_id]
            logger.info(f"Removed agent {agent_id}")

    async def _check_debt(self, agent_id: str) -> bool:
        """
        Check if agent should be paused due to debt.

        Returns True if agent is OK to run.
        """
        if self.debt_pause_threshold is None:
            return True

        ledger = LedgerClient(host=self.postgres_host, port=self.postgres_port)
        await ledger.connect()
        try:
            balance = await ledger.get_balance(agent_id)
            if balance < -self.debt_pause_threshold:
                state = self.agents.get(agent_id)
                if state and not state.is_paused:
                    state.pause(f"Debt exceeds threshold ({balance} < -{self.debt_pause_threshold})")
                return False
            return True
        finally:
            await ledger.close()

    async def _run_agent(self, agent_id: str) -> None:
        """Run a single agent."""
        state = self.agents.get(agent_id)
        if not state:
            return

        if state.is_paused:
            logger.debug(f"Agent {agent_id} is paused, skipping")
            return

        # Check debt
        if not await self._check_debt(agent_id):
            return

        logger.info(f"Running agent {agent_id}")

        try:
            result = await self.runner.run_once(agent_id)
            state.record_success()
            logger.info(
                f"Agent {agent_id} completed: "
                f"{result['tokens_charged']} tokens, "
                f"{len(result['actions'])} actions, "
                f"balance: {result['balance_after']}"
            )
        except Exception as e:
            state.record_error()
            logger.error(f"Agent {agent_id} error: {e}")

    async def _scheduler_loop(self) -> None:
        """Main scheduler loop."""
        logger.info("Scheduler loop started")

        while self._running:
            now = datetime.utcnow()

            # Find agents ready to run
            ready_agents = []
            for agent_id, state in self.agents.items():
                if state.is_paused:
                    continue
                if state.next_run and state.next_run <= now:
                    ready_agents.append(agent_id)

            # Run ready agents (sequentially for now)
            for agent_id in ready_agents:
                if not self._running:
                    break
                await self._run_agent(agent_id)

            # Sleep until next check
            await asyncio.sleep(1)

            # Check for shutdown
            if self._shutdown_event.is_set():
                break

        logger.info("Scheduler loop stopped")

    def _handle_signal(self, signum, frame) -> None:
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, initiating shutdown")
        self._running = False
        self._shutdown_event.set()

    async def run(self) -> None:
        """Run the scheduler."""
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        self._running = True
        self._shutdown_event.clear()

        logger.info(f"Scheduler starting with {len(self.agents)} agent(s)")
        for agent_id, state in self.agents.items():
            logger.info(f"  {agent_id}: next run at {state.next_run}")

        await self._scheduler_loop()

        logger.info("Scheduler stopped")

    async def run_all_once(self) -> dict[str, dict]:
        """Run all agents once (for testing)."""
        results = {}
        for agent_id in self.agents:
            try:
                result = await self.runner.run_once(agent_id)
                results[agent_id] = result
            except Exception as e:
                results[agent_id] = {"error": str(e)}
        return results


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Agent economy scheduler")
    parser.add_argument(
        "--agents-dir",
        default="agents",
        help="Agents base directory",
    )
    parser.add_argument(
        "--nats-url",
        default="nats://localhost:4222",
        help="NATS URL",
    )
    parser.add_argument(
        "--postgres-host",
        default="localhost",
        help="Postgres host",
    )
    parser.add_argument(
        "--debt-threshold",
        type=int,
        default=None,
        help="Pause agents with debt below this threshold",
    )
    parser.add_argument(
        "--agents",
        nargs="*",
        help="Specific agents to schedule (default: all)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run all agents once and exit",
    )

    args = parser.parse_args()

    scheduler = Scheduler(
        agents_base_dir=args.agents_dir,
        nats_url=args.nats_url,
        postgres_host=args.postgres_host,
        debt_pause_threshold=args.debt_threshold,
    )

    # Add agents
    if args.agents:
        for agent_id in args.agents:
            scheduler.add_agent(agent_id)
    else:
        # Discover all agents
        discovered = scheduler.discover_agents()
        for agent_id in discovered:
            scheduler.add_agent(agent_id)

    if not scheduler.agents:
        logger.error("No agents found")
        return

    if args.once:
        logger.info("Running all agents once")
        results = await scheduler.run_all_once()
        for agent_id, result in results.items():
            if "error" in result:
                logger.error(f"{agent_id}: {result['error']}")
            else:
                logger.info(
                    f"{agent_id}: {result['tokens_charged']} tokens, "
                    f"balance: {result['balance_after']}"
                )
    else:
        await scheduler.run()


if __name__ == "__main__":
    asyncio.run(main())
