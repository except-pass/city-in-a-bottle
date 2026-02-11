"""
Tool definitions for City in a Bottle.

These tools are made available to agents via the Claude SDK.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass

from .sandbox import Sandbox, SandboxError


@dataclass
class ToolResult:
    """Result from a tool execution."""
    success: bool
    result: Any
    error: Optional[str] = None


class AgentTools:
    """
    Tools available to agents.

    All file operations are sandboxed to the agent's directory.
    """

    def __init__(
        self,
        agent_id: str,
        sandbox: Sandbox,
        board_client: Any,  # BoardClient
        ledger_client: Any,  # LedgerClient
    ):
        self.agent_id = agent_id
        self.sandbox = sandbox
        self.board_client = board_client
        self.ledger_client = ledger_client
        self._actions: list[dict] = []  # Track actions for logging

    def get_actions(self) -> list[dict]:
        """Get list of actions taken during this run."""
        return self._actions.copy()

    def _record_action(self, action_type: str, **details):
        """Record an action for logging."""
        self._actions.append({"type": action_type, **details})

    # File operations (sandboxed)

    async def read_file(self, path: str) -> ToolResult:
        """
        Read a file from the agent's directory.

        Args:
            path: Path to file (relative to agent directory)

        Returns:
            File contents or error
        """
        try:
            content = self.sandbox.read_file(path)
            return ToolResult(success=True, result=content)
        except SandboxError as e:
            return ToolResult(success=False, result=None, error=str(e))
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"Error reading file: {e}")

    async def write_file(self, path: str, content: str) -> ToolResult:
        """
        Write a file to the agent's directory.

        Args:
            path: Path to file (relative to agent directory)
            content: Content to write

        Returns:
            Success status or error
        """
        try:
            written_path = self.sandbox.write_file(path, content)
            self._record_action(
                "modify_self",
                file=path,
                change_summary=f"Wrote {len(content)} chars to {path}",
            )
            return ToolResult(success=True, result=f"Written to {written_path}")
        except SandboxError as e:
            return ToolResult(success=False, result=None, error=str(e))
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"Error writing file: {e}")

    async def list_files(self, path: str = ".") -> ToolResult:
        """
        List files in a directory.

        Args:
            path: Path to directory (relative to agent directory)

        Returns:
            List of files or error
        """
        try:
            files = self.sandbox.list_files(path)
            return ToolResult(success=True, result=files)
        except SandboxError as e:
            return ToolResult(success=False, result=None, error=str(e))
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"Error listing files: {e}")

    # Board operations

    async def read_board(
        self,
        subject: str,
        limit: int = 50,
    ) -> ToolResult:
        """
        Read messages from the message board.

        Args:
            subject: Subject to read from ('jobs', 'bids', 'work', 'results', 'meta')
            limit: Maximum number of messages to return

        Returns:
            List of messages or error
        """
        from ..board.client import MessageType

        try:
            msg_type = MessageType(subject)
            messages = await self.board_client.read_messages(msg_type, limit=limit)
            result = [msg.to_dict() for msg in messages]
            return ToolResult(success=True, result=result)
        except ValueError:
            return ToolResult(
                success=False,
                result=None,
                error=f"Invalid subject: {subject}. Valid: jobs, bids, work, results, meta",
            )
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"Error reading board: {e}")

    async def post_message(
        self,
        subject: str,
        content: dict,
        thread_id: Optional[str] = None,
        refs: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
    ) -> ToolResult:
        """
        Post a message to the message board.

        Args:
            subject: Subject to post to ('jobs', 'bids', 'work', 'results', 'meta')
            content: Message content (dict)
            thread_id: Optional thread ID for replies
            refs: Optional list of referenced message IDs
            tags: Optional list of tags

        Returns:
            Posted message info or error
        """
        from ..board.client import MessageType

        try:
            msg_type = MessageType(subject)
            msg = await self.board_client.post_message(
                msg_type,
                self.agent_id,
                content,
                thread_id=thread_id,
                refs=refs,
                tags=tags,
            )
            self._record_action(
                "post_message",
                subject=subject,
                msg_id=msg.msg_id,
                content_summary=str(content)[:100],
            )
            return ToolResult(success=True, result=msg.to_dict())
        except ValueError:
            return ToolResult(
                success=False,
                result=None,
                error=f"Invalid subject: {subject}. Valid: jobs, bids, work, results, meta",
            )
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"Error posting message: {e}")

    # Ledger operations

    async def get_balance(self) -> ToolResult:
        """
        Get the agent's current token balance.

        Returns:
            Current balance
        """
        try:
            balance = await self.ledger_client.get_balance(self.agent_id)
            return ToolResult(success=True, result=balance)
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"Error getting balance: {e}")

    async def transfer_tokens(
        self,
        to_agent: str,
        amount: int,
        reason: str,
    ) -> ToolResult:
        """
        Transfer tokens to another agent.

        Args:
            to_agent: Recipient agent ID
            amount: Number of tokens to transfer
            reason: Reason for transfer

        Returns:
            Transfer result or error
        """
        try:
            if amount <= 0:
                return ToolResult(success=False, result=None, error="Amount must be positive")

            out_tx, in_tx = await self.ledger_client.transfer(
                self.agent_id,
                to_agent,
                amount,
                reason,
            )
            self._record_action(
                "transfer",
                to_agent=to_agent,
                amount=amount,
                reason=reason,
            )
            return ToolResult(
                success=True,
                result={
                    "transferred": amount,
                    "to": to_agent,
                    "new_balance": out_tx.balance_after,
                },
            )
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"Error transferring tokens: {e}")

    # Code execution (sandboxed)

    async def run_code(
        self,
        code: str,
        language: str = "python",
        timeout: int = 30,
    ) -> ToolResult:
        """
        Execute code in a sandboxed environment.

        Args:
            code: Code to execute
            language: Programming language ('python', 'bash')
            timeout: Execution timeout in seconds

        Returns:
            Execution output or error
        """
        if language not in ("python", "bash"):
            return ToolResult(
                success=False,
                result=None,
                error=f"Unsupported language: {language}. Supported: python, bash",
            )

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py" if language == "python" else ".sh",
                delete=False,
            ) as f:
                f.write(code)
                f.flush()
                script_path = f.name

            try:
                if language == "python":
                    cmd = ["python3", script_path]
                else:
                    cmd = ["bash", script_path]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(self.sandbox.agent_dir),  # Run in agent's directory
                )

                output = result.stdout
                if result.stderr:
                    output += f"\nSTDERR:\n{result.stderr}"

                return ToolResult(
                    success=result.returncode == 0,
                    result=output,
                    error=None if result.returncode == 0 else f"Exit code: {result.returncode}",
                )

            finally:
                Path(script_path).unlink(missing_ok=True)

        except subprocess.TimeoutExpired:
            return ToolResult(success=False, result=None, error=f"Execution timed out after {timeout}s")
        except Exception as e:
            return ToolResult(success=False, result=None, error=f"Error executing code: {e}")

    # Web search (placeholder - would integrate with actual search API)

    async def web_search(self, query: str, limit: int = 5) -> ToolResult:
        """
        Search the web.

        Args:
            query: Search query
            limit: Maximum number of results

        Returns:
            Search results or error

        Note: This is a placeholder. In production, integrate with a search API.
        """
        # Placeholder - would integrate with DuckDuckGo, Brave, or similar
        return ToolResult(
            success=False,
            result=None,
            error="Web search not implemented. Please implement with your preferred search API.",
        )


def get_tool_definitions() -> list[dict]:
    """
    Get tool definitions for the Claude SDK.

    Returns:
        List of tool definitions in Claude SDK format
    """
    return [
        {
            "name": "read_file",
            "description": "Read a file from your agent directory. You can only read files within your own directory.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to file (relative to your agent directory)",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "Write a file to your agent directory. You can only write files within your own directory. Use this to update your memory, create tools, or modify your own instructions.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to file (relative to your agent directory)",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write to the file",
                    },
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "list_files",
            "description": "List files in a directory within your agent directory.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to directory (relative to your agent directory, default: '.')",
                        "default": ".",
                    },
                },
            },
        },
        {
            "name": "read_board",
            "description": "Read messages from the message board. Use this to find jobs, see bids, check work status, and read results.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Board subject to read from",
                        "enum": ["job", "bid", "status", "result", "meta"],
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of messages to return (default: 50)",
                        "default": 50,
                    },
                },
                "required": ["subject"],
            },
        },
        {
            "name": "post_message",
            "description": "Post a message to the message board. Use this to post jobs, submit bids, update status, or deliver results.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Board subject to post to",
                        "enum": ["job", "bid", "status", "result", "meta"],
                    },
                    "content": {
                        "type": "object",
                        "description": "Message content as a JSON object",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Optional thread ID for replies",
                    },
                    "refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of referenced message IDs",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of tags",
                    },
                },
                "required": ["subject", "content"],
            },
        },
        {
            "name": "get_balance",
            "description": "Get your current token balance. Use this to check how many tokens you have before taking expensive actions.",
            "input_schema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "transfer_tokens",
            "description": "Transfer tokens to another agent. Use this to pay for services or collaborate.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "to_agent": {
                        "type": "string",
                        "description": "ID of the agent to transfer to",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Number of tokens to transfer (must be positive)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for the transfer",
                    },
                },
                "required": ["to_agent", "amount", "reason"],
            },
        },
        {
            "name": "run_code",
            "description": "Execute code in a sandboxed environment. The code runs in your agent directory.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Code to execute",
                    },
                    "language": {
                        "type": "string",
                        "description": "Programming language",
                        "enum": ["python", "bash"],
                        "default": "python",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Execution timeout in seconds (default: 30)",
                        "default": 30,
                    },
                },
                "required": ["code"],
            },
        },
        {
            "name": "web_search",
            "description": "Search the web for information.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    ]


def get_agent_tools(
    agent_id: str,
    sandbox: Sandbox,
    board_client: Any,
    ledger_client: Any,
) -> AgentTools:
    """
    Create tools instance for an agent.

    Args:
        agent_id: The agent's ID
        sandbox: Sandbox instance for the agent
        board_client: BoardClient instance
        ledger_client: LedgerClient instance

    Returns:
        AgentTools instance
    """
    return AgentTools(agent_id, sandbox, board_client, ledger_client)
