"""
Agent Runner for City in a Bottle.

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

# Event logging for replay visualizations
try:
    from ..events.logger import EventLogger
    EVENT_LOGGING_ENABLED = True
except ImportError:
    EVENT_LOGGING_ENABLED = False
    EventLogger = None

# Optional Zulip support
try:
    import zulip
    ZULIP_AVAILABLE = True
except ImportError:
    ZULIP_AVAILABLE = False


@dataclass
class ForgejoConfig:
    """Forgejo configuration for git operations."""
    url: str = "http://localhost:3000"
    username: str = ""
    token: str = ""


@dataclass
class AgentConfig:
    """Agent configuration from config.json."""
    model: str = "claude-sonnet-4-20250514"
    tick_interval_seconds: int = 300
    initial_endowment: int = 100000
    max_turns: int = 10
    debt_limit: Optional[int] = None  # None = unlimited debt allowed
    forgejo: Optional[ForgejoConfig] = None  # Git operations config


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
        zulip_url: str = "http://localhost:8080",
        message_bus: str = "nats",  # "nats" or "zulip"
        postgres_host: str = "localhost",
        postgres_port: int = 5432,
        postgres_db: str = "agent_economy",
        postgres_user: str = "agent_economy",
        postgres_password: str = "agent_economy_dev",
        forgejo_url: str = "http://localhost:3000",
    ):
        self.agents_base_dir = Path(agents_base_dir).resolve()
        self.nats_url = nats_url
        self.zulip_url = zulip_url
        self.message_bus = message_bus
        self.forgejo_url = forgejo_url
        self.postgres_config = {
            "host": postgres_host,
            "port": postgres_port,
            "database": postgres_db,
            "user": postgres_user,
            "password": postgres_password,
        }

        if message_bus == "zulip" and not ZULIP_AVAILABLE:
            raise RuntimeError("Zulip message bus selected but 'zulip' package not installed")

    def _load_config(self, agent_id: str) -> AgentConfig:
        """Load agent configuration."""
        config_path = self.agents_base_dir / agent_id / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                data = json.load(f)
                # Parse nested forgejo config if present
                forgejo_data = data.pop("forgejo", None)
                forgejo_config = None
                if forgejo_data:
                    forgejo_config = ForgejoConfig(**forgejo_data)
                return AgentConfig(**data, forgejo=forgejo_config)
        return AgentConfig()

    def _load_system_rules(self) -> str:
        """Load the universal system rules (same for all agents)."""
        system_prompt_path = Path(__file__).parent / "system_prompt.md"
        if system_prompt_path.exists():
            return system_prompt_path.read_text()
        return "You are an agent in City in a Bottle."

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
        # Get epoch number from environment (set by run-agent.sh or run_epoch.py)
        epoch_number = os.environ.get("EPOCH_NUMBER", "0")

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
            f"**Epoch:** {epoch_number}",
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

    async def _read_zulip_context(self, agent_id: str) -> tuple[str, list[dict]]:
        """Read board context from Zulip channels.

        Returns:
            Tuple of (context_markdown, messages_read_list)
        """
        agent_dir = self.agents_base_dir / agent_id
        zuliprc_path = agent_dir / ".zuliprc"

        if not zuliprc_path.exists():
            return "No Zulip context available (bot not configured).", []

        # Allow ZULIP_URL to override .zuliprc site (for Docker networking)
        # Use insecure=True for Docker connections to handle Zulip's self-signed cert
        if self.zulip_url and self.zulip_url != "http://localhost:8080":
            client = zulip.Client(config_file=str(zuliprc_path), site=self.zulip_url, insecure=True)
        else:
            client = zulip.Client(config_file=str(zuliprc_path))
        messages_read = []
        context_parts = ["## Current Board Messages\n"]

        # Channels to read
        channels = [
            ("job-board", "Jobs and Bids"),
            ("results", "Work Results"),
            ("system", "System Announcements"),
        ]

        for channel_name, channel_label in channels:
            try:
                result = client.get_messages({
                    "narrow": [{"operator": "stream", "operand": channel_name}],
                    "num_before": 20,
                    "num_after": 0,
                    "anchor": "newest",
                })

                if result.get("result") == "success":
                    msgs = result.get("messages", [])
                    if msgs:
                        context_parts.append(f"### {channel_label.upper()} (#{channel_name})\n")
                        for msg in msgs[-10:]:  # Last 10
                            sender = msg["sender_email"].split("@")[0]
                            topic = msg.get("subject", "")
                            content_preview = msg["content"][:200]

                            messages_read.append({
                                "msg_id": str(msg["id"]),
                                "channel": channel_name,
                                "topic": topic,
                                "from_agent": sender,
                            })

                            context_parts.append(
                                f"- **[{sender}]** ({topic}): {content_preview}\n"
                            )
                        context_parts.append("\n")
            except Exception as e:
                context_parts.append(f"### {channel_label} (error: {e})\n\n")

        return "".join(context_parts), messages_read

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

        # Get epoch number from environment (set by run-agent.sh or run_epoch.py)
        epoch_number = int(os.environ.get("EPOCH_NUMBER", "0")) or None

        # Initialize event logger for replay visualizations
        event_logger = None
        if EVENT_LOGGING_ENABLED:
            try:
                event_logger = EventLogger()
            except Exception as e:
                print(f"Warning: Could not initialize event logger: {e}")

        # Initialize clients
        board = None
        if self.message_bus == "nats":
            board = BoardClient(self.nats_url)
        ledger = LedgerClient(**self.postgres_config)
        sandbox = Sandbox(agent_id, str(self.agents_base_dir))

        try:
            if board:
                await board.connect()
            await ledger.connect()

            # Load config and state
            config = self._load_config(agent_id)
            system_rules = self._load_system_rules()
            personality = self._load_personality(agent_id)
            ctx.core_memory = self._load_core_memory(agent_id)
            ctx.balance_before = await ledger.get_balance(agent_id)

            # Log agent start for replay visualizations
            if event_logger:
                event_logger.log_agent_start(
                    agent_id=agent_id,
                    run_id=str(ctx.run_id),
                    epoch_number=epoch_number,
                    balance=ctx.balance_before,
                )

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
            if self.message_bus == "zulip":
                board_context, ctx.messages_read = await self._read_zulip_context(agent_id)
            else:
                # NATS message reading
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

            # Choose message board MCP server based on configuration
            if self.message_bus == "zulip":
                board_mcp_config = {
                    "type": "stdio",
                    "command": python_cmd,
                    "args": [str(project_root / "src" / "mcp_servers" / "zulip_server.py")],
                    "env": {
                        "AGENT_DIR": str(agent_dir),
                        "AGENT_ID": agent_id,
                        "ZULIP_SITE": self.zulip_url,
                        "POSTGRES_HOST": self.postgres_config["host"],
                        "POSTGRES_PORT": str(self.postgres_config["port"]),
                        "POSTGRES_DB": self.postgres_config["database"],
                        "POSTGRES_USER": self.postgres_config["user"],
                        "POSTGRES_PASSWORD": self.postgres_config["password"],
                    },
                }
            else:
                board_mcp_config = {
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
                }

            mcp_servers = {
                "board": board_mcp_config,
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

            # Add Forgejo MCP server if agent has git credentials configured
            if config.forgejo and config.forgejo.token:
                mcp_servers["forgejo"] = {
                    "type": "stdio",
                    "command": python_cmd,
                    "args": [str(project_root / "src" / "mcp_servers" / "forgejo_server.py")],
                    "env": {
                        "FORGEJO_URL": config.forgejo.url or self.forgejo_url,
                        "FORGEJO_TOKEN": config.forgejo.token,
                        "AGENT_ID": agent_id,
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
                            tool_input = block.input if hasattr(block, 'input') else {}
                            ctx.actions.append({
                                "type": "tool_use",
                                "tool": block.name,
                                "input": tool_input,
                            })
                            # Log for replay visualizations
                            if event_logger:
                                event_logger.log_tool_call(
                                    agent_id=agent_id,
                                    tool_name=block.name,
                                    input_data=tool_input,
                                    run_id=str(ctx.run_id),
                                    epoch_number=epoch_number,
                                )

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

            # ── Memory Trigger ────────────────────────────────────────────────
            # After every run, inject a guaranteed status snapshot into
            # memories/status.md so agents always wake up with fresh context,
            # even if they ran out of turns before writing their own memories.
            # Agent-written content is preserved above the snapshot header.
            final_balance = await ledger.get_balance(agent_id)
            action_summary = {}
            for a in ctx.actions:
                tool = a.get("tool", a.get("type", "unknown"))
                action_summary[tool] = action_summary.get(tool, 0) + 1
            action_lines = "\n".join(
                f"  - {tool}: {count}" for tool, count in sorted(action_summary.items())
            )
            snapshot = (
                f"\n\n---\n"
                f"<!-- RUNNER SNAPSHOT — auto-written at run end, do not remove -->\n"
                f"**Last run:** Epoch {epoch_number} | {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC\n"
                f"**Balance:** {final_balance:,} tokens "
                f"(spent {ctx.tokens_out:,} this run)\n"
                f"**Actions this run:**\n{action_lines if action_lines else '  (none)'}\n"
                f"<!-- END RUNNER SNAPSHOT -->\n"
            )
            status_path = agent_dir / "memories" / "status.md"
            status_path.parent.mkdir(parents=True, exist_ok=True)
            existing = status_path.read_text() if status_path.exists() else ""
            # Strip any previous snapshot so we don't accumulate them
            if "<!-- RUNNER SNAPSHOT" in existing:
                existing = existing[:existing.index("\n\n---\n<!-- RUNNER SNAPSHOT")]
            status_path.write_text(existing.strip() + snapshot)
            # ─────────────────────────────────────────────────────────────────

            # Record run
            await self._record_run(ctx, status="completed")

            # final_balance already fetched above in memory trigger

            # Log agent end for replay visualizations
            if event_logger:
                event_logger.log_agent_end(
                    agent_id=agent_id,
                    run_id=str(ctx.run_id),
                    epoch_number=epoch_number,
                    balance=final_balance,
                    status="completed",
                    tokens_spent=ctx.tokens_out,
                )

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
            # Log agent end with error for replay visualizations
            if event_logger:
                event_logger.log_agent_end(
                    agent_id=agent_id,
                    run_id=str(ctx.run_id),
                    epoch_number=epoch_number,
                    balance=ctx.balance_before,  # May not have updated
                    status="error",
                    tokens_spent=ctx.tokens_out,
                )
            # Record failed run
            await self._record_run(ctx, status="error", error_message=str(e))
            raise

        finally:
            if event_logger:
                event_logger.close()
            if board:
                await board.close()
            await ledger.close()


# CLI support
if __name__ == "__main__":
    import argparse

    async def main():
        parser = argparse.ArgumentParser(description="Run an agent")
        parser.add_argument("agent_id", help="Agent ID to run")
        parser.add_argument("--agents-dir", default="agents", help="Agents base directory")
        parser.add_argument("--nats-url", default=os.environ.get("NATS_URL", "nats://localhost:4222"),
                          help="NATS URL (default: $NATS_URL or nats://localhost:4222)")
        parser.add_argument("--zulip-url", default=os.environ.get("ZULIP_URL", "http://localhost:8080"),
                          help="Zulip URL (default: $ZULIP_URL or http://localhost:8080)")
        parser.add_argument("--message-bus", default="zulip", choices=["nats", "zulip"],
                          help="Message bus to use (default: zulip)")
        parser.add_argument("--postgres-host", default=os.environ.get("POSTGRES_HOST", "localhost"),
                          help="Postgres host (default: $POSTGRES_HOST or localhost)")

        args = parser.parse_args()

        runner = AgentRunner(
            agents_base_dir=args.agents_dir,
            nats_url=args.nats_url,
            zulip_url=args.zulip_url,
            message_bus=args.message_bus,
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
