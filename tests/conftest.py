import pytest

from kcms.settings import settings


@pytest.fixture(autouse=True)
def enable_signup_for_test_fixtures(monkeypatch):
    """Enable the legacy test provisioning seam, never production signup."""

    monkeypatch.setattr(settings, "public_signup_enabled", True)
