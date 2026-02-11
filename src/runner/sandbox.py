"""
Sandbox enforcement for City in a Bottle.

Validates file paths and restricts agent operations to their own directories.
"""

import os
from pathlib import Path
from typing import Optional


class SandboxError(Exception):
    """Raised when a sandbox violation is detected."""
    pass


class Sandbox:
    """
    Sandbox enforcement for agent operations.

    Agents can only access files within their own directory.
    """

    def __init__(self, agent_id: str, agents_base_dir: str = "agents"):
        """
        Initialize sandbox for an agent.

        Args:
            agent_id: The agent's ID
            agents_base_dir: Base directory containing all agent folders
        """
        self.agent_id = agent_id
        self.agents_base_dir = Path(agents_base_dir).resolve()
        self.agent_dir = (self.agents_base_dir / agent_id).resolve()

        if not self.agent_dir.exists():
            raise SandboxError(f"Agent directory does not exist: {self.agent_dir}")

    def validate_path(self, path: str, must_exist: bool = False) -> Path:
        """
        Validate that a path is within the agent's sandbox.

        Args:
            path: Path to validate (can be relative or absolute)
            must_exist: If True, raise error if path doesn't exist

        Returns:
            Resolved absolute path

        Raises:
            SandboxError: If path is outside agent directory
        """
        # Handle relative paths as relative to agent dir
        if not os.path.isabs(path):
            resolved = (self.agent_dir / path).resolve()
        else:
            resolved = Path(path).resolve()

        # Check if path is within agent directory
        try:
            resolved.relative_to(self.agent_dir)
        except ValueError:
            raise SandboxError(
                f"Path '{path}' is outside agent sandbox. "
                f"Agent can only access files in {self.agent_dir}"
            )

        if must_exist and not resolved.exists():
            raise SandboxError(f"Path does not exist: {resolved}")

        return resolved

    def read_file(self, path: str) -> str:
        """
        Read a file within the sandbox.

        Args:
            path: Path to file (relative to agent dir or absolute)

        Returns:
            File contents
        """
        resolved = self.validate_path(path, must_exist=True)

        if not resolved.is_file():
            raise SandboxError(f"Not a file: {resolved}")

        return resolved.read_text()

    def write_file(self, path: str, content: str) -> Path:
        """
        Write a file within the sandbox.

        Args:
            path: Path to file (relative to agent dir or absolute)
            content: Content to write

        Returns:
            Path to written file
        """
        resolved = self.validate_path(path)

        # Create parent directories if needed
        resolved.parent.mkdir(parents=True, exist_ok=True)

        resolved.write_text(content)
        return resolved

    def list_files(self, path: str = ".") -> list[str]:
        """
        List files in a directory within the sandbox.

        Args:
            path: Path to directory (relative to agent dir or absolute)

        Returns:
            List of file/directory names
        """
        resolved = self.validate_path(path, must_exist=True)

        if not resolved.is_dir():
            raise SandboxError(f"Not a directory: {resolved}")

        return [p.name for p in resolved.iterdir()]

    def exists(self, path: str) -> bool:
        """
        Check if a path exists within the sandbox.

        Args:
            path: Path to check

        Returns:
            True if path exists
        """
        try:
            resolved = self.validate_path(path)
            return resolved.exists()
        except SandboxError:
            return False

    def get_relative_path(self, path: str) -> str:
        """
        Get path relative to agent directory.

        Args:
            path: Absolute or relative path

        Returns:
            Path relative to agent directory
        """
        resolved = self.validate_path(path)
        return str(resolved.relative_to(self.agent_dir))

    @property
    def memory_dir(self) -> Path:
        """Get the agent's memory directory."""
        return self.agent_dir / "memory"

    @property
    def skills_dir(self) -> Path:
        """Get the agent's skills directory."""
        return self.agent_dir / "skills"

    @property
    def core_memory_path(self) -> Path:
        """Get path to agent's core memory file."""
        return self.memory_dir / "core.md"

    @property
    def config_path(self) -> Path:
        """Get path to agent's config file."""
        return self.agent_dir / "config.json"

    @property
    def instructions_path(self) -> Path:
        """Get path to agent's instruction file."""
        return self.agent_dir / "agent.md"
