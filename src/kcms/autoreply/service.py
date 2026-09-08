"""Provider-neutral processing for newly ingested automated-reply comments."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from kcms.autoreply.pipeline import InboundMessage, ReplyDecision, decide_reply
from kcms.autoreply.repository import (
    event_exists,
    list_rules,
    mark_event_failed,
    mark_event_replied,
    record_event,
)
from kcms.autoreply.rules import ReplyRule
from kcms.integrations.contracts import ProviderComment
from kcms.integrations.facebook import MetaClient
from kcms.moderation.contracts import Verdict

logger = logging.getLogger("kcms.autoreply")


@dataclass(frozen=True)
class ReplyProcessingResult:
    replied: int = 0
    skipped: int = 0


async def process_new_comments(
    connection: asyncpg.Connection,
    *,
    workspace_id: str,
    enabled: bool,
    comments: list[tuple[ProviderComment, Verdict]],
    token: str,
    meta: MetaClient,
) -> ReplyProcessingResult:
    """Decide and, when explicitly live, reply to newly ingested comments.

    The comment list is deliberately limited to the current ingestion batch.
    This prevents enabling the feature from replying to an old queue. The
    event uniqueness constraint remains the final duplicate-send guard.
    """
    rules: list[ReplyRule] = await list_rules(connection, workspace_id)
    replied = skipped = 0

    for comment, verdict in comments:
        provider_event_id = comment.comment_id
        if await event_exists(connection, workspace_id, "comments", provider_event_id):
            continue

        decision: ReplyDecision = decide_reply(
            InboundMessage(
                text=comment.text,
                channel="comments",
                severity=verdict.severity.value,
                abstained=verdict.abstain,
            ),
            rules,
            enabled=enabled,
        )

        if decision.kind != "REPLY":
            await record_event(
                connection,
                workspace_id,
                rule_id=decision.rule_id,
                provider_event_id=provider_event_id,
                channel="comments",
                decision="skipped",
                reason=decision.reason or decision.kind.lower(),
                reply_body=decision.reply_body,
            )
            skipped += 1
            continue

        inserted = await record_event(
            connection,
            workspace_id,
            rule_id=decision.rule_id,
            provider_event_id=provider_event_id,
            channel="comments",
            decision="would_reply",
            reason="rule matched; sending to Facebook",
            reply_body=decision.reply_body,
        )
        if not inserted:
            continue

        try:
            await meta.reply_to_comment(provider_event_id, token, decision.reply_body or "")
        except Exception as exc:  # provider failures must degrade to silence
            reason = f"Facebook reply failed: {str(exc)[:200]}"
            await mark_event_failed(connection, workspace_id, "comments", provider_event_id, reason)
            logger.warning(
                "Facebook auto-reply failed for provider event %s: %s",
                provider_event_id,
                reason,
            )
            skipped += 1
        else:
            await mark_event_replied(connection, workspace_id, "comments", provider_event_id)
            replied += 1

    return ReplyProcessingResult(replied=replied, skipped=skipped)
