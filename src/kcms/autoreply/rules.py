"""Validation and matching for workspace-owned reply rules."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from kcms.autoreply.khmer import normalize_text, written_unit_count

Channel = Literal["comments", "messages"]


@dataclass(frozen=True)
class ReplyRule:
    id: str
    name: str
    keywords: tuple[str, ...]
    reply_body: str
    on_comments: bool
    on_messages: bool
    position: int
    enabled: bool


@dataclass(frozen=True)
class ValidatedRule:
    rule: ReplyRule
    warnings: tuple[str, ...]


class RuleValidationError(ValueError):
    """A rule cannot be saved because it would be misleading or unusable."""


def validate_rule(
    *,
    name: str,
    keywords: Sequence[str],
    reply_body: str,
    on_comments: bool,
    on_messages: bool,
    rule_id: str = "pending",
    position: int = 0,
    enabled: bool = False,
) -> ValidatedRule:
    clean_name = name.strip()
    clean_reply = reply_body.strip()
    if not clean_name:
        raise RuleValidationError("name is required")
    if len(clean_name) > 120:
        raise RuleValidationError("name must be at most 120 characters")
    if not clean_reply:
        raise RuleValidationError("reply_body is required")
    if len(clean_reply) > 1000:
        raise RuleValidationError("reply_body must be at most 1000 characters")
    if not keywords:
        raise RuleValidationError("at least one keyword is required")
    if len(keywords) > 50:
        raise RuleValidationError("a rule may contain at most 50 keywords")
    if not on_comments and not on_messages:
        raise RuleValidationError("enable comments or messages")

    clean_keywords: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for raw_keyword in keywords:
        keyword = raw_keyword.strip()
        if not keyword:
            raise RuleValidationError("keywords cannot be empty")
        if len(keyword) > 100:
            raise RuleValidationError("each keyword must be at most 100 characters")
        normalized = normalize_text(keyword)
        if not normalized:
            raise RuleValidationError("keywords cannot contain only invisible characters")
        if normalized in seen:
            raise RuleValidationError("keywords must be unique after normalization")
        seen.add(normalized)
        if any("ក" <= char <= "៿" for char in normalized):
            if len(normalized) < 4:
                raise RuleValidationError("Khmer keywords must contain at least 4 codepoints")
        elif len(normalized) < 3:
            raise RuleValidationError("Latin keywords must contain at least 3 characters")
        if written_unit_count(normalized) < 3:
            warnings.append(f"keyword '{keyword}' contains fewer than 3 written units")
        clean_keywords.append(keyword)

    return ValidatedRule(
        rule=ReplyRule(
            id=rule_id,
            name=clean_name,
            keywords=tuple(clean_keywords),
            reply_body=clean_reply,
            on_comments=on_comments,
            on_messages=on_messages,
            position=position,
            enabled=enabled,
        ),
        warnings=tuple(warnings),
    )


def first_matching_rule(
    text: str, rules: Sequence[ReplyRule], channel: Channel
) -> ReplyRule | None:
    normalized_text = normalize_text(text)
    if not normalized_text:
        return None
    for rule in sorted(rules, key=lambda item: (item.position, item.id)):
        if not rule.enabled or (channel == "comments" and not rule.on_comments) or (
            channel == "messages" and not rule.on_messages
        ):
            continue
        if any(normalize_text(keyword) in normalized_text for keyword in rule.keywords):
            return rule
    return None
