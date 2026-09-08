"""Client Workspace Automated Replies configuration and simulation."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from kcms.api.auth import current_user
from kcms.auth import repository as auth_repository
from kcms.autoreply.pipeline import InboundMessage, ReplyDecision, decide_reply
from kcms.autoreply.repository import (
    create_rule,
    delete_rule,
    get_rule,
    list_events,
    list_rules,
    reorder_rules,
    set_settings,
    update_rule,
)
from kcms.autoreply.rules import ReplyRule, RuleValidationError, validate_rule
from kcms.moderation.contracts import CommentContext
from kcms.moderation.repository import _matcher_for
from kcms.shared.database import database

router = APIRouter(prefix="/api/v1/auto-replies")
Channel = Literal["comments", "messages"]


class AutoReplySettings(BaseModel):
    enabled: bool
    your_role: str


class AutoReplySettingsPatch(BaseModel):
    enabled: bool | None = None


class AutoReplyRuleInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    keywords: list[str] = Field(min_length=1, max_length=50)
    reply_body: str = Field(min_length=1, max_length=1000)
    on_comments: bool = True
    on_messages: bool = False
    enabled: bool = False


class AutoReplyRuleResponse(AutoReplyRuleInput):
    id: str
    position: int
    warnings: list[str]


class AutoReplyRulePatch(AutoReplyRuleInput):
    pass


class ReorderRules(BaseModel):
    rule_ids: list[str] = Field(min_length=0, max_length=50)


class SimulationInput(BaseModel):
    text: str = Field(max_length=5000)
    channel: Channel = "comments"


class SimulationResponse(BaseModel):
    kind: str
    rule_id: str | None
    reply_body: str | None
    reason: str | None


class AutoReplyEvent(BaseModel):
    id: int
    rule_id: str | None
    provider_event_id: str
    channel: str
    decision: str
    reason: str
    reply_body: str | None
    provider_applied: bool
    occurred_at: datetime


def _require_database() -> None:
    if not database.connected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")


async def _workspace(connection, user: dict[str, Any]) -> dict[str, Any]:
    found = await auth_repository.workspace_for_user(connection, user["id"])
    if not found:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no workspace for this account")
    return found


def _require_owner(workspace: dict[str, Any]) -> None:
    if workspace["role"] != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only an owner can manage automated replies")


def _response(rule: ReplyRule) -> AutoReplyRuleResponse:
    validated = validate_rule(
        name=rule.name, keywords=rule.keywords, reply_body=rule.reply_body,
        on_comments=rule.on_comments, on_messages=rule.on_messages,
        rule_id=rule.id, position=rule.position, enabled=rule.enabled,
    )
    return AutoReplyRuleResponse(
        id=validated.rule.id,
        name=validated.rule.name,
        keywords=list(validated.rule.keywords),
        reply_body=validated.rule.reply_body,
        on_comments=validated.rule.on_comments,
        on_messages=validated.rule.on_messages,
        enabled=validated.rule.enabled,
        position=validated.rule.position,
        warnings=list(validated.warnings),
    )


def _validated(body: AutoReplyRuleInput, *, rule_id: str, position: int) -> ReplyRule:
    try:
        return validate_rule(
            name=body.name, keywords=body.keywords, reply_body=body.reply_body,
            on_comments=body.on_comments, on_messages=body.on_messages,
            rule_id=rule_id, position=position, enabled=body.enabled,
        ).rule
    except RuleValidationError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error


@router.get("/settings", operation_id="getAutoReplySettings", response_model=AutoReplySettings)
async def get_auto_reply_settings(
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> AutoReplySettings:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
    return AutoReplySettings(
        enabled=workspace["auto_reply_enabled"],
        your_role=workspace["role"],
    )


@router.patch("/settings", operation_id="patchAutoReplySettings", response_model=AutoReplySettings)
async def patch_auto_reply_settings(
    body: AutoReplySettingsPatch,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> AutoReplySettings:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        _require_owner(workspace)
        await set_settings(connection, workspace["id"], enabled=body.enabled)
    return AutoReplySettings(
        enabled=body.enabled if body.enabled is not None else workspace["auto_reply_enabled"],
        your_role=workspace["role"],
    )


@router.get("/rules", operation_id="listAutoReplyRules", response_model=list[AutoReplyRuleResponse])
async def get_auto_reply_rules(
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> list[AutoReplyRuleResponse]:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        rules = await list_rules(connection, workspace["id"])
    return [_response(rule) for rule in rules]


@router.post(
    "/rules",
    operation_id="createAutoReplyRule",
    response_model=AutoReplyRuleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_auto_reply_rule(
    body: AutoReplyRuleInput,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> AutoReplyRuleResponse:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        _require_owner(workspace)
        position = await connection.fetchval(
            "SELECT COALESCE(MAX(position) + 1, 0) FROM auto_reply_rule WHERE workspace_id = $1",
            workspace["id"],
        )
        rule = _validated(body, rule_id=uuid4().hex, position=position)
        created = await create_rule(connection, workspace["id"], rule, user["id"])
    return _response(created)


@router.patch(
    "/rules/{rule_id}",
    operation_id="updateAutoReplyRule",
    response_model=AutoReplyRuleResponse,
)
async def patch_auto_reply_rule(
    rule_id: str,
    body: AutoReplyRulePatch,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> AutoReplyRuleResponse:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        _require_owner(workspace)
        old = await get_rule(connection, workspace["id"], rule_id)
        if old is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
        updated = await update_rule(
            connection, workspace["id"], _validated(body, rule_id=rule_id, position=old.position)
        )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    return _response(updated)


@router.delete(
    "/rules/{rule_id}",
    operation_id="deleteAutoReplyRule",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_auto_reply_rule(
    rule_id: str,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> None:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        _require_owner(workspace)
        if not await delete_rule(connection, workspace["id"], rule_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")


@router.post(
    "/rules/reorder",
    operation_id="reorderAutoReplyRules",
    response_model=list[AutoReplyRuleResponse],
)
async def post_reorder_auto_reply_rules(
    body: ReorderRules,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> list[AutoReplyRuleResponse]:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        _require_owner(workspace)
        if not await reorder_rules(connection, workspace["id"], body.rule_ids):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "rule order does not match this workspace",
            )
        rules = await list_rules(connection, workspace["id"])
    return [_response(rule) for rule in rules]


@router.post("/simulate", operation_id="simulateAutoReply", response_model=SimulationResponse)
async def simulate_auto_reply(
    body: SimulationInput,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> SimulationResponse:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        rules = await list_rules(connection, workspace["id"])
        matcher = await _matcher_for(
            connection,
            workspace["id"],
            allowlist=workspace["keyword_allowlist"],
            blocklist=workspace["keyword_blocklist"],
        )
        verdict = (await matcher.classify([
            CommentContext(comment_id="simulation", text=body.text)
        ]))[0]
    decision: ReplyDecision = decide_reply(
        InboundMessage(
            text=body.text,
            channel=body.channel,
            severity=verdict.severity.value,
            abstained=verdict.abstain,
        ),
        rules,
        enabled=workspace["auto_reply_enabled"],
    )
    return SimulationResponse(**decision.__dict__)


@router.get("/events", operation_id="listAutoReplyEvents", response_model=list[AutoReplyEvent])
async def get_auto_reply_events(
    user: Annotated[dict[str, Any], Depends(current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[AutoReplyEvent]:
    _require_database()
    async with database.acquire() as connection:
        workspace = await _workspace(connection, user)
        events = await list_events(connection, workspace["id"], limit)
    return [AutoReplyEvent(**event) for event in events]
