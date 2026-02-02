"""Pydantic models for API responses."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class TokenStats(BaseModel):
    """Token economy statistics."""
    circulation: int
    escrowed: int
    velocity_24h: int


class AgentStats(BaseModel):
    """Agent statistics by status."""
    total: int
    active: int
    idle: int
    in_debt: int


class JobStats(BaseModel):
    """Job statistics by status."""
    total: int
    open: int
    in_progress: int
    submitted: int
    completed: int


class LeaderboardEntry(BaseModel):
    """Single entry in agent leaderboard."""
    agent_id: str
    balance: int
    total_transactions: int
    jobs_completed: int
    delta_24h: int


class JobItem(BaseModel):
    """Job list item."""
    job_id: str
    title: str
    description: str
    reward: int
    status: str
    assigned_agent: Optional[str] = None
    created_at: datetime
    tags: list[str] = []


class SystemEvent(BaseModel):
    """System event for SSE stream."""
    timestamp: datetime
    category: str  # LEDGER, JOBS, RUNNER, BOARD, SCHED, MCP, WARN
    message: str
    color: str  # CSS color class


class HealthStatus(BaseModel):
    """Infrastructure health status."""
    postgresql: bool
    nats: bool
    overall: bool


class QuickStats(BaseModel):
    """Quick stats for dashboard footer."""
    agent_runs_today: int
    messages_posted: int
    job_success_rate: float
    avg_response_time: float
