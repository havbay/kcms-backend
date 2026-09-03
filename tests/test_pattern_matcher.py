"""The behaviours this product exists to protect."""

import pytest

from kcms.moderation.contracts import CommentContext, Severity, SurfacedReason, Target
from kcms.moderation.pattern_matcher import PatternMatcher


async def classify_one(text: str):
    (verdict,) = await PatternMatcher().classify([CommentContext(comment_id="c1", text=text)])
    return verdict


async def classify_with(text: str, allowlist=(), blocklist=()):
    matcher = PatternMatcher(allowlist=allowlist, blocklist=blocklist)
    (verdict,) = await matcher.classify([CommentContext(comment_id="c1", text=text)])
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

    assert verdict.abstain is False
    assert verdict.surfaced_reason is SurfacedReason.CLEARED



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


async def test_blocklist_forces_harmful_on_otherwise_safe_text():
    verdict = await classify_with("អាហារនេះឆ្ងាញ់ណាស់", blocklist=["ឆ្ងាញ់"])

    assert verdict.severity is Severity.HARMFUL
    assert "ឆ្ងាញ់" in (verdict.rationale or "")


async def test_allowlist_overrides_a_default_harmful_keyword():
    """The scam phrase from test_scam_is_treated_as_harmful, but this
    workspace has explicitly cleared it — the allowlist wins over the AI."""
    text = "គណនីនេះស្នើសុំលេខកូដ សូមប្រយ័ត្ន។"
    without_override = await classify_one(text)
    assert without_override.severity is Severity.HARMFUL  # sanity check

    verdict = await classify_with(text, allowlist=["លេខកូដ"])
    assert verdict.severity is Severity.SAFE


async def test_allowlist_wins_when_both_allowlist_and_blocklist_match():
    verdict = await classify_with(
        "នេះជាការសាកល្បង", allowlist=["សាកល្បង"], blocklist=["សាកល្បង"]
    )
    assert verdict.severity is Severity.SAFE


async def test_an_ambiguous_target_never_surfaces_a_comment_that_is_otherwise_safe():
    """Abstaining on *who* it's aimed at is not a reason to ask a human when
    the comment itself carries no risk — only an unsure severity is. A
    person marker and an institution marker together with no risk words:
    ambiguous target (abstain), but nothing else is uncertain."""
    verdict = await classify_one("អ្នកនេះ ក្រុមហ៊ុន សួស្តី")

    assert verdict.severity is Severity.SAFE
    assert verdict.abstain is True
    assert verdict.surfaced_reason is SurfacedReason.CLEARED


async def test_a_blocklisted_institution_complaint_still_never_routes_to_triage():
    """Forcing HARMFUL must not defeat the institution-exception: hostile
    criticism of a company still goes to institution_sample, not triage."""
    text = "ធនាគារនេះសេវាអន់ណាស់"
    verdict = await classify_with(text, blocklist=["សេវាអន់ណាស់"])

    assert verdict.severity is Severity.HARMFUL
    assert verdict.target is Target.INSTITUTION
    assert verdict.surfaced_reason is SurfacedReason.INSTITUTION_SAMPLE
    assert verdict.surfaced_reason is not SurfacedReason.TRIAGE
