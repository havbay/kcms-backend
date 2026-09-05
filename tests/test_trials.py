from datetime import UTC, datetime, timedelta

from kcms.auth.repository import trial_expired


def test_trial_workspace_expires_at_seven_day_boundary():
    now = datetime(2026, 9, 5, tzinfo=UTC)
    assert trial_expired({"plan": "TRIAL", "trial_expires_at": now}, now=now)
    assert not trial_expired(
        {"plan": "TRIAL", "trial_expires_at": now + timedelta(seconds=1)}, now=now
    )


def test_paid_workspace_is_not_expired_even_with_an_old_trial_timestamp():
    assert not trial_expired(
        {"plan": "STARTER", "trial_expires_at": datetime(2020, 1, 1, tzinfo=UTC)},
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )
