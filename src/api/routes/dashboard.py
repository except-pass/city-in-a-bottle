"""REST endpoints for dashboard statistics."""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..models import (
    TokenStats,
    AgentStats,
    JobStats,
    LeaderboardEntry,
    JobItem,
    HealthStatus,
    QuickStats,
)
from ..services.stats import StatsService

router = APIRouter(prefix="/api", tags=["dashboard"])


async def get_stats_service() -> StatsService:
    """Dependency to get stats service instance."""
    # This will be overridden in main.py to use shared service
    service = StatsService()
    await service.connect()
    try:
        yield service
    finally:
        await service.close()


@router.get("/stats/tokens", response_model=TokenStats)
async def get_token_stats(
    stats: StatsService = Depends(get_stats_service),
) -> TokenStats:
    """Get token economy statistics."""
    return await stats.get_token_stats()


@router.get("/stats/agents", response_model=AgentStats)
async def get_agent_stats(
    stats: StatsService = Depends(get_stats_service),
) -> AgentStats:
    """Get agent statistics by status."""
    return await stats.get_agent_stats()


@router.get("/stats/jobs", response_model=JobStats)
async def get_job_stats(
    stats: StatsService = Depends(get_stats_service),
) -> JobStats:
    """Get job statistics by status."""
    return await stats.get_job_stats()


@router.get("/stats/quick", response_model=QuickStats)
async def get_quick_stats(
    stats: StatsService = Depends(get_stats_service),
) -> QuickStats:
    """Get quick stats for dashboard footer."""
    return await stats.get_quick_stats()


@router.get("/agents/leaderboard", response_model=list[LeaderboardEntry])
async def get_leaderboard(
    limit: int = Query(default=10, le=50),
    stats: StatsService = Depends(get_stats_service),
) -> list[LeaderboardEntry]:
    """Get top agents by balance."""
    return await stats.get_leaderboard(limit=limit)


@router.get("/jobs", response_model=list[JobItem])
async def get_jobs(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=20, le=100),
    stats: StatsService = Depends(get_stats_service),
) -> list[JobItem]:
    """Get list of jobs with optional status filter."""
    return await stats.get_jobs_list(status=status, limit=limit)


@router.get("/health", response_model=HealthStatus)
async def get_health(
    stats: StatsService = Depends(get_stats_service),
) -> HealthStatus:
    """Check infrastructure health status."""
    return await stats.check_health()
