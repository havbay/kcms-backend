"""The behaviours this product exists to protect."""

import pytest

from kcms.moderation.contracts import CommentContext, Severity, SurfacedReason, Target
from kcms.moderation.pattern_matcher import PatternMatcher


async def classify_one(text: str):
    (verdict,) = await PatternMatcher().classify([CommentContext(comment_id="c1", text=text)])
    return verdict


async def test_institution_complaint_is_not_routed_as_harm_to_remove():
    """The forcing case: angry criticism of a company must not be treated
    the same as targeted abuse."""
    verdict = await classify_one("សេវាកម្មក្រុមហ៊ុននេះយឺតណាស់ ខកចិត្តខ្លាំង។")

    assert verdict.target is Target.INSTITUTION
    assert verdict.surfaced_reason is SurfacedReason.INSTITUTION_SAMPLE
    assert verdict.surfaced_reason is not SurfacedReason.TRIAGE


async def test_person_directed_abuse_reaches_a_human():
    verdict = await classify_one("អ្នកនេះល្ងង់ណាស់ កុំឱ្យវានិយាយ។")

    assert verdict.target is Target.PERSON
    assert verdict.severity in (Severity.OFFENSIVE, Severity.HARMFUL)
    assert verdict.surfaced_reason is SurfacedReason.TRIAGE


async def test_scam_is_treated_as_harmful():
    verdict = await classify_one("គណនីនេះស្នើសុំលេខកូដ សូមប្រយ័ត្ន។")

    assert verdict.severity is Severity.HARMFUL
    assert "លេខកូដ" in (verdict.rationale or "")


async def test_unknown_khmer_slang_abstains_instead_of_being_cleared():
    """A no-pattern-hit is not evidence of safety. New slang must reach a
    human, or the matcher silently clears exactly what it cannot read."""
    verdict = await classify_one("ស្អីគេចឹង បងៗ ធ្វើម៉េចទៅ")

    assert verdict.abstain is True
    assert verdict.surfaced_reason is SurfacedReason.NOVEL_LANGUAGE
    assert verdict.surfaced_reason is not SurfacedReason.CLEARED


async def test_every_verdict_is_attributable_to_a_model_version():
    verdict = await classify_one("សួស្តី")
    assert verdict.model_version == "pattern-matching-v0.1"


@pytest.mark.parametrize(
    "text",
    [
        "សេវាកម្មក្រុមហ៊ុននេះយឺតណាស់",
        "ក្រសួងនេះធ្វើការយឺត ខកចិត្ត",
        "ធនាគារនេះសេវាអន់ណាស់",
    ],
)
async def test_institution_criticism_is_never_routed_to_triage(text: str):
    verdict = await classify_one(text)
    assert verdict.target is Target.INSTITUTION
    assert verdict.surfaced_reason is not SurfacedReason.TRIAGE
