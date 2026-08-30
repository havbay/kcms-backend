"""Scripted Khmer comments for the prototype.

Kept deliberately separate from any production integration (see AGENTS.md).
These are hand-written examples, NOT real user content, and they exist to
exercise the contrast the product turns on: targeted abuse and scams versus
angry-but-legitimate complaint.

Replace with real labelled data once the annotation guideline exists.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeedComment:
    comment_id: str
    author_ref: str
    text: str
    post_text: str | None = None
    parent_text: str | None = None
    is_reply: bool = False


PAGE_ID = "demo-page"

SEED_COMMENTS: list[SeedComment] = [
    # --- legitimate institutional complaint: must stay visible -------------
    SeedComment("c-001", "user-a", "សេវាកម្មក្រុមហ៊ុននេះយឺតណាស់ ខកចិត្តខ្លាំង។"),
    SeedComment("c-002", "user-b", "ធនាគារនេះសេវាអន់ណាស់ រង់ចាំយូរពេក។"),
    SeedComment("c-003", "user-c", "ក្រសួងនេះធ្វើការយឺត តម្លៃថ្លៃ គុណភាពមិនល្អ។"),
    # --- person-directed abuse: must reach a human ------------------------
    SeedComment("c-004", "user-d", "អ្នកនេះល្ងង់ណាស់ កុំឱ្យវានិយាយ។"),
    SeedComment("c-005", "user-e", "បងនេះឆ្កួតហើយ គ្មានខួរសោះ។"),
    # --- scam / fraud -----------------------------------------------------
    SeedComment("c-006", "user-f", "គណនីនេះស្នើសុំលេខកូដ សូមប្រយ័ត្ន។"),
    SeedComment("c-007", "user-g", "អ្នកឈ្នះរង្វាន់ហើយ ចុចលីងនេះដើម្បីទទួលរង្វាន់។"),
    SeedComment("c-008", "user-h", "វិនិយោគជាមួយយើង ចំណេញ100% ក្នុងមួយសប្តាហ៍។"),
    # --- ordinary safe traffic --------------------------------------------
    SeedComment("c-009", "user-i", "សួស្តី តម្លៃប៉ុន្មានដែរ?"),
    SeedComment("c-010", "user-j", "អរគុណច្រើន សេវាកម្មល្អ។"),
    # --- unknown slang: must abstain, not be cleared -----------------------
    SeedComment("c-011", "user-k", "ស្អីគេចឹង បងៗ ធ្វើម៉េចទៅ"),
    SeedComment("c-012", "user-l", "ចឹងហ្នឹងណា មិនយល់សោះ"),
]
