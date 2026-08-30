"""Disclosed, versioned Khmer pattern matching. The prototype classifier.

This is NOT Khmer NLP. Every accuracy claim about this component is a claim
about routing, not about language understanding. It exists so the whole
system can be built and proven before the trained model is ready.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from kcms.moderation.contracts import (
    CommentContext,
    Severity,
    SurfacedReason,
    Target,
    Verdict,
)

MODEL_VERSION = "pattern-matching-v0.1"

# --- Scam / fraud signals -------------------------------------------------
SCAM = [
    "លេខកូដ", "កូដសម្ងាត់", "otp", "ផ្ញើលុយ", "ផ្ទេរលុយ", "ឈ្នះរង្វាន់",
    "ចុចលីង", "ចុច link", "ទទួលរង្វាន់", "វិនិយោគ", "ចំណេញ100",
]

# --- Abusive vocabulary ---------------------------------------------------
ABUSE_STRONG = ["ឆ្កួត", "សត្វ", "ចង្រៃ", "អាក្រក់ណាស់", "ងាប់"]
ABUSE_MILD = ["ល្ងង់", "គ្មានខួរ", "អន់ណាស់", "ឆោត"]

# --- Who is being addressed ----------------------------------------------
INSTITUTION_MARKERS = [
    "ក្រុមហ៊ុន", "សេវាកម្ម", "ធនាគារ", "ក្រសួង", "ហាង", "ក្រុមការងារ",
    "អង្គភាព", "រដ្ឋបាល", "ការិយាល័យ",
]
# Khmer is unspaced, so short markers collide inside longer words.
# "វា" (it) lives inside "សេវា" (service) — matching it bare mislabels
# every service complaint as person-directed. Only longer forms are safe.
PERSON_MARKERS = ["អ្នកនេះ", "ឱ្យវា", "ឲ្យវា", "នាងនេះ", "គាត់នេះ", "ប្អូននេះ", "បងនេះ"]

# --- Ordinary complaint vocabulary (protects legitimate criticism) --------
COMPLAINT = ["យឺត", "ខកចិត្ត", "មិនល្អ", "រង់ចាំ", "គុណភាព", "តម្លៃថ្លៃ", "សេវាអន់"]

# Ordinary, unremarkable Khmer. Present so everyday traffic clears instead of
# flooding the review queue as "unfamiliar". A small vocabulary makes almost
# all real Khmer look novel, which is safe but unusable.
SAFE_COMMON = [
    "សួស្តី", "អរគុណ", "តម្លៃ", "ប៉ុន្មាន", "សុំ", "បាទ", "ចាស",
    "ល្អ", "ជួយ", "ទីតាំង", "ដឹកជញ្ជូន", "ម៉ោង", "ថ្ងៃ",
]

# Everything the matcher claims to recognise. Anything outside this is novel.
KNOWN_VOCABULARY = set(
    SCAM + ABUSE_STRONG + ABUSE_MILD + INSTITUTION_MARKERS + PERSON_MARKERS
    + COMPLAINT + SAFE_COMMON
)

_KHMER_RUN = re.compile(r"[ក-៿]+")


def _hits(text: str, phrases: list[str]) -> list[str]:
    lowered = text.lower()
    return [p for p in phrases if p.lower() in lowered]


def _looks_novel(text: str) -> bool:
    """Crude out-of-distribution signal: Khmer text the matcher recognises
    nothing in. A no-pattern-hit is NOT the same as 'safe' — new slang must
    reach a human instead of being silently cleared."""
    runs = _KHMER_RUN.findall(text)
    if not runs:
        return False
    return not any(known in text for known in KNOWN_VOCABULARY)


class PatternMatcher:
    """Implements the Classifier Protocol."""

    model_version = MODEL_VERSION

    async def classify(self, items: Sequence[CommentContext]) -> Sequence[Verdict]:
        return [self._classify_one(item) for item in items]

    def _classify_one(self, item: CommentContext) -> Verdict:
        text = item.text
        scam = _hits(text, SCAM)
        strong = _hits(text, ABUSE_STRONG)
        mild = _hits(text, ABUSE_MILD)
        institution = _hits(text, INSTITUTION_MARKERS)
        person = _hits(text, PERSON_MARKERS)
        complaint = _hits(text, COMPLAINT)

        # 1. Language we have never seen. Abstain rather than guess.
        if _looks_novel(text):
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

        # 3. How severe?
        if scam:
            severity, severity_confidence = Severity.HARMFUL, 0.88
            rationale = f"Scam pattern: {', '.join(scam)}"
        elif strong:
            severity, severity_confidence = Severity.HARMFUL, 0.79
            rationale = f"Strong abusive term: {', '.join(strong)}"
        elif mild:
            severity, severity_confidence = Severity.OFFENSIVE, 0.71
            rationale = f"Abusive term: {', '.join(mild)}"
        elif complaint:
            severity, severity_confidence = Severity.OFFENSIVE, 0.62
            rationale = "Complaint vocabulary without abusive terms."
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
