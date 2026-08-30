from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from kcms.moderation import repository
from kcms.shared.database import database

router = APIRouter(prefix="/api/v1")

ActionKind = Literal["LEAVE", "HIDE", "UNHIDE"]


class WorkListItem(BaseModel):
    comment_id: str
    text: str
    author_ref: str
    posted_at: datetime
    severity: str | None
    severity_confidence: float | None
    target: str | None
    target_confidence: float | None
    abstain: bool | None
    surfaced_reason: str | None
    rationale: str | None
    model_version: str | None
    latest_action: str | None
    latest_actor: str | None
    latest_action_at: datetime | None


class WorkList(BaseModel):
    items: list[WorkListItem]
    total: int


class ActionRequest(BaseModel):
    kind: ActionKind
    actor: str = "demo-client"


class HistoryEntry(BaseModel):
    kind: str
    actor: str
    occurred_at: datetime


def _require_database() -> None:
    if not database.connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )


@router.get("/comments", operation_id="listComments", response_model=WorkList)
async def list_comments() -> WorkList:
    _require_database()
    async with database.acquire() as connection:
        rows = await repository.fetch_work_list(connection)
    return WorkList(items=[WorkListItem(**row) for row in rows], total=len(rows))


@router.post(
    "/comments/{comment_id}/actions",
    operation_id="recordAction",
    response_model=list[HistoryEntry],
    status_code=status.HTTP_201_CREATED,
)
async def record_action(comment_id: str, body: ActionRequest) -> list[HistoryEntry]:
    """Records a moderation Action. Actions are append-only and reversible,
    and never become training labels."""
    _require_database()
    async with database.acquire() as connection:
        exists = await connection.fetchval(
            "SELECT 1 FROM comment_content WHERE comment_id = $1", comment_id
        )
        if not exists:
            raise HTTPException(status_code=404, detail="comment not found")
        await repository.record_action(connection, comment_id, body.kind, body.actor)
        history = await repository.fetch_history(connection, comment_id)
    return [HistoryEntry(**entry) for entry in history]
