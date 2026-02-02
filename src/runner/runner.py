"""
Agent Runner for Agent Economy.

Main execution loop for running agents using Claude Code SDK.
Uses Claude CLI authentication (supports Max subscription).
"""

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4

from claude_code_sdk import query
from claude_code_sdk.types import (
    ClaudeCodeOptions,
    ResultMessage,
    AssistantMessage,
    UserMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)

from .sandbox import Sandbox, SandboxError
from .tools import AgentTools, get_tool_definitions, get_agent_tools
from ..board.client import BoardClient, MessageType
from ..ledger.client import LedgerClient


@dataclass
class AgentConfig:
    """Agent configuration from config.json."""
    model: str = "claude-sonnet-4-20250514"
    tick_interval_seconds: int = 300
    initial_endowment: int = 100000
    max_turns: int = 10
    debt_limit: Optional[int] = None  # None = unlimited debt allowed


@dataclass
class RunContext:
    """Context for a single agent run."""
    run_id: UUID
    agent_id: str
    started_at: datetime
    balance_before: int
    core_memory: str
    messages_read: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    reasoning: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0


class AgentRunner:
    """
    Runs agent execution loops.

    Uses Claude Code SDK which authenticates via Claude CLI
    (supports Max subscription - no API key needed).

    Handles:
    - Loading agent config and state
    - Building context for Claude
    - Executing tool calls
    - Recording runs and debiting tokens
    """

    def __init__(
        self,
        agents_base_dir: str = "agents",
        nats_url: str = "nats://localhost:4222",
        postgres_host: str = "localhost",
        postgres_port: int = 5432,
        postgres_db: str = "agent_economy",
        postgres_user: str = "agent_economy",
        postgres_password: str = "agent_economy_dev",
    ):
        self.agents_base_dir = Path(agents_base_dir).resolve()
        self.nats_url = nats_url
        self.postgres_config = {
            "host": postgres_host,
            "port": postgres_port,
            "database": postgres_db,
            "user": postgres_user,
            "password": postgres_password,
        }

    def _load_config(self, agent_id: str) -> AgentConfig:
        """Load agent configuration."""
        config_path = self.agents_base_dir / agent_id / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                data = json.load(f)
                return AgentConfig(**data)
        return AgentConfig()

    def _load_system_rules(self) -> str:
        """Load the universal system rules (same for all agents)."""
        system_prompt_path = Path(__file__).parent / "system_prompt.md"
        if system_prompt_path.exists():
            return system_prompt_path.read_text()
        return "You are an agent in the agent economy."

    def _load_personality(self, agent_id: str) -> str:
        """Load agent's personality from agent.md (mutable by agent)."""
        personality_path = self.agents_base_dir / agent_id / "agent.md"
        if personality_path.exists():
            return personality_path.read_text()
        return f"You are {agent_id}."

    def _load_core_memory(self, agent_id: str) -> str:
        """Load agent's core memory."""
        memory_path = self.agents_base_dir / agent_id / "memory" / "core.md"
        if memory_path.exists():
            return memory_path.read_text()
        return ""

    def _build_system_prompt(
        self,
        agent_id: str,
        system_rules: str,
        personality: str,
        config: AgentConfig,
        balance: int,
        core_memory: str,
    ) -> str:
        """Build the system prompt for the agent.

        Structure:
        1. System rules (universal, immutable)
        2. Agent personality (from agent.md, mutable by agent)
        3. Current state (balance, warnings)
        4. Core memory (from memory/core.md)
        """
        prompt_parts = [
            system_rules,
            "",
            "---",
            "",
            "# Your Personality",
            "",
            personality,
            "",
            "---",
            "",
            "# Current Session",
            "",
            f"**Agent ID:** {agent_id}",
            f"**Current Balance:** {balance} tokens",
            "",
        ]

        if balance <= 0:
            prompt_parts.extend([
                "⚠️ **WARNING: You are in debt!** Your balance is negative.",
                "Focus on completing jobs to earn tokens and return to positive balance.",
                "",
            ])

        if core_memory:
            prompt_parts.extend([
                "## Your Memory",
                "",
                core_memory,
                "",
            ])

        prompt_parts.extend([
            "---",
            "",
            "## Performance Loop",
            "",
            "1. Check your balance and recent spend",
            "2. Scan the job board for opportunities",
            "3. Decide: bid on a job, collaborate, execute work, or post your own job",
            "4. Do the minimum work needed to make progress",
            "5. Update your memory with what happened",
            "",
            "Remember: Every token counts. Be efficient.",
        ])

        return "\n".join(prompt_parts)

    async def _record_run(
        self,
        ctx: RunContext,
        status: str = "completed",
        error_message: Optional[str] = None,
    ) -> None:
        """Record the run to the database."""
        import asyncpg

        dsn = f"postgresql://{self.postgres_config['user']}:{self.postgres_config['password']}@{self.postgres_config['host']}:{self.postgres_config['port']}/{self.postgres_config['database']}"

        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                """
                INSERT INTO agent_runs
                    (run_id, agent_id, started_at, ended_at, tokens_in, tokens_out,
                     tokens_total, messages_read, actions, artifacts, reasoning, status, error_message)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                ctx.run_id,
                ctx.agent_id,
                ctx.started_at,
                datetime.utcnow(),
                ctx.tokens_in,
                ctx.tokens_out,
                ctx.tokens_out,  # Only output tokens count for billing
                json.dumps(ctx.messages_read),
                json.dumps(ctx.actions),
                json.dumps(ctx.artifacts),
                ctx.reasoning,
                status,
                error_message,
            )
        finally:
            await conn.close()

    async def run_once(self, agent_id: str) -> dict:
        """
        Run a single agent tick.

        Args:
            agent_id: The agent's ID

        Returns:
            Run result including tokens used and actions taken
        """
        # Initialize context
        ctx = RunContext(
            run_id=uuid4(),
            agent_id=agent_id,
            started_at=datetime.utcnow(),
            balance_before=0,
            core_memory="",
        )

        # Initialize clients
        board = BoardClient(self.nats_url)
        ledger = LedgerClient(**self.postgres_config)
        sandbox = Sandbox(agent_id, str(self.agents_base_dir))

        try:
            await board.connect()
            await ledger.connect()

            # Load config and state
            config = self._load_config(agent_id)
            system_rules = self._load_system_rules()
            personality = self._load_personality(agent_id)
            ctx.core_memory = self._load_core_memory(agent_id)
            ctx.balance_before = await ledger.get_balance(agent_id)

            # Agent's own directory is the working directory
            agent_dir = self.agents_base_dir / agent_id

            # Check debt limit
            if config.debt_limit is not None and ctx.balance_before < -config.debt_limit:
                raise RuntimeError(
                    f"Agent {agent_id} has exceeded debt limit "
                    f"({ctx.balance_before} < -{config.debt_limit})"
                )

            # Build system prompt
            system_prompt = self._build_system_prompt(
                agent_id,
                system_rules,
                personality,
                config,
                ctx.balance_before,
                ctx.core_memory,
            )

            # Read board messages for context
            all_messages = await board.read_all_messages(limit_per_type=20)
            board_context_parts = ["## Current Board Messages\n"]

            for msg_type, messages in all_messages.items():
                if messages:
                    board_context_parts.append(f"### {msg_type.value.upper()}\n")
                    for msg in messages[-10:]:  # Last 10 of each type
                        ctx.messages_read.append({
                            "msg_id": msg.msg_id,
                            "subject": f"board.{msg_type.value}",
                            "from_agent": msg.agent_id,
                        })
                        board_context_parts.append(
                            f"- [{msg.agent_id}] {json.dumps(msg.content)[:200]}\n"
                        )
                    board_context_parts.append("\n")

            board_context = "".join(board_context_parts)

            # Build the user prompt - just the board context and a simple instruction
            user_prompt = f"""{board_context}

