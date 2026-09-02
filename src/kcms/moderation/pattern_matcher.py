"""Disclosed, versioned Khmer pattern matching. The prototype classifier.

This is NOT Khmer NLP. Every accuracy claim about this component is a claim
about routing, not about language understanding. It exists so the whole
system can be built and proven before the trained model is ready.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from pathlib import Path

from kcms.moderation.contracts import (
    CommentContext,
    Severity,
    SurfacedReason,
    Target,
    Verdict,
)

MODEL_VERSION = "pattern-matching-v0.1"

# The starting set every workspace inherits. Anything a workspace adds lives in
# workspace_keyword and is passed in per classification run, so one client's
# vocabulary never reclassifies another client's queue.
KEYWORDS_JSON = Path(__file__).resolve().parents[3] / "data" / "keywords.json"

_KHMER_RUN = re.compile(r"[ក-៿]+")

# The only two severities a keyword can carry. There is deliberately no SAFE
# list: a word can surface a comment for review, it can never clear one, and a
# "safe" list only taught the novelty check that unfamiliar Khmer was familiar.
SEVERITY_KEYS = ("HARMFUL", "OFFENSIVE")
MARKER_KEYS = ("INSTITUTION", "PERSON")


class KeywordStore:
    """The platform's default keywords, reloaded when the file changes."""

    _cached_mtime: float = 0.0
    _severity: dict[str, list[str]] = {}
    _markers: dict[str, list[str]] = {}

    @classmethod
    def severity(cls, key: str) -> list[str]:
        cls._ensure_loaded()
        return cls._severity.get(key, [])

    @classmethod
    def markers(cls, key: str) -> list[str]:
        cls._ensure_loaded()
        return cls._markers.get(key, [])

    @classmethod
    def get_all_known(cls) -> set[str]:
        cls._ensure_loaded()
        known: set[str] = set()
        for words in cls._severity.values():
            known.update(words)
        for words in cls._markers.values():
            known.update(words)
        return known

    @classmethod
    def _ensure_loaded(cls) -> None:
        try:
            mtime = os.path.getmtime(KEYWORDS_JSON)
        except OSError:
            return  # File missing: keep whatever is already cached.
        if mtime > cls._cached_mtime:
            cls._load(mtime)

    @classmethod
    def _load(cls, mtime: float) -> None:
        try:
            with open(KEYWORDS_JSON, encoding="utf-8") as handle:
                data = json.load(handle)
            severity = data.get("severity") or {}
            markers = data.get("markers") or {}
            cls._severity = {
                key: [w for w in severity.get(key, []) if isinstance(w, str) and w.strip()]
                for key in SEVERITY_KEYS
            }
            cls._markers = {
                key: [w for w in markers.get(key, []) if isinstance(w, str) and w.strip()]
                for key in MARKER_KEYS
            }
            cls._cached_mtime = mtime
        except (OSError, ValueError):
            pass  # Fail safe if the file is mid-write or malformed.


def _hits(text: str, phrases: list[str]) -> list[str]:
    lowered = text.lower()
    return [p for p in phrases if p.lower() in lowered]


def _looks_novel(text: str, known_vocab: set[str]) -> bool:
    """Crude out-of-distribution signal: Khmer text the matcher recognises
    nothing in. A no-pattern-hit is NOT the same as 'safe' — new slang must
    reach a human instead of being silently cleared."""
    runs = _KHMER_RUN.findall(text)
    if not runs:
        return False
    return not any(known in text for known in known_vocab)


