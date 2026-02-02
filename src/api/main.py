"""FastAPI application for Agent Economy dashboard."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, AsyncGenerator

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from .models import (
    TokenStats,
    AgentStats,
    JobStats,
    LeaderboardEntry,
    JobItem,
    HealthStatus,
    QuickStats,
    SystemEvent,
)
from .services.stats import StatsService
from .services.events import EventsService

# Import board client for message streaming
from src.board.client import BoardClient, MessageType


# Configuration from environment
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "agent_economy")
DB_USER = os.getenv("DB_USER", "agent_economy")
DB_PASSWORD = os.getenv("DB_PASSWORD", "agent_economy_dev")
NATS_URL = os.getenv("NATS_URL", "nats://localhost:4222")

# Shared services
stats_service: Optional[StatsService] = None
events_service: Optional[EventsService] = None
board_client: Optional[BoardClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown."""
    global stats_service, events_service, board_client

    # Startup
    stats_service = StatsService(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
    await stats_service.connect()

    events_service = EventsService(nats_url=NATS_URL)
    await events_service.connect()

    board_client = BoardClient(nats_url=NATS_URL)
    await board_client.connect()

    yield

    # Shutdown
    if stats_service:
        await stats_service.close()
    if events_service:
        await events_service.close()
    if board_client:
        await board_client.close()


app = FastAPI(
    title="Agent Economy API",
    description="Backend API for the Agent Economy dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for local development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Dashboard static files
# __file__ is src/api/main.py, so .parent.parent.parent gets us to agent_economy/
UI_PATH = Path(__file__).parent.parent.parent / "ui" / "design"


@app.get("/")
async def root():
    """Redirect to dashboard."""
    return FileResponse(UI_PATH / "project_dashboard.html")


@app.get("/project_dashboard.html")
async def dashboard():
    """Serve the project dashboard HTML."""
    return FileResponse(UI_PATH / "project_dashboard.html")


# API Routes

@app.get("/api/stats/tokens", response_model=TokenStats, tags=["stats"])
async def get_token_stats() -> TokenStats:
    """Get token economy statistics."""
    if not stats_service:
        raise RuntimeError("Service not initialized")
    return await stats_service.get_token_stats()


@app.get("/api/stats/agents", response_model=AgentStats, tags=["stats"])
async def get_agent_stats() -> AgentStats:
    """Get agent statistics by status."""
    if not stats_service:
        raise RuntimeError("Service not initialized")
    return await stats_service.get_agent_stats()


@app.get("/api/stats/jobs", response_model=JobStats, tags=["stats"])
async def get_job_stats() -> JobStats:
    """Get job statistics by status."""
    if not stats_service:
        raise RuntimeError("Service not initialized")
    return await stats_service.get_job_stats()


@app.get("/api/stats/quick", response_model=QuickStats, tags=["stats"])
async def get_quick_stats() -> QuickStats:
    """Get quick stats for dashboard footer."""
    if not stats_service:
        raise RuntimeError("Service not initialized")
    return await stats_service.get_quick_stats()


@app.get("/api/agents/leaderboard", response_model=list[LeaderboardEntry], tags=["agents"])
async def get_leaderboard(
    limit: int = Query(default=10, le=50),
) -> list[LeaderboardEntry]:
    """Get top agents by balance."""
    if not stats_service:
        raise RuntimeError("Service not initialized")
    return await stats_service.get_leaderboard(limit=limit)


@app.get("/api/jobs", response_model=list[JobItem], tags=["jobs"])
async def get_jobs(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=20, le=100),
) -> list[JobItem]:
    """Get list of jobs with optional status filter."""
    if not stats_service:
        raise RuntimeError("Service not initialized")
    return await stats_service.get_jobs_list(status=status, limit=limit)


@app.get("/api/health", response_model=HealthStatus, tags=["health"])
async def get_health() -> HealthStatus:
    """Check infrastructure health status."""
    if not stats_service:
        return HealthStatus(postgresql=False, nats=False, overall=False)

    health = await stats_service.check_health()
    # Update NATS status from events service
    if events_service:
        health.nats = events_service.is_connected
        health.overall = health.postgresql and health.nats
    return health


@app.get("/api/events/stream", tags=["events"])
async def stream_events() -> EventSourceResponse:
    """
    Stream real-time system events via Server-Sent Events.

    Connect to this endpoint with EventSource to receive live updates.
    """
    if not events_service:
        async def error_stream():
            yield "data: {\"error\": \"Events service not initialized\"}\n\n"
        return EventSourceResponse(error_stream())
    return EventSourceResponse(events_service.stream_events())


@app.get("/api/events/recent", response_model=list[SystemEvent], tags=["events"])
async def get_recent_events(
    limit: int = Query(default=20, le=50),
) -> list[SystemEvent]:
    """Get recent system events from database."""
    if not events_service or not stats_service or not stats_service._pool:
        return []
    return await events_service.get_recent_events(stats_service._pool, limit=limit)


# Board message endpoints

@app.get("/api/board/messages", tags=["board"])
async def get_board_messages(
    limit: int = Query(default=20, le=100),
):
    """Get recent messages from the board."""
    if not board_client:
        return []

    all_messages = []
    for msg_type in MessageType:
        try:
            messages = await board_client.read_messages(msg_type, limit=limit)
            for msg in messages:
                all_messages.append({
                    "msg_id": msg.msg_id,
                    "type": msg.type.value,
                    "agent_id": msg.agent_id,
                    "content": msg.content,
                    "timestamp": msg.timestamp,
                    "thread_id": msg.thread_id,
                    "tags": msg.tags,
                })
        except Exception:
            pass

    # Sort by timestamp descending
    all_messages.sort(key=lambda m: m["timestamp"], reverse=True)
    return all_messages[:limit]


@app.get("/api/board/stream", tags=["board"])
async def stream_board_messages() -> EventSourceResponse:
    """Stream real-time board messages via Server-Sent Events."""

    async def generate() -> AsyncGenerator[str, None]:
        if not board_client or not board_client._nc:
            yield f"data: {json.dumps({'error': 'Board not connected'})}\n\n"
            return

        try:
            # Subscribe to all board subjects
            sub = await board_client._nc.subscribe("board.>")

            yield f"data: {json.dumps({'type': 'meta', 'agent_id': 'system', 'content': {{'message': 'Connected to board'}}, 'timestamp': ''})}\n\n"

            while True:
                try:
                    msg = await asyncio.wait_for(sub.next_msg(), timeout=30.0)
                    try:
                        data = json.loads(msg.data.decode())
                        yield f"data: {json.dumps(data)}\n\n"
                    except json.JSONDecodeError:
                        pass
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield ": keepalive\n\n"
                except Exception as e:
                    yield f"data: {json.dumps({'error': str(e)[:100]})}\n\n"
                    break
        except Exception as e:
            yield f"data: {json.dumps({'error': f'Subscribe failed: {str(e)[:100]}'})}\n\n"

    return EventSourceResponse(generate())


# Run with: uvicorn src.api.main:app --reload --port 8000
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
