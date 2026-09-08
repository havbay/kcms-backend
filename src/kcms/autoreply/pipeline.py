"""Four-gate reply decision pipeline."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from kcms.autoreply.rules import Channel, ReplyRule, first_matching_rule


@dataclass(frozen=True)
class InboundMessage:
    text: str
    channel: Channel
    severity: Literal["SAFE", "OFFENSIVE", "HARMFUL"] = "SAFE"
    abstained: bool = False
    sandbox: bool = False
    from_page: bool = False
    already_answered: bool = False


@dataclass(frozen=True)
class ReplyDecision:
    kind: Literal["OFF", "INELIGIBLE", "EMPTY", "UNSAFE", "NO_MATCH", "REPLY"]
    rule_id: str | None = None
    reply_body: str | None = None
    reason: str | None = None


def decide_reply(
    message: InboundMessage,
    rules: Sequence[ReplyRule],
    *,
    enabled: bool = True,
) -> ReplyDecision:
    """Return a side-effect-free decision; this function never sends a reply."""
    if not enabled:
        return ReplyDecision("OFF", reason="automated replies are disabled")
    if message.sandbox or message.from_page or message.already_answered:
        return ReplyDecision("INELIGIBLE", reason="message is not eligible")
    if not message.text.strip():
        return ReplyDecision("EMPTY", reason="message has no text")
    if message.severity != "SAFE" or message.abstained:
        return ReplyDecision("UNSAFE", reason="risk gate did not clear the message")
    matched = first_matching_rule(message.text, rules, message.channel)
    if matched is None:
        return ReplyDecision("NO_MATCH", reason="no enabled rule matched")
    return ReplyDecision("REPLY", rule_id=matched.id, reply_body=matched.reply_body)