class PatternMatcher:
    """Implements the Classifier Protocol.

    Constructed per classification run with the workspace's own keywords, which
    are merged over the shipped defaults. Two workspaces classifying the same
    text can legitimately disagree, and that is the point.
    """

    model_version = MODEL_VERSION

    def __init__(self, workspace_keywords: Sequence[tuple[str, str]] = ()) -> None:
        """`workspace_keywords` is (keyword, severity) with severity in
        HARMFUL / OFFENSIVE. Anything else is ignored rather than trusted."""
        extra: dict[str, list[str]] = {key: [] for key in SEVERITY_KEYS}
        for keyword, severity in workspace_keywords:
            if keyword and keyword.strip() and severity in extra:
                extra[severity].append(keyword.strip())
        self._extra = extra

    def _severity_words(self, key: str) -> list[str]:
        return KeywordStore.severity(key) + self._extra.get(key, [])

    async def classify(self, items: Sequence[CommentContext]) -> Sequence[Verdict]:
        return [self._classify_one(item) for item in items]

    def _classify_one(self, item: CommentContext) -> Verdict:
        text = item.text
        harmful = _hits(text, self._severity_words("HARMFUL"))
        offensive = _hits(text, self._severity_words("OFFENSIVE"))
        institution = _hits(text, KeywordStore.markers("INSTITUTION"))
        person = _hits(text, KeywordStore.markers("PERSON"))

        # 1. Language we have never seen. Abstain rather than guess.
        if _looks_novel(text, self._all_known()):
            return Verdict(
                severity=Severity.SAFE,
                severity_confidence=0.0,
                target=Target.NEITHER,
                target_confidence=0.0,
                abstain=True,
                surfaced_reason=SurfacedReason.NOVEL_LANGUAGE,
                rationale="No known Khmer pattern matched; unfamiliar wording.",
                model_version=MODEL_VERSION,
            )

        # 2. Who is it aimed at?
        if institution and not person:
            target, target_confidence = Target.INSTITUTION, 0.82
        elif person and not institution:
            target, target_confidence = Target.PERSON, 0.80
        elif person and institution:
            # Ambiguous. Abstaining is correct: guessing PERSON here is how a
            # legitimate complaint gets suppressed.
            target, target_confidence = Target.PERSON, 0.45
        else:
            target, target_confidence = Target.NEITHER, 0.55

        # 3. How severe? Only two lists are searched, so this is a two-way
        # decision plus "nothing matched".
        if harmful:
            severity, severity_confidence = Severity.HARMFUL, 0.85
            rationale = f"Harmful keyword: {', '.join(harmful)}"
        elif offensive:
            severity, severity_confidence = Severity.OFFENSIVE, 0.70
            rationale = f"Offensive keyword: {', '.join(offensive)}"
        else:
            severity, severity_confidence = Severity.SAFE, 0.66
            rationale = "No risk pattern matched."

        abstain = min(severity_confidence, target_confidence) < 0.5

        return Verdict(
            severity=severity,
            severity_confidence=severity_confidence,
            target=target,
            target_confidence=target_confidence,
            abstain=abstain,
            surfaced_reason=self._route(severity, target, abstain),
            rationale=rationale,
            model_version=MODEL_VERSION,
        )

    def _all_known(self) -> set[str]:
        known = KeywordStore.get_all_known()
        for words in self._extra.values():
            known.update(words)
        return known

    @staticmethod
    def _route(severity: Severity, target: Target, abstain: bool) -> SurfacedReason:
        """Routing rules. Institution-directed criticism is never treated as
        harm to be removed, however hostile."""
        if abstain:
            return SurfacedReason.UNCERTAINTY
        if target is Target.INSTITUTION and severity is not Severity.SAFE:
            return SurfacedReason.INSTITUTION_SAMPLE
        if severity is Severity.HARMFUL:
            return SurfacedReason.TRIAGE
        if severity is Severity.OFFENSIVE:
            return SurfacedReason.TRIAGE
        return SurfacedReason.CLEARED


def auto_removable(verdict: Verdict) -> bool:
    """Whether this verdict is removed from the Page without asking a human.

    Harmful and confident only. Two carve-outs are deliberate and must stay:
    an abstention is not a finding, and criticism aimed at an institution is
    never auto-removed however hostile it reads — suppressing that is the
    failure this product exists to avoid. Everything else that is surfaced
    goes to a person.
    """
    return (
        verdict.severity is Severity.HARMFUL
        and not verdict.abstain
        and verdict.target is not Target.INSTITUTION
    )
