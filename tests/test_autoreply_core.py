from kcms.autoreply.pipeline import InboundMessage, ReplyDecision, decide_reply
from kcms.autoreply.rules import ReplyRule, RuleValidationError, validate_rule


def rule(**overrides):
    values = {
        "id": "r-1",
        "name": "Opening hours",
        "keywords": ("ម៉ោងបើក",),
        "reply_body": "We are open from 8:00 to 17:00.",
        "on_comments": True,
        "on_messages": False,
        "position": 1,
        "enabled": True,
    }
    values.update(overrides)
    return ReplyRule(**values)


def test_first_enabled_rule_by_position_wins_when_any_keyword_matches():
    decision = decide_reply(
        InboundMessage(text="សូមប្រាប់ម៉ោងបើក", channel="comments"),
        [
            rule(id="r-later", position=2, keywords=("ម៉ោង",), reply_body="later"),
            rule(id="r-first", position=1, keywords=("ម៉ោងបើក",), reply_body="first"),
        ],
    )

    assert decision == ReplyDecision(kind="REPLY", rule_id="r-first", reply_body="first")


def test_unsafe_message_never_reaches_reply_matching():
    decision = decide_reply(
        InboundMessage(text="ម៉ោងបើក អ្នកនេះជាមនុស្សអាក្រក់", channel="comments", severity="OFFENSIVE"),
        [rule()],
    )

    assert decision.kind == "UNSAFE"
    assert decision.rule_id is None


def test_page_echo_and_already_answered_messages_are_silent():
    page_echo = decide_reply(
        InboundMessage(text="ម៉ោងបើក", channel="comments", from_page=True), [rule()]
    )
    answered = decide_reply(
        InboundMessage(text="ម៉ោងបើក", channel="comments", already_answered=True), [rule()]
    )

    assert page_echo.kind == "INELIGIBLE"
    assert answered.kind == "INELIGIBLE"


def test_message_rule_is_not_used_for_comments():
    decision = decide_reply(
        InboundMessage(text="ម៉ោងបើក", channel="comments"),
        [rule(on_comments=False, on_messages=True)],
    )

    assert decision.kind == "NO_MATCH"


def test_disabled_and_sandbox_rules_are_silent():
    disabled = decide_reply(
        InboundMessage(text="ម៉ោងបើក", channel="comments"), [rule(enabled=False)]
    )
    sandbox = decide_reply(
        InboundMessage(text="ម៉ោងបើក", channel="comments", sandbox=True), [rule()]
    )

    assert disabled.kind == "NO_MATCH"
    assert sandbox.kind == "INELIGIBLE"


def test_rule_validation_returns_short_keyword_error_and_cluster_warning():
    try:
        validate_rule(
            name="Bad",
            keywords=("អ", "សត្វ"),
            reply_body="Reply",
            on_comments=True,
            on_messages=False,
        )
    except RuleValidationError as error:
        assert "4 codepoints" in str(error)
    else:
        raise AssertionError("short Khmer keyword was accepted")

    validated = validate_rule(
        name="Warning",
        keywords=("សត្វល្អ",),
        reply_body="Reply",
        on_comments=True,
        on_messages=False,
    )
    assert validated.warnings == ()


def test_rule_validation_rejects_empty_or_oversized_fields():
    for values, expected in (
        ({"name": "", "keywords": ("ម៉ោងបើក",), "reply_body": "Reply"}, "name"),
        ({"name": "Good", "keywords": (), "reply_body": "Reply"}, "keyword"),
        ({"name": "Good", "keywords": ("ម៉ោងបើក",), "reply_body": "x" * 1001}, "reply_body"),
    ):
        try:
            validate_rule(
                **values,
                on_comments=True,
                on_messages=False,
            )
        except RuleValidationError as error:
            assert expected in str(error)
        else:
            raise AssertionError(f"invalid {expected} field was accepted")