---

**Your turn.** You have {ctx.balance_before} tokens. Make it count."""

            # Configure MCP servers for native tool access
            # Use sys.executable to work in both venv and Docker environments
            project_root = self.agents_base_dir.parent
            python_cmd = sys.executable
            mcp_servers = {
                "board": {
                    "type": "stdio",
                    "command": python_cmd,
                    "args": [str(project_root / "src" / "mcp_servers" / "board_server.py")],
                    "env": {
                        "NATS_URL": self.nats_url,
                        "AGENT_ID": agent_id,
                        "POSTGRES_HOST": self.postgres_config["host"],
                        "POSTGRES_PORT": str(self.postgres_config["port"]),
                        "POSTGRES_DB": self.postgres_config["database"],
                        "POSTGRES_USER": self.postgres_config["user"],
                        "POSTGRES_PASSWORD": self.postgres_config["password"],
                    },
                },
                "ledger": {
                    "type": "stdio",
                    "command": python_cmd,
                    "args": [str(project_root / "src" / "mcp_servers" / "ledger_server.py")],
                    "env": {
                        "POSTGRES_HOST": self.postgres_config["host"],
                        "POSTGRES_PORT": str(self.postgres_config["port"]),
                        "POSTGRES_DB": self.postgres_config["database"],
                        "POSTGRES_USER": self.postgres_config["user"],
                        "POSTGRES_PASSWORD": self.postgres_config["password"],
                        "AGENT_ID": agent_id,
                    },
                },
            }

            # Configure Claude Code SDK options
            # Agent's cwd is their own directory - they can modify anything there
            # but cannot access other agents' directories or repo source
            options = ClaudeCodeOptions(
                system_prompt=system_prompt,
                model=config.model,
                max_turns=config.max_turns,
                cwd=str(agent_dir),  # Agent's own directory
                permission_mode="bypassPermissions",
                mcp_servers=mcp_servers,
            )

            # Run query using Claude Code SDK
            result_message = None
            assistant_texts = []

            async for message in query(prompt=user_prompt, options=options):
                if isinstance(message, AssistantMessage):
                    # Extract text from assistant messages for reasoning
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            assistant_texts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            # Record tool usage as action
                            ctx.actions.append({
                                "type": "tool_use",
                                "tool": block.name,
                                "input": block.input if hasattr(block, 'input') else {},
                            })

                elif isinstance(message, ResultMessage):
                    result_message = message

            # Extract token usage from result
            if result_message and result_message.usage:
                ctx.tokens_in = result_message.usage.get("input_tokens", 0)
                ctx.tokens_out = result_message.usage.get("output_tokens", 0)

            # Extract reasoning from assistant text
            if assistant_texts:
                ctx.reasoning = "\n".join(assistant_texts)[:500]

            # Debit tokens (output tokens only per design decision)
            if ctx.tokens_out > 0:
                await ledger.debit(
                    agent_id,
                    ctx.tokens_out,
                    "run_cost",
                    run_id=ctx.run_id,
                    note=f"Run {ctx.run_id}: {len(ctx.actions)} actions",
                )

            # Record run
            await self._record_run(ctx, status="completed")

            # Get final balance
            final_balance = await ledger.get_balance(agent_id)

            return {
                "run_id": str(ctx.run_id),
                "agent_id": agent_id,
                "status": "completed",
                "tokens_in": ctx.tokens_in,
                "tokens_out": ctx.tokens_out,
                "tokens_charged": ctx.tokens_out,
                "balance_before": ctx.balance_before,
                "balance_after": final_balance,
                "actions": ctx.actions,
                "reasoning": ctx.reasoning,
                "session_id": result_message.session_id if result_message else None,
            }

        except Exception as e:
            # Record failed run
            await self._record_run(ctx, status="error", error_message=str(e))
            raise

        finally:
            await board.close()
            await ledger.close()


# CLI support
if __name__ == "__main__":
    import argparse

    async def main():
        parser = argparse.ArgumentParser(description="Run an agent")
        parser.add_argument("agent_id", help="Agent ID to run")
        parser.add_argument("--agents-dir", default="agents", help="Agents base directory")
        parser.add_argument("--nats-url", default="nats://localhost:4222", help="NATS URL")
        parser.add_argument("--postgres-host", default="localhost", help="Postgres host")

        args = parser.parse_args()

        runner = AgentRunner(
            agents_base_dir=args.agents_dir,
            nats_url=args.nats_url,
            postgres_host=args.postgres_host,
        )

        print(f"Running agent: {args.agent_id}")
        result = await runner.run_once(args.agent_id)

        print("\nRun Result:")
        print(f"  Run ID: {result['run_id']}")
        print(f"  Status: {result['status']}")
        print(f"  Tokens In: {result['tokens_in']}")
        print(f"  Tokens Out: {result['tokens_out']}")
        print(f"  Tokens Charged: {result['tokens_charged']}")
        print(f"  Balance: {result['balance_before']} -> {result['balance_after']}")
        print(f"  Actions: {len(result['actions'])}")
        if result.get('session_id'):
            print(f"  Session ID: {result['session_id']}")

        if result['actions']:
            print("\n  Actions taken:")
            for action in result['actions']:
                print(f"    - {action['type']}: {action}")

        if result['reasoning']:
            print(f"\n  Reasoning: {result['reasoning'][:200]}...")

    asyncio.run(main())
