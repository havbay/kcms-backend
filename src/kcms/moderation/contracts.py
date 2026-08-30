"""The classifier seam. Nothing downstream knows which implementation runs.

Swapping the trained Khmer model in later is one line:

    classifier = PatternMatcher()          # now
    classifier = KhmerModel("./model_v1")  # later

Routing, queue, thresholds and storage all talk to this Protocol, never to a model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Severity(StrEnum):
    SAFE = "SAFE"
    OFFENSIVE = "OFFENSIVE"
    HARMFUL = "HARMFUL"


class Target(StrEnum):
    PERSON = "PERSON"
    INSTITUTION = "INSTITUTION"
    NEITHER = "NEITHER"


class SurfacedReason(StrEnum):
    """Why a comment reached a human. Stored so anyone training on the
    resulting corrections can correct for selection bias."""

    TRIAGE = "triage"
    UNCERTAINTY = "uncertainty"
    INSTITUTION_SAMPLE = "institution_sample"
    NOVEL_LANGUAGE = "novel_language"
    CLEARED = "cleared"


@dataclass(frozen=True)
class CommentContext:
    comment_id: str
    text: str
    is_reply: bool = False
    parent_text: str | None = None
    post_text: str | None = None


@dataclass(frozen=True)
class Verdict:
    severity: Severity
    severity_confidence: float
    target: Target
    target_confidence: float
    abstain: bool
    surfaced_reason: SurfacedReason
    rationale: str | None
    model_version: str


class Classifier(Protocol):
    async def classify(self, items: Sequence[CommentContext]) -> Sequence[Verdict]: ...
