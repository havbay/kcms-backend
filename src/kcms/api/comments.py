from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from kcms.api.auth import current_user
from kcms.auth import repository as auth_repository
from kcms.integrations import repository as integrations_repository
from kcms.integrations.credentials import CredentialCipher, get_optional_credential_cipher
from kcms.integrations.facebook import MetaClient, get_optional_meta_client
from kcms.moderation import repository
from kcms.shared.database import database

router = APIRouter(prefix="/api/v1")

# A reviewer has two choices: take the comment down, or let it stand.
ActionKind = Literal["LEAVE", "HIDE", "UNHIDE", "DELETE"]
SeverityLabel = Literal["SAFE", "OFFENSIVE", "HARMFUL"]
TargetLabel = Literal["PERSON", "INSTITUTION", "NEITHER"]


class WorkListItem(BaseModel):
    comment_id: str
    text: str
    author_ref: str
    posted_at: datetime
    page_id: str
    # The Page's name, so a row says which Page it came from. Null when the
    # connection has since been removed; the comment and its record remain.
    page_name: str | None
    # Meta withholds `from` for commenters who have not authorized the app,
    # so this is usually absent on a real Page.
    author_id: str | None
    post_text: str | None
    parent_text: str | None
    is_reply: bool
    post_kind: str
    post_permalink: str | None
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
    latest_action_on_facebook: bool | None
    corrected_severity: str | None
    corrected_target: str | None
    corrected_by: str | None
    corrected_at: datetime | None
    # Set while this comment is quarantined: hidden now, deleted once this
    # time passes with no human action. Cleared the moment a human acts.
    pending_delete_at: datetime | None


class WorkList(BaseModel):
    items: list[WorkListItem]
    total: int
    limit: int
    offset: int


class ReasonCount(BaseModel):
    surfaced_reason: str
    count: int


class Summary(BaseModel):
    """Counts computed across the whole workspace, not from one page."""

    processed: int
    need_review: int
    reviewed: int
    pending: int
    left_visible: int
    deleted: int
    reasons: list[ReasonCount]


class SampleRemoval(BaseModel):
    removed: int


class ActionRequest(BaseModel):
    kind: ActionKind


class HistoryEntry(BaseModel):
    kind: str
    actor: str
    occurred_at: datetime


class CorrectionRequest(BaseModel):
    severity: SeverityLabel
    target: TargetLabel
    note: str | None = None


class CorrectionResponse(BaseModel):
    severity: str
    target: str
    actor: str
    note: str | None
    disagrees_with_model: str
    occurred_at: datetime


def _require_database() -> None:
    if not database.connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )


