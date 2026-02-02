"""SSE endpoint for real-time system events."""

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from ..models import SystemEvent
from ..services.events import EventsService

router = APIRouter(prefix="/api", tags=["events"])


async def get_events_service() -> EventsService:
    """Dependency to get events service instance."""
    # This will be overridden in main.py to use shared service
    service = EventsService()
    await service.connect()
    try:
        yield service
    finally:
        await service.close()


@router.get("/events/stream")
async def stream_events(
    events: EventsService = Depends(get_events_service),
) -> EventSourceResponse:
    """
    Stream real-time system events via Server-Sent Events.

    Connect to this endpoint with EventSource to receive live updates
    about ledger transactions, job status changes, and agent activity.
    """
    return EventSourceResponse(events.stream_events())


@router.get("/events/recent", response_model=list[SystemEvent])
async def get_recent_events(
    limit: int = 20,
) -> list[SystemEvent]:
    """
    Get recent system events from database.

    This endpoint returns historical events, useful for initial page load.
    """
    # This endpoint will be implemented with pool access in main.py
    # For now, return empty list - will be overridden
    return []