@router.get("/comments", operation_id="listComments", response_model=WorkList)
async def list_comments(
    user: Annotated[dict[str, Any], Depends(current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
    query: Annotated[str | None, Query(max_length=200)] = None,
    severity: Annotated[SeverityLabel | None, Query()] = None,
    target: Annotated[TargetLabel | None, Query()] = None,
    # "cleared" is deliberately absent: the work list excludes cleared rows
    # before any filter applies, so offering it advertised a value that could
    # never return a row.
    surfaced_reason: Annotated[
        Literal["triage", "institution_sample", "novel_language", "uncertainty"] | None,
        Query(),
    ] = None,
    review_status: Annotated[Literal["PENDING", "ACTIONED"] | None, Query()] = None,
    sort: Annotated[Literal["PRIORITY", "NEWEST", "OLDEST"], Query()] = "PRIORITY",
) -> WorkList:
    _require_database()
    async with database.acquire() as connection:
        rows, total = await repository.fetch_work_list(
            connection,
            await _workspace_id(connection, user),
            limit,
            offset,
            query=query,
            severity=severity,
            target=target,
            surfaced_reason=surfaced_reason,
            review_status=review_status,
            sort=sort,
        )
    return WorkList(
        items=[WorkListItem(**row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/comments/summary", operation_id="getWorkspaceSummary", response_model=Summary)
async def get_summary(user: Annotated[dict[str, Any], Depends(current_user)]) -> Summary:
    _require_database()
    async with database.acquire() as connection:
        summary = await repository.summarise_workspace(
            connection, await _workspace_id(connection, user)
        )
    return Summary(**summary)


@router.post(
    "/comments/{comment_id}/actions",
    operation_id="recordAction",
    response_model=list[HistoryEntry],
    status_code=status.HTTP_201_CREATED,
)
async def record_action(
    comment_id: str,
    body: ActionRequest,
    user: Annotated[dict[str, Any], Depends(current_user)],
    meta: Annotated[MetaClient | None, Depends(get_optional_meta_client)],
    cipher: Annotated[CredentialCipher | None, Depends(get_optional_credential_cipher)],
) -> list[HistoryEntry]:
    """Records a moderation Action. Actions are append-only and reversible,
    and never become training labels.

    When the comment came from a connected Facebook Page, HIDE, UNHIDE and
    DELETE are applied on Facebook as well. The Action row and the Facebook
    state are written together: if Facebook refuses, the row is rolled back,
    because an Action records what actually happened to the comment.

    HIDE is reversible through UNHIDE and is the option to reach for when the
    classifier's judgement is uncertain. DELETE cannot be undone on Facebook.
    LEAVE changes nothing there and exists so that a decision to allow a
    comment is still recorded, which is a different fact from nobody having
    looked.

    Any action here also cancels a pending auto-delete on this comment
    (quarantine.sweep_once would otherwise delete it once the delay expires):
    a human has now decided, so the countdown no longer applies.
    """
    _require_database()
    async with database.acquire() as connection:
        workspace = await auth_repository.workspace_for_user(connection, user["id"])
        if not workspace:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace for this account")
        if auth_repository.trial_expired(workspace):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                (
                    "This free trial has expired. Upgrade the workspace to continue using "
                    "Facebook actions."
                ),
            )
        workspace_id = await _workspace_id(connection, user)
        # 404 rather than 403: a different status would confirm the comment
        # exists in someone else's workspace.
        if not await repository.comment_belongs_to(connection, comment_id, workspace_id):
            raise HTTPException(status_code=404, detail="comment not found")

        mirror = await _provider_credential(connection, comment_id, workspace_id, body.kind)

        async with connection.transaction():
            await repository.record_action(
                connection,
                comment_id,
                body.kind,
                user["display_name"],
                provider_applied=bool(mirror),
            )
            # A fresh human decision always overrides a pending auto-delete —
            # whichever way they decided. Rolled back with everything else in
            # this transaction if the Facebook mirror call below fails.
            await repository.cancel_scheduled_deletion(connection, comment_id)
            if mirror:
                if meta is None or cipher is None:
                    raise HTTPException(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        "this comment is on a connected Facebook Page, but Facebook "
                        "is not configured on this deployment",
                    )
                token = cipher.open(mirror["credential_ciphertext"])
                try:
                    if body.kind == "DELETE":
                        await meta.delete_comment(comment_id, token)
                    else:
                        await meta.set_comment_hidden(
                            comment_id, token, hidden=body.kind == "HIDE"
                        )
                except ValueError as exc:
                    raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

        history = await repository.fetch_history(connection, comment_id)
    return [HistoryEntry(**entry) for entry in history]


async def _provider_credential(
    connection, comment_id: str, workspace_id: str, kind: str
) -> dict[str, Any] | None:
    """The stored Page credential when this action must reach Facebook.

    Returns None for LEAVE, which changes nothing on Facebook, and for seeded
    sample comments, whose page_id is the sandbox Page rather than a connected
    one.
    """
    if kind == "LEAVE":
        return None
    page_id = await repository.comment_page_id(connection, comment_id)
    if not page_id:
        return None
    return await integrations_repository.credential_for_page(connection, workspace_id, page_id)


@router.post(
    "/comments/{comment_id}/corrections",
    operation_id="recordCorrection",
    response_model=CorrectionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_correction(
    comment_id: str,
    body: CorrectionRequest,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> CorrectionResponse:
    """Records what a human asserts the labels should be.

    A Correction is not an Action. Submitting one does not hide, unhide or
    otherwise change what happens to the comment.
    """
    _require_database()
    async with database.acquire() as connection:
        workspace_id = await _workspace_id(connection, user)
        if not await repository.comment_belongs_to(connection, comment_id, workspace_id):
            raise HTTPException(status_code=404, detail="comment not found")
        created = await repository.record_correction(
            connection, comment_id, body.severity, body.target, user["display_name"], body.note
        )
    if created is None:
        raise HTTPException(status_code=500, detail="correction was not stored")
    return CorrectionResponse(**created)


async def _workspace_id(connection, user: dict[str, Any]) -> str:
    workspace = await auth_repository.workspace_for_user(connection, user["id"])
    if not workspace:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace for this account")
    return workspace["id"]


@router.delete(
    "/comments/samples", operation_id="removeSampleComments", response_model=SampleRemoval
)
async def remove_sample_comments(
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> SampleRemoval:
    """Clear the seeded sample comments from this workspace.

    Only the samples are removed: the delete is scoped by the sample Page id,
    so comments imported from a connected Facebook Page are never touched.
    Emptying a shared workspace affects everyone in it, so it is an owner
    action rather than something any member can do.
    """
    _require_database()
    async with database.acquire() as connection:
        workspace = await auth_repository.workspace_for_user(connection, user["id"])
        if not workspace:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace for this account")
        if workspace["role"] != "owner":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "only an owner can remove the sample comments"
            )
        removed = await repository.delete_sample_comments(connection, workspace["id"])
    return SampleRemoval(removed=removed)
